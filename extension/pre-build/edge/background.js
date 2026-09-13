/* Extension runtime layer. Owns tab lifecycle and delegates transport/site work. */
if (typeof importScripts === "function" && !globalThis.FancyGPTTransport) {
  importScripts("bridge_transport.js");
}
const ext = globalThis.browser ?? globalThis.chrome;
const DEFAULTS = {
  transport: "websocket",
  endpoint: "ws://127.0.0.1:8765",
  token: "",
  tunnelId: "edge-remote",
  browserName: "edge",
  nativeHost: "com.fancygpt.bridge",
  autoConnect: true,
  reconnectIntervalMs: 2000,
};

let taskWindowId = null;
// The tab left behind to keep the task window alive between jobs. Removing a
// window's last tab closes the window, so without this the window created for
// one job is gone before the next arrives.
let taskKeeperTabId = null;

async function taskWindowFor(url) {
  if (taskWindowId != null) {
    try {
      await ext.windows.get(taskWindowId);
      // Active, and the window brought back up. A tab that is not the active
      // one in its window is a hidden document however visible the window is,
      // and a hidden document stops being painted mid-reply.
      /* Inactive, and pushed back down afterwards.
       *
       * Adding an active tab restores a minimized window, so asking for one
       * undid the minimizing on every turn after the first: measured, seven of
       * eight turns ran with the document visible and focused, which is the
       * window appearing in front of the user again.
       *
       * There is no longer anything to gain by activating it. The reply is
       * read from the response that carried it, not from a painted page.
       */
      const tab = await ext.tabs.create({url, windowId: taskWindowId, active: false});
      try { await ext.windows.update(taskWindowId, {state: "minimized"}); } catch (_) {}
      return tab;
    } catch (_) {
      taskWindowId = null;
    }
  }
  /* Out of the way, and it can stay there.
   *
   * This window was focused because a hidden document stops being painted and
   * the reply froze part-written in the DOM. That is no longer how a reply is
   * read: the turn ends when the response that carried it ends, and its text
   * comes from that response. So the automation no longer needs the screen,
   * and taking it was the last thing making this intrusive.
   *
   * Minimized rather than merely unfocused, because an unfocused window still
   * sits somewhere in the way.
   */
  // No geometry alongside the state: Chrome rejects the whole call with
  // "Invalid value for state" when left, top, width or height are combined
  // with minimized, and every turn then fails before it opens a tab. A
  // minimized window has no geometry worth asking for anyway.
  const created = await ext.windows.create({url, focused: false, state: "minimized"});
  taskWindowId = created.id;
  return created.tabs?.[0] ?? null;
}

/* Give the tab back without destroying the window it lives in.
 *
 * Removing a window's last tab closes the window, so simply removing the task
 * tab meant the window never survived a single job: the next one found a dead
 * window id, created a fresh window, and took the user's focus again. The
 * reuse path and the idle close below could never run at all.
 *
 * One tab is therefore parked on a blank page instead of removed, which costs
 * nothing to keep and leaves the window reusable. It is closed with the window
 * once work has genuinely stopped arriving.
 */
async function releaseTaskTab(tabId) {
  if (taskWindowId == null) {
    try { await ext.tabs.remove(tabId); } catch (_) {}
    return;
  }
  let siblings = [];
  try { siblings = await ext.tabs.query({windowId: taskWindowId}) ?? []; } catch (_) {}
  const isLast = siblings.length <= 1 && siblings.some(item => item.id === tabId);
  if (!isLast) {
    try { await ext.tabs.remove(tabId); } catch (_) {}
    if (taskKeeperTabId === tabId) taskKeeperTabId = null;
    return;
  }
  try {
    await ext.tabs.update(tabId, {url: "about:blank"});
    taskKeeperTabId = tabId;
  } catch (_) {
    try { await ext.tabs.remove(tabId); } catch (_) {}
    taskKeeperTabId = null;
  }
}

/* Close the automation window once it has been idle for a while.
 *
 * Closing it the instant a job ends means the next job creates a new one, and
 * creating a window is visible to the user however unfocused it is asked to be.
 * Keeping it forever leaves an empty minimized window behind for the rest of
 * the session. So it is reused while work keeps arriving, and closed once it
 * has genuinely gone quiet.
 */
const TASK_WINDOW_IDLE_MS = 30000;
let taskWindowIdleTimer = null;

function scheduleTaskWindowClose() {
  if (taskWindowIdleTimer != null) clearTimeout(taskWindowIdleTimer);
  taskWindowIdleTimer = setTimeout(() => {
    taskWindowIdleTimer = null;
    closeTaskWindowIfIdle().catch(() => {});
  }, TASK_WINDOW_IDLE_MS);
}

