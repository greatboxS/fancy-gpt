/* Extension runtime layer. Owns tab lifecycle and delegates transport/site work. */
if (typeof importScripts === "function" && !globalThis.FancyGPTTransport) {
  importScripts("bridge_transport.js");
}
const ext = globalThis.browser ?? globalThis.chrome;
const EXTENSION_BUILD = "f73144e08121";
const DEFAULTS = {
  transport: "websocket",
  endpoint: "ws://127.0.0.1:8765",
  token: "",
  tunnelId: "chrome-remote",
  browserName: "chrome",
  nativeHost: "com.fancygpt.bridge",
  autoConnect: true,
  reconnectIntervalMs: 2000,
  maxConcurrentTurns: 4,
};

let nextLeaseId = 1;
const RECONNECT_ALARM = "fancy-gpt-bridge-reconnect";
let bridgeConnectInFlight = null;
const surfaceLeases = new Map();
const SURFACE_SESSION_KEY = "fancyGptSurfaceLeases";
let surfaceStateTail = Promise.resolve();

function withSurfaceStateLock(operation) {
  const next = surfaceStateTail.then(operation, operation);
  surfaceStateTail = next.catch(() => {});
  return next;
}

async function writeSurfaceState() {
  const area = ext.storage.session;
  if (!area) return;
  const records = [...surfaceLeases.values()].map(lease => ({
    leaseId: lease.leaseId, jobId: lease.jobId, epoch: lease.epoch,
    windowId: lease.windowId, tabId: lease.tabId,
  }));
  await area.set({[SURFACE_SESSION_KEY]: records});
}

async function quarantineOrphanSurfaces() {
  const area = ext.storage.session;
  if (!area) return;
  const stored = await area.get(SURFACE_SESSION_KEY);
  const records = Array.isArray(stored?.[SURFACE_SESSION_KEY]) ? stored[SURFACE_SESSION_KEY] : [];
  // A new service-worker generation cannot prove the outcome of an old turn.
  // Never replay it. Close only windows whose exact ids this extension stored.
  for (const record of records) {
    if (Number.isInteger(record?.windowId)) {
      try { await ext.windows.remove(record.windowId); } catch (_) {}
    }
  }
  await area.set({[SURFACE_SESSION_KEY]: []});
}

const surfaceRecovery = withSurfaceStateLock(quarantineOrphanSurfaces).catch(() => {});
let focusTail = Promise.resolve();

async function acquireFocus() {
  const previous = focusTail;
  let release;
  focusTail = new Promise(resolve => { release = resolve; });
  await previous;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    release();
  };
}

async function acquireSurface(jobId, epoch, url) {
  /* Unfocused, but never minimized.
   *
   * This used to minimize the window on the assumption that a minimized window
   * still renders. It does not: Chromium reports a minimized or fully occluded
   * window as hidden, which stops the rendering ChatGPT's UI is driven by. The
   * reply then freezes in the DOM part-written -- measured at 30 characters of
   * a 1294 character answer, with the stop control frozen alongside it, so the
   * turn looked finished and stalled until its deadline. Bringing the same tab
   * to the front let the identical prompt complete.
   *
   * So the window stays a real, rendered window. It is small and unfocused and
   * keeps out of the way, but it is not hidden, because a hidden document
   * cannot be automated through its UI.
   */
  // Focused, for now, because an unfocused window is still occluded behind
  // whatever the user is working in, and an occluded window is hidden: a
  // non-minimized window measured no better than a minimized one, freezing at
  // 21 characters of the same answer. Whether this has to cost the user their
  // focus is the next question; that it must not be hidden is settled.
  const created = await ext.windows.create({
    url, focused: true, state: "normal", width: 900, height: 700, top: 0, left: 0,
  });
  const tab = created.tabs?.[0] ?? null;
  if (created.id == null || tab?.id == null) {
    if (created.id != null) try { await ext.windows.remove(created.id); } catch (_) {}
    throw new Error("failed to create site task surface");
  }
  const lease = {
    leaseId: nextLeaseId++, jobId, epoch, windowId: created.id, tabId: tab.id, released: false,
  };
  surfaceLeases.set(lease.leaseId, lease);
  await withSurfaceStateLock(writeSurfaceState);
  return lease;
}

async function releaseSurface(lease) {
  if (!lease || lease.released) return;
  lease.released = true;
  surfaceLeases.delete(lease.leaseId);
  await withSurfaceStateLock(writeSurfaceState);
  // Close only the exact window created for this lease. A stale completion can
  // never remove another job's tab, even if jobs finish out of order.
  try { await ext.windows.remove(lease.windowId); } catch (_) {}
}

