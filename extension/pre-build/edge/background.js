/* Extension runtime layer. Owns tab lifecycle and delegates transport/site work. */
if (typeof importScripts === "function" && !globalThis.FancyGPTTransport) {
  importScripts("bridge_transport.js");
}
const ext = globalThis.browser ?? globalThis.chrome;
const DEFAULTS = {
  transport: "websocket",
  endpoint: "ws://127.0.0.1:8765",
  token: "",
  tunnelId: "edge-extension-ws-remote",
  browserName: "edge",
  nativeHost: "com.fancygpt.bridge",
  autoConnect: true,
  reconnectIntervalMs: 2000
};

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

async function executeJob(job) {
  let tab = null;
  try {
    if (job.operation !== "model.turn") throw new Error(`unsupported operation: ${job.operation}`);
    if (job.site !== "chatgpt") throw new Error(`unsupported site: ${job.site}`);
    tab = await ext.tabs.create({url: "https://chatgpt.com/?temporary-chat=true", active: false});
    if (!tab || tab.id == null) throw new Error("failed to create site task tab");
    const result = await sendToContent(tab.id, {
      type: "fancy_execute_turn",
      site: job.site,
      prompt: job.prompt,
      jobId: job.job_id,
      timeoutMs: Math.max(1000, Math.floor((job.timeout_s ?? 300) * 1000))
    });
    if (!result || !result.ok) throw new Error(result?.error ?? "site content adapter failed");
    globalThis.FancyGPTTransport.send({
      type: "job_result",
      job_id: job.job_id,
      text: result.text,
      assistant_turn_id: result.responseIdentity,
      response_identity: result.responseIdentity
    });
  } catch (error) {
    globalThis.FancyGPTTransport.send({type: "job_error", job_id: job.job_id, error: String(error?.message ?? error)});
  } finally {
    if (tab?.id != null) try { await ext.tabs.remove(tab.id); } catch (_) {}
  }
}

globalThis.FancyGPTTransport.setHandler(async message => {
  if (message.type === "job") await executeJob(message);
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
