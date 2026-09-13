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
  // Run task tabs in their own minimized window instead of the window you are
  // working in, so automation never appears in your tab strip.
  separateTaskWindow: true
};

let taskWindowId = null;

async function taskWindowFor(url) {
  if (taskWindowId != null) {
    try {
      await ext.windows.get(taskWindowId);
      return ext.tabs.create({url, windowId: taskWindowId, active: false});
    } catch (_) {
      taskWindowId = null;
    }
  }
  // A minimized, unfocused window keeps the page rendered -- the automation
  // drives the real ChatGPT UI and needs a live document -- while staying out
  // of the way. Chrome may throttle timers in a hidden window, so completion
  // detection can be slower here than in a visible tab.
  const created = await ext.windows.create({url, focused: false, state: "minimized"});
  taskWindowId = created.id;
  return created.tabs?.[0] ?? null;
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
    // left in it: never take away a tab the user opened.
    if (!remaining || remaining.length === 0) {
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
 * The obvious fix, setInterval here in the background, does not work: this is a
 * Manifest V3 service worker, and a timer does not keep one alive. It is
 * terminated after about 30 seconds idle, the ticks stop, and the turn hangs
 * exactly as before - which is what happened, intermittently, in testing.
 *
 * A connected port does keep the worker alive, so the content script opens one
 * for the duration of its turn and is ticked over it. The tick carries no data;
 * it exists only so the turn gets to re-check.
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

async function cancelJob(message) {
  const jobId = String(message.job_id ?? "");
  const entry = activeJobs.get(jobId);
  if (!entry) return;
  const epoch = Number(message.generation_epoch ?? 0);
  if (Number(entry.epoch ?? 0) !== epoch) return;  // stale: a recycled tab
  entry.cancelled = true;
  try {
    await ext.tabs.sendMessage(entry.tabId, {type: "fancy_cancel_turn", jobId});
  } catch (_) {
    // The tab may already be gone; the turn unwinds on its own.
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
    activeJobs.set(job.job_id, {tabId: null, epoch: Number(job.generation_epoch ?? 0), cancelled: false});
    if (taskWindowIdleTimer != null) { clearTimeout(taskWindowIdleTimer); taskWindowIdleTimer = null; }
    tab = config.separateTaskWindow
      ? await taskWindowFor(taskUrl)
      : await ext.tabs.create({url: taskUrl, active: false});
    if (!tab || tab.id == null) throw new Error("failed to create site task tab");
    if (job.operation === "site.health") {
      const health = await sendToContentOrTabClose(tab.id, {type: "fancy_site_health", site});
      const payload = health ?? {ok: false, reason: "site-health-no-response"};
      globalThis.FancyGPTTransport.send({
        type: "job_result", job_id: job.job_id, text: JSON.stringify(payload),
        response_identity: "site-health"
      });
      return;
    }
    activeJobs.set(job.job_id, {tabId: tab.id, epoch: Number(job.generation_epoch ?? 0), cancelled: false});
    const result = await sendToContentOrTabClose(tab.id, {
      type: "fancy_execute_turn",
      site,
      prompt: job.prompt,
      jobId: job.job_id,
      continuing: job.conversation?.mode === "continue",
      timeoutMs: Math.max(1000, Math.floor((job.timeout_s ?? 300) * 1000))
    });
    if (!result || !result.ok) throw new Error(result?.error ?? "site content adapter failed");
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
      conversation_id: result.conversationId ?? null
    });
  } catch (error) {
    globalThis.FancyGPTTransport.send({type: "job_error", job_id: job.job_id, error: String(error?.message ?? error)});
  } finally {
    activeJobs.delete(job.job_id);
    if (tab?.id != null) try { await ext.tabs.remove(tab.id); } catch (_) {}
    scheduleTaskWindowClose();
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
  if (message?.type === "fancy_status") sendResponse(globalThis.FancyGPTTransport.status());
  if (message?.type === "fancy_progress" && message.jobId) {
    try { globalThis.FancyGPTTransport.send({type: "job_progress", job_id: message.jobId, text: String(message.text ?? "")}); }
    catch (_) {}
  }
});

connectBridge().catch(console.error);
