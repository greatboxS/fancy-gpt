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
  document.getElementById("status").textContent = JSON.stringify(status ?? {}, null, 2);
  const badge = document.getElementById("statusBadge");
  badge.classList.remove("connected", "disconnected");
  badge.classList.add(status?.connected ? "connected" : "disconnected");
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
  ext.runtime.sendMessage({type:"fancy_reconnect"}, renderStatus);
};
