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

/* The reply as the stream carried it, kept apart from the shape reports.
 *
 * Only the most recent, because a turn compares against the reply for the turn
 * it just ran. It is not returned as the answer: the page is still the source
 * of truth, and this is here to be checked against it on real turns until
 * there is evidence that it agrees.
 */
let streamedReply = null;

window.addEventListener("message", event => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== "fancygpt-page-hook") return;
  const {source, ...report} = data;
  if (report.kind === "reply") { streamedReply = report; return; }
  pageHookReports.push(report);
  // Only the recent ones: this is a diagnostic, not a log.
  if (pageHookReports.length > 8) pageHookReports.shift();
});

/* How the two readings compare, in shapes.
 *
 * Never the text of either, and never a diff of them: what matters is whether
 * they agree, and if not, how far in they first part company and by how much.
 * Two readings that always agree are what would justify trusting the stream;
 * one disagreement is what would stop it.
 */
function compareReadings(fromPage) {
  if (streamedReply == null) return {streamed: false};
  const streamed = String(streamedReply.text ?? "");
  const page = String(fromPage ?? "");
  let divergesAt = null;
  if (streamed !== page) {
    const limit = Math.min(streamed.length, page.length);
    divergesAt = limit;
    for (let i = 0; i < limit; ++i) {
      if (streamed[i] !== page[i]) { divergesAt = i; break; }
    }
  }
  return {
    streamed: true,
    agree: streamed === page,
    streamedChars: streamed.length,
    pageChars: page.length,
    divergesAt,
    stats: streamedReply.stats ?? null,
    hasConversationId: Boolean(streamedReply.conversationId),
    hasMessageId: Boolean(streamedReply.messageId),
  };
}

installPageHook();

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
        },
      );
  Promise.resolve(task)
    .then(result => sendResponse({
      ok: true,
      ...result,
      diagnostics: {
        ...(result?.diagnostics ?? {}),
        pageHook: pageHookReports.slice(),
        streamComparison: compareReadings(result?.text),
      },
    }))
    .catch(error => sendResponse({ok: false, error: String(error?.message ?? error)}))
    .finally(() => { try { closeTickPort(); } catch (_) {} });
  return true;
});