async function closeTaskWindowIfIdle() {
  if (taskWindowId == null || activeJobs.size > 0) return;
  const windowId = taskWindowId;
  try {
    const remaining = await ext.tabs.query({windowId});
    // Only ever close a window this extension created, and only when nothing is
    // left in it but the blank tab parked there to keep it open: never take
    // away a tab the user opened.
    const onlyOurs = (remaining ?? []).every(item => item.id === taskKeeperTabId);
    if (!remaining || remaining.length === 0 || onlyOurs) {
      taskKeeperTabId = null;
      taskWindowId = null;
      await ext.windows.remove(windowId);
    }
  } catch (_) {
    // Already gone, which is the state we wanted anyway.
    taskWindowId = null;
  }
}

async function getConfig() {
  const value = await ext.storage.local.get(DEFAULTS);
  return {...DEFAULTS, ...value};
}

async function sendToContent(tabId, message, retries = 50) {
  let lastError = null;
  for (let i = 0; i < retries; ++i) {
    try { return await ext.tabs.sendMessage(tabId, message); }
    catch (error) { lastError = error; await new Promise(resolve => setTimeout(resolve, 200)); }
  }
  throw lastError ?? new Error("site content adapter didn't become ready");
}

function whenTabCloses(tabId) {
  return new Promise(resolve => {
    const listener = closedId => {
      if (closedId === tabId) {
        ext.tabs.onRemoved.removeListener(listener);
        resolve();
      }
    };
    ext.tabs.onRemoved.addListener(listener);
  });
}

// ext.tabs.sendMessage() does not reliably reject when the receiving tab is
// closed mid-execution (the content script's context is just destroyed), so
// a manually or externally closed task tab can otherwise hang the whole job
// until its full timeout instead of failing fast.
async function sendToContentOrTabClose(tabId, message) {
  const closed = whenTabCloses(tabId).then(() => {
    throw new Error("site task tab was closed before the turn completed");
  });
  return Promise.race([sendToContent(tabId, message), closed]);
}

const CONVERSATION_ID_PATTERN = /^[a-zA-Z0-9-]{8,64}$/;

// Where each site is opened for each conversation mode. The runtime layer needs
// its own table because the site adapters live in the content script and are not
// reachable from here. Adding a site means adding an entry, its adapter, and a
// manifest match -- no change to the job or tab machinery below.
const SITES = {
  chatgpt: {
    hosts: ["chatgpt.com"],
    conversation: id => `https://chatgpt.com/c/${id}`,
    // Both modes are stated explicitly rather than letting the bare origin
    // inherit whichever mode the UI was last left in.
    persistent: "https://chatgpt.com/?temporary-chat=false",
    fresh: "https://chatgpt.com/?temporary-chat=true",
  },
  gemini: {
    hosts: ["gemini.google.com"],
    conversation: id => `https://gemini.google.com/app/${id}`,
    // Gemini has no not-saved chat that still yields a URL, so a new
    // conversation is the same page in both modes.
    persistent: "https://gemini.google.com/app",
    fresh: "https://gemini.google.com/app",
  },
};

function taskUrlFor(site, conversation) {
  const policy = SITES[site];
  if (!policy) throw new Error(`unsupported site: ${site}`);
  const mode = conversation?.mode;
  if (mode === "continue" && CONVERSATION_ID_PATTERN.test(String(conversation.conversation_id ?? ""))) {
    return policy.conversation(conversation.conversation_id);
  }
  return mode === "persistent" ? policy.persistent : policy.fresh;
}

/* Which tab is serving which job, so a cancel can find it.
 * The generation epoch lets a cancel for a previous occupant of a recycled tab
 * be discarded instead of stopping the turn currently using it. */
const activeJobs = new Map();

/* An unthrottled clock for the content script, over a long-lived port.
 *
 * The automation tab lives in a minimized window where setTimeout is throttled
 * hard, so a reply that has finished - and therefore stops mutating the DOM -
 * can go unnoticed for a minute.
 *
 * Chrome applies intensive throttling to a minimized window: timers there fire
 * about once a MINUTE. One of the conditions is that the page has been silent
 * for 30 seconds, so it engages precisely when a reply has just finished - the
 * moment a clock is most needed. Measured here: a reply that arrived at 7
 * seconds took 69 to be noticed.
 *
 * Timers inside an extension service worker are NOT throttled, so the clock
 * belongs here. But a timer alone does not keep a Manifest V3 service worker
 * alive: it is terminated after about 30 seconds idle, the ticks stop, and the
 * turn hangs exactly as before - which is what happened, intermittently.
 *
 * A connected port does reset the idle timer. So the content script opens one
 * for the duration of its turn and is ticked over it: the port keeps this
 * worker alive, and this worker's unthrottled timer drives the tick. The tick
 * carries no data; it exists only so the turn gets to re-check.
 */
