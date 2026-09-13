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

function getStatus() {
  return new Promise(resolve => ext.runtime.sendMessage({type: "fancy_status"}, resolve));
}

function setIndicator(state) {
  const spinner = document.getElementById("statusSpinner");
  const badge = document.getElementById("statusBadge");
  const text = document.getElementById("statusText");
  spinner.classList.toggle("active", state === "connecting");
  badge.classList.remove("connected", "disconnected", "failed");
  text.classList.remove("failed");
  badge.classList.add(state === "connecting" ? "disconnected" : state);
  if (state === "failed") text.classList.add("failed");
}

function renderStatus(status) {
  const detail = document.getElementById("statusDetail");
  if (status?.connected) {
    setIndicator("connected");
    document.getElementById("statusText").textContent = "Connected";
    detail.textContent = status.websocket ? "via WebSocket" : status.native ? "via Native Messaging" : "";
  } else {
    setIndicator("disconnected");
    document.getElementById("statusText").textContent = "Disconnected";
    detail.textContent = "";
  }
}

function renderConnecting() {
  setIndicator("connecting");
  document.getElementById("statusText").textContent = "Connecting…";
  document.getElementById("statusDetail").textContent = "";
}

function renderFailed() {
  setIndicator("failed");
  document.getElementById("statusText").textContent = "Connection failed";
  document.getElementById("statusDetail").textContent = "check endpoint, token and that the bridge is running";
  const row = document.getElementById("statusRow");
  row.classList.remove("shake");
  void row.offsetWidth; // restart the animation on repeated failures
  row.classList.add("shake");
}

async function pollUntilConnected(attempts = 10, intervalMs = 400) {
  for (let i = 0; i < attempts; i += 1) {
    const status = await getStatus();
    if (status?.connected) {
      renderStatus(status);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  renderFailed();
}

(async () => {
  const value = await ext.storage.local.get([...ids, ...checkboxIds]);
  for (const id of ids) if (value[id] != null) document.getElementById(id).value = value[id];
  for (const id of checkboxIds) if (value[id] != null) document.getElementById(id).checked = Boolean(value[id]);
  normalizeTunnel();
  renderStatus(await getStatus());
})();

document.getElementById("transport").onchange = normalizeTunnel;
document.getElementById("browserName").onchange = normalizeTunnel;
document.getElementById("save").onclick = async () => {
  normalizeTunnel();
  const value = {};
  for (const id of ids) value[id] = document.getElementById(id).value;
  for (const id of checkboxIds) value[id] = document.getElementById(id).checked;
  await ext.storage.local.set(value);

  const button = document.getElementById("save");
  button.disabled = true;
  renderConnecting();
  try {
    await new Promise(resolve => ext.runtime.sendMessage({type: "fancy_reconnect"}, resolve));
    await pollUntilConnected();
  } finally {
    button.disabled = false;
  }
};
