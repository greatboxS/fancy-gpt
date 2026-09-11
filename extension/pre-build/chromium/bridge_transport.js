/* Extension transport layer. Owns WebSocket and Native Messaging only. */
(() => {
  const ext = globalThis.browser ?? globalThis.chrome;
  let socket = null;
  let nativePort = null;
  let heartbeatTimer = null;
  let reconnectTimer = null;
  let handler = async () => {};

  function scheduleReconnect(connectFn, config) {
    if (reconnectTimer) return;
    if (config.autoConnect === false) return;
    if (!config.token) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectFn().catch(() => scheduleReconnect(connectFn, config));
    }, config.reconnectIntervalMs || 2000);
  }

  function cancelReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function send(message) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
      return;
    }
    if (nativePort) {
      nativePort.postMessage(message);
      return;
    }
    throw new Error("FancyGPT bridge transport is not connected");
  }

  async function connectWebSocket(config, reconnect) {
    socket = new WebSocket(config.endpoint);
    socket.onopen = () => {
      socket.send(JSON.stringify({
        type: "hello",
        protocol: "fancy-browser/1",
        role: "browser",
        token: config.token,
        tunnel_ids: [config.tunnelId],
        browser: config.browserName
      }));
      heartbeatTimer = setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: "heartbeat", ts: Date.now()}));
      }, 20000);
    };
    socket.onmessage = event => handler(typeof event.data === "string" ? JSON.parse(event.data) : event.data).catch(console.error);
    socket.onerror = () => {};
    socket.onclose = () => {
      if (heartbeatTimer) clearInterval(heartbeatTimer);
      heartbeatTimer = null;
      socket = null;
      scheduleReconnect(reconnect, config);
    };
  }

  async function connectNative(config, reconnect) {
    nativePort = ext.runtime.connectNative(config.nativeHost);
    nativePort.onMessage.addListener(message => handler(message).catch(console.error));
    nativePort.onDisconnect.addListener(() => {
      nativePort = null;
      scheduleReconnect(reconnect, config);
    });
  }

  async function connect(config) {
    if (socket || nativePort) return;
    const reconnect = () => connect(config);
    if (config.transport === "native") await connectNative(config, reconnect);
    else await connectWebSocket(config, reconnect);
  }

  function disconnect() {
    cancelReconnect();
    try { socket?.close(); } catch (_) {}
    try { nativePort?.disconnect(); } catch (_) {}
    socket = null;
    nativePort = null;
  }

  function status() {
    return {
      connected: Boolean((socket && socket.readyState === WebSocket.OPEN) || nativePort),
      websocket: Boolean(socket && socket.readyState === WebSocket.OPEN),
      native: Boolean(nativePort)
    };
  }

  globalThis.FancyGPTTransport = {
    connect,
    disconnect,
    send,
    status,
    setHandler(fn) { handler = fn; }
  };
})();