const TICK_INTERVAL_MS = 400;
const TICK_PORT_PREFIX = "fancy-tick:";

ext.runtime.onConnect.addListener(port => {
  if (!port.name || !port.name.startsWith(TICK_PORT_PREFIX)) return;
  const timer = setInterval(() => {
    try { port.postMessage({type: "fancy_tick"}); }
    catch (_) { clearInterval(timer); }
  }, TICK_INTERVAL_MS);
  port.onDisconnect.addListener(() => clearInterval(timer));
});

/* Answer every cancel, rather than letting the bridge acknowledge it blind.
 *
 * A cancel can legitimately do nothing - the job is unknown, or it names an
 * older generation - and a caller told "accepted" in those cases believes the
 * turn is stopping when it is not. */
async function cancelJob(message) {
  const jobId = String(message.job_id ?? "");
  const reply = (accepted, reason) => {
    try { globalThis.FancyGPTTransport.send({
      type: "cancel_result", job_id: jobId, control_id: message.control_id, accepted, reason
    }); }
    catch (_) {}
  };
  const entry = activeJobs.get(jobId);
  if (!entry) { reply(false, "no such job is running"); return; }
  const epoch = Number(message.generation_epoch ?? 0);
  if (Number(entry.epoch ?? 0) !== epoch) { reply(false, "cancel names a different generation"); return; }
  entry.cancelled = true;
  if (entry.tabId == null) { reply(true, "cancelled before the tab was created"); return; }
  try {
    await ext.tabs.sendMessage(entry.tabId, {type: "fancy_cancel_turn", jobId});
    reply(true, "the page was asked to stop");
  } catch (_) {
    // The tab may already be gone; the turn unwinds on its own.
    reply(true, "the tab is already gone");
  }
}

async function executeJob(job) {
  let tab = null;
  try {
    if (!["model.turn", "site.health"].includes(job.operation)) throw new Error(`unsupported operation: ${job.operation}`);
    // Which sites this build can drive is decided by which adapters registered
    // themselves, not by a name hardcoded in the runtime layer.
    const site = String(job.site ?? "chatgpt");
    if (!SITES[site]) throw new Error(`unsupported site: ${site}`);
    const taskUrl = taskUrlFor(site, job.conversation);
    const config = await getConfig();
    // Claimed before the tab exists: otherwise another job finishing in this
    // moment sees an idle window and closes it while this tab is being created.
    const entry = {tabId: null, epoch: Number(job.generation_epoch ?? 0), cancelled: false};
    activeJobs.set(job.job_id, entry);
    if (taskWindowIdleTimer != null) { clearTimeout(taskWindowIdleTimer); taskWindowIdleTimer = null; }
    /* Always the extension's own window, never the one you are working in.
     *
     * Driving a page means the page has to be drawn, and a tab that is not the
     * active one in its window is a hidden document that stops being drawn
     * mid-reply. Sharing the user's window therefore cannot be done quietly:
     * it would have to keep taking over the tab in front of them. The separate
     * window is what makes that cost affordable -- it is the automation's own
     * space, reused for as long as work keeps arriving.
     */
    tab = await taskWindowFor(taskUrl);
    if (!tab || tab.id == null) throw new Error("failed to create site task tab");
    entry.tabId = tab.id;
    if (entry.cancelled) {
      globalThis.FancyGPTTransport.send({
        type: "job_cancelled", job_id: job.job_id, text: "", stopped_generation: false,
        conversation_id: null, reason: "cancelled before the tab was ready"
      });
      return;
    }
    if (job.operation === "site.health") {
      const health = await sendToContentOrTabClose(tab.id, {type: "fancy_site_health", site});
      const payload = health ?? {ok: false, reason: "site-health-no-response"};
      globalThis.FancyGPTTransport.send({
        type: "job_result", job_id: job.job_id, text: JSON.stringify(payload),
        response_identity: "site-health"
      });
      return;
    }
    const result = await sendToContentOrTabClose(tab.id, {
      type: "fancy_execute_turn",
      site,
      prompt: job.prompt,
      jobId: job.job_id,
      continuing: job.conversation?.mode === "continue",
      // Deliberately short of the job's own deadline. If the adapter and the
      // bridge time out together, the bridge wins the race and reports a
      // generic "worker timed out", throwing away the adapter's account of what
      // it could actually see - which is the only thing that makes an
      // intermittent hang diagnosable.
      timeoutMs: Math.max(1000, Math.floor(((job.timeout_s ?? 300) - 5) * 1000))
    });
    if (!result || !result.ok) throw new Error(result?.error ?? "site content adapter failed");
    if (result.diagnostics) {
      console.info("FancyGPT turn diagnostics", {browser: config.browserName, site, jobId: job.job_id, ...result.diagnostics});
    }
    if (result.cancelled) {
      // Report cancellation distinctly. The controller must be able to tell a
      // stopped turn from one that answered, and keep whatever partial text
      // existed rather than treating it as a complete reply.
      globalThis.FancyGPTTransport.send({
        type: "job_cancelled",
        job_id: job.job_id,
        text: result.text ?? "",
        stopped_generation: Boolean(result.stoppedGeneration),
        conversation_id: result.conversationId ?? null
      });
      return;
    }
    globalThis.FancyGPTTransport.send({
      type: "job_result",
      job_id: job.job_id,
      text: result.text,
      assistant_turn_id: result.responseIdentity,
      response_identity: result.responseIdentity,
      conversation_id: result.conversationId ?? null,
      diagnostics: {
        ...(result.diagnostics ?? {}),
        // Everything watched since the last turn, including sites this one did
        // not touch. Cleared as it leaves, so each turn carries what is new.
        otherSites: await takeObservations(),
      }
    });
  } catch (error) {
    globalThis.FancyGPTTransport.send({type: "job_error", job_id: job.job_id, error: String(error?.message ?? error)});
  } finally {
    activeJobs.delete(job.job_id);
    if (tab?.id != null) await releaseTaskTab(tab.id);
    scheduleTaskWindowClose();
  }
}

