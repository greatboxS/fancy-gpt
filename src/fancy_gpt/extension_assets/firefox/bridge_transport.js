/* Extension transport layer. Owns WebSocket and Native Messaging only. */
(() => {
  const ext = globalThis.browser ?? globalThis.chrome;
  let socket = null;
  let nativePort = null;
  let heartbeatTimer = null;
  let reconnectTimer = null;
  let handler = async () => {};
  let generation = 0;
  let connectedGeneration = 0;
  let activeConfig = null;

  function clearHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }

  // Reconnect stays opt-out (popup "auto connect") and never retries without a
  // token; the generation guard keeps a stale attempt from reviving a socket
  // that a newer connect()/disconnect() already replaced.
  function scheduleReconnect(connectFn, ownerGeneration) {
    if (ownerGeneration !== generation || reconnectTimer) return;
    if (activeConfig?.autoConnect === false) return;
    if (!activeConfig?.token) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (ownerGeneration !== generation) return;
      connectFn().catch(() => scheduleReconnect(connectFn, ownerGeneration));
    }, activeConfig?.reconnectIntervalMs || 2000);
  }

  function send(message) {
    if (socket && socket.readyState === WebSocket.OPEN && connectedGeneration === generation) {
      socket.send(JSON.stringify(message));
      return;
    }
    if (nativePort && connectedGeneration === generation) {
      nativePort.postMessage(message);
      return;
    }
    throw new Error("FancyGPT bridge transport is not connected");
  }

  async function connectWebSocket(config, ownerGeneration, reconnect) {
    await new Promise((resolve, reject) => {
      const ws = new WebSocket(config.endpoint);
      socket = ws;
      let acknowledged = false;
      const fail = error => {
        if (!acknowledged) reject(error instanceof Error ? error : new Error(String(error)));
      };
      ws.onopen = () => {
        if (ownerGeneration !== generation) { ws.close(); return; }
        ws.send(JSON.stringify({
          type: "hello",
          protocol: "fancy-browser/1",
          role: "browser",
          token: config.token,
          tunnel_ids: config.tunnelIds ?? [config.tunnelId],
          browser: config.browserName
        }));
      };
      ws.onmessage = event => {
        const message = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
        if (!acknowledged && message?.type === "hello_ack") {
          acknowledged = true;
          connectedGeneration = ownerGeneration;
          clearHeartbeat();
          heartbeatTimer = setInterval(() => {
            if (ownerGeneration === generation && ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({type: "heartbeat", ts: Date.now()}));
            }
          }, 20000);
          resolve();
          return;
        }
        handler(message).catch(console.error);
      };
      ws.onerror = () => fail(new Error("FancyGPT WebSocket connection failed"));
      ws.onclose = () => {
        if (ownerGeneration !== generation || socket !== ws) return;
        clearHeartbeat();
        socket = null;
        connectedGeneration = 0;
        scheduleReconnect(reconnect, ownerGeneration);
        fail(new Error("FancyGPT WebSocket closed before bridge acknowledgement"));
      };
    });
  }

  async function connectNative(config, ownerGeneration, reconnect) {
    const port = ext.runtime.connectNative(config.nativeHost);
    nativePort = port;
    connectedGeneration = ownerGeneration;
    clearHeartbeat();
    heartbeatTimer = setInterval(() => {
      if (ownerGeneration === generation && nativePort === port) {
        try { port.postMessage({type: "heartbeat", ts: Date.now()}); } catch (_) {}
      }
    }, 20000);
    port.onMessage.addListener(message => handler(message).catch(console.error));
    port.onDisconnect.addListener(() => {
      if (ownerGeneration !== generation || nativePort !== port) return;
      clearHeartbeat();
      nativePort = null;
      connectedGeneration = 0;
      scheduleReconnect(reconnect, ownerGeneration);
    });
  }

  async function connect(config) {
    if ((socket || nativePort) && connectedGeneration === generation) return;
    activeConfig = config;
    generation += 1;
    const ownerGeneration = generation;
    const reconnect = () => {
      if (ownerGeneration !== generation) return Promise.resolve();
      return config.transport === "native"
        ? connectNative(config, ownerGeneration, reconnect)
        : connectWebSocket(config, ownerGeneration, reconnect);
    };
    await reconnect();
  }

  function disconnect() {
    generation += 1;
    connectedGeneration = 0;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    clearHeartbeat();
    const ws = socket;
    const port = nativePort;
    socket = null;
    nativePort = null;
    try { ws?.close(); } catch (_) {}
    try { port?.disconnect(); } catch (_) {}
  }

  function status() {
    return {
      connected: Boolean(connectedGeneration === generation && ((socket && socket.readyState === WebSocket.OPEN) || nativePort)),
      websocket: Boolean(connectedGeneration === generation && socket && socket.readyState === WebSocket.OPEN),
      native: Boolean(connectedGeneration === generation && nativePort),
      generation
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
