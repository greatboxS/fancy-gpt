/* Extension runtime layer. Owns tab lifecycle and delegates transport/site work. */
if (typeof importScripts === "function" && !globalThis.FancyGPTTransport) {
  importScripts("bridge_transport.js");
}
const ext = globalThis.browser ?? globalThis.chrome;
const DEFAULTS = {
  transport: "websocket",
  endpoint: "ws://127.0.0.1:8765",
  token: "",
  tunnelId: "chrome-extension-ws-remote",
  browserName: "chrome",
  nativeHost: "com.fancygpt.bridge"
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

function conversationTarget(conversation) {
  const mode = conversation?.mode ?? "fresh";
  const binding = conversation?.binding ?? null;
  if (!["fresh", "resume", "fork"].includes(mode)) throw new Error(`unsupported conversation mode: ${mode}`);
  if (mode === "resume") {
    if (!binding) throw new Error("resume conversation requires a logical or ChatGPT binding");
    return String(binding).startsWith("https://chatgpt.com/") ? String(binding) : "https://chatgpt.com/";
  }
  if (mode === "fork") return "https://chatgpt.com/";
  return "https://chatgpt.com/?temporary-chat=true";
}

async function captureConversationBinding(tabId, fallback = null) {
  for (let i = 0; i < 20; ++i) {
    try {
      const current = await ext.tabs.get(tabId);
      const url = current?.url ?? "";
      if (url.startsWith("https://chatgpt.com/") && !url.includes("temporary-chat=true")) return url;
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return fallback;
}

async function executeJob(job) {
  let tab = null;
  try {
    if (!["model.turn", "site.health"].includes(job.operation)) throw new Error(`unsupported operation: ${job.operation}`);
    if (job.site !== "chatgpt") throw new Error(`unsupported site: ${job.site}`);
    const targetUrl = conversationTarget(job.conversation);
    tab = await ext.tabs.create({url: targetUrl, active: false});
    if (!tab || tab.id == null) throw new Error("failed to create site task tab");
    if (job.operation === "site.health") {
      const health = await sendToContent(tab.id, {type: "fancy_site_health", site: job.site});
      const payload = health ?? {ok: false, reason: "site-health-no-response"};
      globalThis.FancyGPTTransport.send({
        type: "job_result", job_id: job.job_id, text: JSON.stringify(payload),
        response_identity: "site-health"
      });
      return;
    }
    const result = await sendToContent(tab.id, {
      type: "fancy_execute_turn",
      site: job.site,
      prompt: job.prompt,
      jobId: job.job_id,
      timeoutMs: Math.max(1000, Math.floor((job.timeout_s ?? 300) * 1000))
    });
    if (!result || !result.ok) throw new Error(result?.error ?? "site content adapter failed");
    const binding = await captureConversationBinding(tab.id, job.conversation?.binding ?? null);
    globalThis.FancyGPTTransport.send({
      type: "job_result",
      job_id: job.job_id,
      text: result.text,
      assistant_turn_id: result.responseIdentity,
      response_identity: result.responseIdentity,
      conversation_binding: binding
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
});

connectBridge().catch(console.error);