/* Shapes seen on sites we watch but do not drive.
 *
 * Kept here because a site with no adapter has no turn of its own to report
 * through. They ride out with the next turn that does run, so measuring a new
 * site needs no new transport and no new permission: log in, ask it something
 * by hand, and the shapes arrive in the next ordinary turn's diagnostics.
 *
 * Bounded, and shapes only. This is a notebook, not a log.
 */
const OBSERVATION_KEY = "fancyGptSiteObservations";

async function rememberObservation(origin, report) {
  /* Stored, not held in a variable.
   *
   * A Manifest V3 service worker is torn down when it goes idle and started
   * again on demand, so anything it keeps in memory is gone by the time the
   * next turn asks for it. That is exactly what happened: observations from
   * Grok and Copilot were recorded correctly and had evaporated before any
   * turn could carry them out, which read as "the hook saw nothing" -- the
   * same silence as a hook that never ran.
   */
  try {
    const stored = await ext.storage.local.get(OBSERVATION_KEY);
    const kept = Array.isArray(stored?.[OBSERVATION_KEY]) ? stored[OBSERVATION_KEY] : [];
    kept.push({origin, at: new Date().toISOString(), ...report});
    while (kept.length > 24) kept.shift();
    await ext.storage.local.set({[OBSERVATION_KEY]: kept});
  } catch (_) {
    // Losing an observation costs a measurement, never a turn.
  }
}

async function takeObservations() {
  try {
    const stored = await ext.storage.local.get(OBSERVATION_KEY);
    const kept = Array.isArray(stored?.[OBSERVATION_KEY]) ? stored[OBSERVATION_KEY] : [];
    if (kept.length) await ext.storage.local.set({[OBSERVATION_KEY]: []});
    return kept;
  } catch (_) {
    return [];
  }
}

globalThis.FancyGPTTransport.setHandler(async message => {
  if (message.type === "job") await executeJob(message);
  else if (message.type === "cancel") await cancelJob(message);
});

async function connectBridge() {
  const config = await getConfig();
  await globalThis.FancyGPTTransport.connect(config);
}

ext.runtime.onInstalled.addListener(() => connectBridge().catch(console.error));
ext.runtime.onStartup?.addListener(() => connectBridge().catch(console.error));
ext.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "fancy_reconnect") {
    globalThis.FancyGPTTransport.disconnect();
    connectBridge().then(() => sendResponse({ok: true})).catch(error => sendResponse({ok: false, error: String(error)}));
    return true;
  }
  if (message?.type === "fancy_site_observation") {
    rememberObservation(String(message.origin ?? "unknown"), message.report ?? {});
    return false;
  }
  if (message?.type === "fancy_status") sendResponse(globalThis.FancyGPTTransport.status());
  if (message?.type === "fancy_progress" && message.jobId) {
    try { globalThis.FancyGPTTransport.send({type: "job_progress", job_id: message.jobId, text: String(message.text ?? "")}); }
    catch (_) {}
  }
});

connectBridge().catch(console.error);