async function getConfig() {
  const value = await ext.storage.local.get(DEFAULTS);
  const config = {...DEFAULTS, ...value};
  // v0.8 pre-build popup briefly derived tunnel identity from transport and
  // persisted e.g. edge-extension-ws-remote. Migrate that value on worker
  // startup so merely reloading an updated unpacked extension repairs it.
  if (/^(chrome|edge|firefox)-extension-(native-local|ws-remote)$/.test(config.tunnelId)) {
    config.tunnelId = `${config.browserName}-remote`;
    await ext.storage.local.set({tunnelId: config.tunnelId});
  }
  return config;
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
  let listener;
  const promise = new Promise(resolve => {
    listener = closedId => {
      if (closedId === tabId) {
        ext.tabs.onRemoved.removeListener(listener);
        resolve();
      }
    };
    ext.tabs.onRemoved.addListener(listener);
  });
  return {promise, dispose: () => ext.tabs.onRemoved.removeListener(listener)};
}

// ext.tabs.sendMessage() does not reliably reject when the receiving tab is
// closed mid-execution (the content script's context is just destroyed), so
// a manually or externally closed task tab can otherwise hang the whole job
// until its full timeout instead of failing fast.
async function sendToContentOrTabClose(tabId, message) {
  const watcher = whenTabCloses(tabId);
  const closed = watcher.promise.then(() => {
    throw new Error("site task tab was closed before the turn completed");
  });
  try { return await Promise.race([sendToContent(tabId, message), closed]); }
  finally { watcher.dispose(); }
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
  grok: {
    hosts: ["grok.com", "x.com"],
    conversation: id => `https://grok.com/c/${id}`,
    persistent: "https://grok.com/",
    fresh: "https://grok.com/",
  },
  kimi: {
    hosts: ["www.kimi.com", "kimi.com", "www.kimi.ai", "kimi.ai"],
    conversation: id => `https://www.kimi.com/chat/${id}`,
    persistent: "https://www.kimi.com/",
    fresh: "https://www.kimi.com/",
  },
  glm: {
    hosts: ["chat.z.ai", "z.ai"],
    conversation: id => `https://chat.z.ai/c/${id}`,
    persistent: "https://chat.z.ai/",
    fresh: "https://chat.z.ai/",
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

function reloadExtension(message) {
  if (activeJobs.size > 0) {
    globalThis.FancyGPTTransport.send({
      type: "reload_result", control_id: message.control_id, accepted: false,
      reason: `${activeJobs.size} browser turn(s) are still active`,
    });
    return;
  }
  try {
    globalThis.FancyGPTTransport.send({
      type: "reload_result", control_id: message.control_id, accepted: true,
      reason: "extension reload scheduled",
    });
  } catch (_) {
    return;
  }
  // Let the acknowledgement leave the socket before replacing this worker.
  setTimeout(() => ext.runtime.reload(), 100);
}

async function executeJob(job) {
  let lease = null;
  let releaseFocus = null;
  try {
    await surfaceRecovery;
    if (!["model.turn", "site.health"].includes(job.operation)) throw new Error(`unsupported operation: ${job.operation}`);
    // Which sites this build can drive is decided by which adapters registered
    // themselves, not by a name hardcoded in the runtime layer.
    const site = String(job.site ?? "chatgpt");
    if (!SITES[site]) throw new Error(`unsupported site: ${site}`);
    const taskUrl = taskUrlFor(site, job.conversation);
    const config = await getConfig();
    const capacity = Math.max(1, Math.min(16, Number(config.maxConcurrentTurns) || 4));
    if (activeJobs.size >= capacity) {
      throw new Error(`browser worker is at capacity (${capacity} concurrent turns)`);
    }
    // Claimed before the tab exists: otherwise another job finishing in this
    // moment sees an idle window and closes it while this tab is being created.
    const entry = {tabId: null, leaseId: null, epoch: Number(job.generation_epoch ?? 0), cancelled: false};
    activeJobs.set(job.job_id, entry);
    /* Always the extension's own window, never the one you are working in.
     *
     * Driving a page means the page has to be drawn, and a tab that is not the
     * active one in its window is a hidden document that stops being drawn
     * mid-reply. Sharing the user's window therefore cannot be done quietly:
     * it would have to keep taking over the tab in front of them. The separate
     * window is what makes that cost affordable -- it is the automation's own
     * space, reused for as long as work keeps arriving.
    */
    releaseFocus = await acquireFocus();
    if (entry.cancelled) {
      globalThis.FancyGPTTransport.send({
        type: "job_cancelled", job_id: job.job_id, text: "", stopped_generation: false,
        conversation_id: null, reason: "cancelled while waiting for browser focus"
      });
      return;
    }
    lease = await acquireSurface(job.job_id, entry.epoch, taskUrl);
    entry.tabId = lease.tabId;
    entry.leaseId = lease.leaseId;
    if (entry.cancelled) {
      globalThis.FancyGPTTransport.send({
        type: "job_cancelled", job_id: job.job_id, text: "", stopped_generation: false,
        conversation_id: null, reason: "cancelled before the tab was ready"
      });
      return;
    }
    if (job.operation === "site.health") {
      const health = await sendToContentOrTabClose(lease.tabId, {type: "fancy_site_health", site});
      releaseFocus();
      releaseFocus = null;
      const payload = health ?? {ok: false, reason: "site-health-no-response"};
      globalThis.FancyGPTTransport.send({
        type: "job_result", job_id: job.job_id, text: JSON.stringify(payload),
        response_identity: "site-health"
      });
      return;
    }
    let submittedResolve;
    entry.submitted = new Promise(resolve => { submittedResolve = resolve; });
    entry.noteSubmitted = submittedResolve;
    const responsePromise = sendToContentOrTabClose(lease.tabId, {
      type: "fancy_execute_turn",
      site,
      prompt: job.prompt,
      jobId: job.job_id,
      leaseId: lease.leaseId,
      generationEpoch: entry.epoch,
      continuing: job.conversation?.mode === "continue",
      // Deliberately short of the job's own deadline. If the adapter and the
      // bridge time out together, the bridge wins the race and reports a
      // generic "worker timed out", throwing away the adapter's account of what
      // it could actually see - which is the only thing that makes an
      // intermittent hang diagnosable.
      timeoutMs: Math.max(1000, Math.floor(((job.timeout_s ?? 300) - 5) * 1000))
    });
    // The response promise can settle before the explicit acknowledgement on a
    // very fast failure/cancel. Either event releases focus; generation itself
    // never owns the arbiter.
    await Promise.race([entry.submitted, responsePromise.then(() => undefined, () => undefined)]);
    releaseFocus();
    releaseFocus = null;
    const result = await responsePromise;
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
    if (releaseFocus) releaseFocus();
    activeJobs.delete(job.job_id);
    await releaseSurface(lease);
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
let observationQueue = Promise.resolve();

function withObservationLock(operation) {
  const next = observationQueue.then(operation, operation);
  observationQueue = next.catch(() => {});
  return next;
}

function rememberObservation(origin, report) {
  return withObservationLock(() => rememberObservationUnlocked(origin, report));
}

async function rememberObservationUnlocked(origin, report) {
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

function takeObservations() {
  return withObservationLock(takeObservationsUnlocked);
}

async function takeObservationsUnlocked() {
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
  else if (message.type === "extension.reload") reloadExtension(message);
});

async function connectBridge() {
  const config = await getConfig();
  await globalThis.FancyGPTTransport.connect({
    ...config,
    capabilities: {
      max_turns: Math.max(1, Math.min(16, Number(config.maxConcurrentTurns) || 4)),
      max_render_slots: Math.max(1, Math.min(16, Number(config.maxConcurrentTurns) || 4)),
      sites: Object.keys(SITES),
      surface_mode: "dedicated-window",
      build: EXTENSION_BUILD,
    },
  });
}

function ensureBridgeConnection() {
  if (globalThis.FancyGPTTransport.status().connected) return Promise.resolve();
  if (bridgeConnectInFlight) return bridgeConnectInFlight;
  bridgeConnectInFlight = connectBridge().finally(() => { bridgeConnectInFlight = null; });
  return bridgeConnectInFlight;
}

// Timers vanish with an MV3 service worker. An alarm is persisted by the
// browser and wakes a fresh worker, so bridge downtime cannot permanently
// strand the extension. One minute is supported by every declared Chromium
// version; the transport's 2s retry remains the fast path while the worker lives.
try { ext.alarms?.create(RECONNECT_ALARM, {periodInMinutes: 1}); } catch (_) {}
ext.alarms?.onAlarm.addListener(alarm => {
  if (alarm?.name === RECONNECT_ALARM) ensureBridgeConnection().catch(console.error);
});

ext.runtime.onInstalled.addListener(() => ensureBridgeConnection().catch(console.error));
ext.runtime.onStartup?.addListener(() => ensureBridgeConnection().catch(console.error));
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
    const entry = activeJobs.get(String(message.jobId));
    if (!entry || entry.tabId !== _sender?.tab?.id || entry.leaseId !== message.leaseId
        || entry.epoch !== Number(message.generationEpoch ?? 0)) return false;
    try { globalThis.FancyGPTTransport.send({type: "job_progress", job_id: message.jobId, text: String(message.text ?? "")}); }
    catch (_) {}
  }
  if (message?.type === "fancy_turn_submitted" && message.jobId) {
    const entry = activeJobs.get(String(message.jobId));
    if (entry && entry.tabId === _sender?.tab?.id && entry.leaseId === message.leaseId
        && entry.epoch === Number(message.generationEpoch ?? 0)) entry.noteSubmitted?.();
  }
});

// A narrow seam for the executable lifecycle test. It intentionally exposes
// ownership operations, not browser credentials or transport internals.
if (globalThis.__FANCYGPT_TEST__) {
  globalThis.FancyGPTBackgroundTest = {
    acquireSurface, releaseSurface, surfaceLeases, activeJobs, rememberObservation, takeObservations,
  };
}

ensureBridgeConnection().catch(console.error);
