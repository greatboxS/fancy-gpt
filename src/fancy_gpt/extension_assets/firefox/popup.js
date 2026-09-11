const ext = globalThis.browser ?? globalThis.chrome;
const ids = ["transport", "endpoint", "token", "tunnelId", "browserName"];
const checkboxIds = ["autoConnect"];

function expectedTunnel(browserName, transport) {
  return `${browserName}-extension-${transport === "native" ? "native-local" : "ws-remote"}`;
}

function normalizeTunnel() {
  const transport = document.getElementById("transport").value;
  const browserName = document.getElementById("browserName").value;
  const tunnel = document.getElementById("tunnelId");
  if (!tunnel.value || /^(chrome|edge|firefox)-extension-(native-local|ws-remote)$/.test(tunnel.value)) {
    tunnel.value = expectedTunnel(browserName, transport);
  }
}

function renderStatus(status) {
  const badge = document.getElementById("statusBadge");
  const text = document.getElementById("statusText");
  const detail = document.getElementById("statusDetail");
  badge.classList.remove("connected", "disconnected", "connecting");
  if (status?.connected) {
    badge.classList.add("connected");
    text.textContent = "Connected";
    detail.textContent = status.websocket ? "via WebSocket" : status.native ? "via Native Messaging" : "";
  } else {
    badge.classList.add("disconnected");
    text.textContent = "Disconnected";
    detail.textContent = "";
  }
}

function renderConnecting() {
  const badge = document.getElementById("statusBadge");
  badge.classList.remove("connected", "disconnected");
  badge.classList.add("connecting");
  document.getElementById("statusText").textContent = "Connecting…";
  document.getElementById("statusDetail").textContent = "";
}

(async () => {
  const value = await ext.storage.local.get([...ids, ...checkboxIds]);
  for (const id of ids) if (value[id] != null) document.getElementById(id).value = value[id];
  for (const id of checkboxIds) if (value[id] != null) document.getElementById(id).checked = Boolean(value[id]);
  normalizeTunnel();
  ext.runtime.sendMessage({type:"fancy_status"}, renderStatus);
})();

document.getElementById("transport").onchange = normalizeTunnel;
document.getElementById("browserName").onchange = normalizeTunnel;
document.getElementById("save").onclick = async () => {
  normalizeTunnel();
  const value = {};
  for (const id of ids) value[id] = document.getElementById(id).value;
  for (const id of checkboxIds) value[id] = document.getElementById(id).checked;
  await ext.storage.local.set(value);
  renderConnecting();
  ext.runtime.sendMessage({type:"fancy_reconnect"}, renderStatus);
};
