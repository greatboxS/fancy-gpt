/* Content dispatcher. No site-specific selectors belong here. */
const ext = globalThis.browser ?? globalThis.chrome;

/* Jobs the controller asked to stop. The adapter's poll loop consults this,
 * so cancelling is a cooperative check rather than an attempt to kill a
 * running promise. Ids are kept after the turn ends so a cancel that arrives
 * late is still recorded rather than starting an unstoppable turn. */
const cancelledJobs = new Set();

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
  const task = message.type === "fancy_site_health"
    ? adapter.healthCheck()
    : adapter.executeTurn(
        String(message.prompt ?? ""),
        Number(message.timeoutMs ?? 300000),
        text => ext.runtime.sendMessage({type: "fancy_progress", jobId: message.jobId, text}).catch(() => {}),
        {
          continuing: Boolean(message.continuing),
          isCancelled: () => cancelledJobs.has(String(message.jobId ?? "")),
        },
      );
  Promise.resolve(task)
    .then(result => sendResponse({ok: true, ...result}))
    .catch(error => sendResponse({ok: false, error: String(error?.message ?? error)}));
  return true;
});
