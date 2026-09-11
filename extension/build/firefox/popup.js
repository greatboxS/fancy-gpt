const ext = globalThis.browser ?? globalThis.chrome;
const ids = ["transport", "endpoint", "token", "tunnelId", "browserName"];

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

(async () => {
  const value = await ext.storage.local.get(ids);
  for (const id of ids) if (value[id] != null) document.getElementById(id).value = value[id];
  normalizeTunnel();
  ext.runtime.sendMessage({type:"fancy_status"}, status => {
    document.getElementById("status").textContent = JSON.stringify(status ?? {}, null, 2);
  });
})();

document.getElementById("transport").onchange = normalizeTunnel;
document.getElementById("browserName").onchange = normalizeTunnel;
document.getElementById("save").onclick = async () => {
  normalizeTunnel();
  const value = {};
  for (const id of ids) value[id] = document.getElementById(id).value;
  await ext.storage.local.set(value);
  ext.runtime.sendMessage({type:"fancy_reconnect"}, status => {
    document.getElementById("status").textContent = JSON.stringify(status ?? {}, null, 2);
  });
};
