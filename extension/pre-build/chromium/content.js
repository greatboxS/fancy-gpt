/* Content dispatcher. No site-specific selectors belong here. */
const ext = globalThis.browser ?? globalThis.chrome;

/* Jobs the controller asked to stop. The adapter's poll loop consults this,
 * so cancelling is a cooperative check rather than an attempt to kill a
 * running promise. Ids are kept after the turn ends so a cancel that arrives
 * late is still recorded rather than starting an unstoppable turn. */
const cancelledJobs = new Set();

/* A turn opens a port to the background worker for its lifetime.
 *
 * The port is what gives this tab an unthrottled clock: this window's timers
 * are throttled while it is minimized, and a reply that has finished stops
 * mutating the DOM, so without a tick the turn has nothing to wake it. The port
 * also keeps the Manifest V3 service worker alive, which a timer there would
 * not. The tick carries no data. */
function openTickPort(jobId, onTick) {
  let port = null;
  try {
    port = ext.runtime.connect({name: `fancy-tick:${jobId}`});
    port.onMessage.addListener(() => { try { onTick(); } catch (_) {} });
  } catch (_) {
    // No clock available; the turn still runs on DOM mutations alone.
    return () => {};
  }
  return () => { try { port.disconnect(); } catch (_) {} };
}

/* Install the page-world observer and keep what it reports.
 *
 * It runs in the page's own world, which a content script cannot reach, so it
 * is injected as a script element -- the one method that works the same in
 * Chrome, Edge and Firefox. It reports shapes and counts only, and this keeps
 * just the most recent few so a turn can carry them in its diagnostics.
 */
const pageHookReports = [];

/* Did the hook land in the page's world, or in ours?
 *
 * The two worlds have separate window objects, so a hook that ran where it was
 * meant to is invisible from here. Seeing its marker means it ran beside this
 * script instead, wrapping a fetch the page never calls -- which looks exactly
 * like a site that makes no requests, and would be read as one.
 */
function hookLandedInTheWrongWorld() {
  try { return window.__fancyGptPageHook === true; } catch (_) { return false; }
}

function installPageHook() {
  try {
    const element = document.createElement("script");
    element.src = ext.runtime.getURL("page_hook.js");
    element.async = false;
    (document.head || document.documentElement).appendChild(element);
    element.addEventListener("load", () => element.remove());
  } catch (_) {
    // Without it a turn still runs exactly as before: this observes, and the
    // turn does not depend on what it sees.
  }
}

/* The events a site streamed, forwarded unread.
 *
 * Only the most recent stream, because a turn is interested in the one it just
 * ran. Nothing here interprets them: what a reply looks like is knowledge that
 * changes when a site changes, and knowledge kept in an extension can only be
 * corrected by asking someone to reload their browser.
 */
/* Several, not the latest.
 *
 * Keeping only the most recent capture meant any request that finished after
 * the reply overwrote it -- a telemetry call of a few hundred characters
 * replacing a reply of eleven thousand, so the turn reported no stream at all
 * or decoded an empty answer. Which request carries the reply is knowledge
 * about a site, and that belongs in the runtime, so all of them are handed
 * over and it chooses.
 */
const CAPTURES = 6;
let streamedEvents = [];
let streamedBody = [];
let streamFinishedAt = null;

function sizeOf(report) {
  if (Array.isArray(report.events)) return report.events.join("").length;
  return String(report.text ?? "").length;
}

function remember(list, report) {
  list.push(report);
  // When it is full, the smallest goes -- not the oldest. Gemini makes eight
  // requests a turn and its reply is not the last of them, so dropping by age
  // pushed an eleven thousand character answer out behind telemetry.
  while (list.length > CAPTURES) {
    let smallest = 0;
    for (let i = 1; i < list.length; ++i) {
      if (sizeOf(list[i]) < sizeOf(list[smallest])) smallest = i;
    }
    list.splice(smallest, 1);
  }
}

window.addEventListener("message", event => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== "fancygpt-page-hook") return;
  const {source, ...report} = data;
  if (report.kind === "events") { remember(streamedEvents, report); return; }
  // A response body that grew while it loaded, which is how a site answering
  // over XHR rather than a server-sent stream delivers its reply.
  if (report.kind === "body") { remember(streamedBody, report); return; }
  // A reply-shaped response has ended. Recorded rather than acted on here: the
  // adapter decides what to do with it, and the runtime decides what the bytes
  // meant.
  if (report.kind === "finished") { streamFinishedAt = Date.now(); return; }
  pageHookReports.push(report);
  // Only the recent ones: this is a diagnostic, not a log.
  if (pageHookReports.length > 8) pageHookReports.shift();
});

/* The hook is declared as a MAIN-world content script, which the browser
 * injects itself, so the page's CSP does not apply to it. A <script> element
 * pointing at an extension file is subject to that CSP and ChatGPT's refuses
 * one: measured, the hook then reported nothing at all -- not even its own
 * install, which reads exactly like a site that makes no requests.
 *
 * Seeing its marker from here means it ran in this world instead, on a browser
 * too old for MAIN-world content scripts. The element is the fallback for that
 * case, and the turn reports which world it ended up in either way.
 */
if (hookLandedInTheWrongWorld()) installPageHook();

ext.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "fancy_cancel_turn") {
    const jobId = String(message.jobId ?? "");
    if (jobId) cancelledJobs.add(jobId);
    sendResponse({ok: Boolean(jobId)});
    return false;
  }
  if (message?.type !== "fancy_execute_turn" && message?.type !== "fancy_site_health") return undefined;
  const siteId = String(message.site ?? "chatgpt");
  const adapter = globalThis.FancyGPTSites?.[siteId];
  if (!adapter) {
    sendResponse({ok: false, error: `unsupported site adapter: ${siteId}`});
    return false;
  }
  let closeTickPort = () => {};
  if (message.type === "fancy_execute_turn") { streamFinishedAt = null; streamedEvents = []; streamedBody = []; }
  const task = message.type === "fancy_site_health"
    ? adapter.healthCheck()
    : adapter.executeTurn(
        String(message.prompt ?? ""),
        Number(message.timeoutMs ?? 300000),
        text => ext.runtime.sendMessage({type: "fancy_progress", jobId: message.jobId, text}).catch(() => {}),
        {
          continuing: Boolean(message.continuing),
          isCancelled: () => cancelledJobs.has(String(message.jobId ?? "")),
          onTick: handler => { closeTickPort = openTickPort(String(message.jobId ?? ""), handler); },
          /* When the network said this turn's reply was over.
           *
           * A hidden document stops being painted, so waiting for the page to
           * look finished can wait forever while the answer sits complete in a
           * response that already ended. This is the same fact, from the one
           * place that does not depend on rendering.
           */
          streamFinishedAt: () => streamFinishedAt,
        },
      );
  Promise.resolve(task)
    .then(result => sendResponse({
      ok: true,
      ...result,
      diagnostics: {
        ...(result?.diagnostics ?? {}),
        pageHook: pageHookReports.slice(),
        streams: streamedEvents.slice(),
        streamBodies: streamedBody.slice(),
        pageHookWorld: hookLandedInTheWrongWorld() ? "isolated" : "page",
      },
    }))
    .catch(error => sendResponse({ok: false, error: String(error?.message ?? error)}))
    .finally(() => { try { closeTickPort(); } catch (_) {} });
  return true;
});
