const DEFAULT_SERVER = "http://127.0.0.1:5000";
const DEFAULT_START_URL = "";

function normalizeBase(value) {
  const raw = (value || "").trim();
  if (!raw) return DEFAULT_SERVER;
  return raw.replace(/\/+$/, "");
}

function setStatus(text, isError = false) {
  const el = document.getElementById("status");
  el.textContent = text || "";
  el.style.color = isError ? "#b42318" : "#0b6b56";
}

function loadSettings() {
  chrome.storage.sync.get(["ktvServerBase", "ktvStartUrl"], (result) => {
    document.getElementById("serverBase").value = result.ktvServerBase || DEFAULT_SERVER;
    document.getElementById("startUrl").value = result.ktvStartUrl || DEFAULT_START_URL;
  });
}

function saveSettings() {
  const input = document.getElementById("serverBase");
  const startInput = document.getElementById("startUrl");
  const normalized = normalizeBase(input.value);
  const startUrl = String(startInput.value || "").trim();
  if (!/^https?:\/\//i.test(normalized)) {
    setStatus("Please use http:// or https://", true);
    return;
  }

  if (startUrl && !/^(https?:\/\/|[a-zA-Z][a-zA-Z0-9+.-]*:\/\/)/.test(startUrl)) {
    setStatus("Start URL must be http(s):// or custom-protocol://", true);
    return;
  }

  chrome.storage.sync.set({ ktvServerBase: normalized, ktvStartUrl: startUrl }, () => {
    setStatus(`Saved: ${normalized}${startUrl ? ` | start: ${startUrl}` : ""}`);
  });
}

document.getElementById("saveBtn").addEventListener("click", saveSettings);
loadSettings();
