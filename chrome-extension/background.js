const DEFAULT_SERVER = "http://127.0.0.1:5000";
const DEFAULT_START_URL = "";
const DOWNLOADABLE_HINT = "OpenKTV: click to queue this page";
const UNSUPPORTED_HINT = "OpenKTV: no downloadable media detected on this page";

const ICON_RED = {
  16: "icon_red16.png",
  32: "icon_red32.png",
  48: "icon_red48.png",
  128: "icon_red128.png",
};

const ICON_GRAY = {
  16: "icon_gray16.png",
  32: "icon_gray32.png",
  48: "icon_gray48.png",
  128: "icon_gray128.png",
};

async function getServerBase() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(["ktvServerBase"], (result) => {
      const raw = (result.ktvServerBase || DEFAULT_SERVER).trim();
      resolve(raw.replace(/\/+$/, ""));
    });
  });
}

async function getStartUrl(serverBase) {
  return new Promise((resolve) => {
    chrome.storage.sync.get(["ktvStartUrl"], (result) => {
      const configured = String(result.ktvStartUrl || DEFAULT_START_URL).trim();
      if (configured) {
        resolve(configured);
        return;
      }
      resolve(`${serverBase}/admin`);
    });
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function createRequestId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `req-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

async function postAddSong(endpoint, payload) {
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await response.json().catch(() => null);
    const queuedCount = data && Number.isFinite(Number(data.queued)) ? Number(data.queued) : null;
    const explicitFailure = data && Object.prototype.hasOwnProperty.call(data, "ok") && data.ok === false;
    const successByPayload = !!(data && (data.ok === true || queuedCount !== null));

    if ((!response.ok && !successByPayload) || explicitFailure) {
      const message = (data && data.error) ? data.error : `HTTP ${response.status}`;
      return { ok: false, reason: message, queuedCount: null, retryable: response.status >= 500 || response.status === 0 };
    }

    return { ok: true, reason: "", queuedCount, retryable: false };
  } catch (error) {
    return {
      ok: false,
      reason: error && error.message ? error.message : "Network error",
      queuedCount: null,
      retryable: true,
    };
  }
}

async function autoStartAndRetry(serverBase, endpoint, payload) {
  const startUrl = await getStartUrl(serverBase);
  let opened = false;
  try {
    await chrome.tabs.create({ url: startUrl, active: true });
    opened = true;
  } catch {
    // fallback: try opening with window API if tab creation is blocked
    try {
      await chrome.windows.create({ url: startUrl, focused: true, type: "popup", width: 980, height: 760 });
      opened = true;
    } catch {
      // ignore; retry loop still runs
    }
  }

  if (opened) {
    notify("Starting KTV", "Start URL opened, waiting for server...");
  }

  const delays = [1500, 3000, 5000, 6000];
  for (const delay of delays) {
    await sleep(delay);
    const attempt = await postAddSong(endpoint, payload);
    if (attempt.ok) {
      return { ok: true, queuedCount: attempt.queuedCount };
    }
  }
  return { ok: false };
}

function notify(title, message, ok = true) {
  const text = (title || "") + (message ? `: ${message}` : "");
  try {
    chrome.action.setTitle({ title: text.slice(0, 180) });
    chrome.action.setBadgeBackgroundColor({ color: ok ? "#1e7d66" : "#b42318" });
    chrome.action.setBadgeText({ text: ok ? "OK" : "ERR" });
  } catch {
    // Ignore action UI errors to avoid breaking queue flow.
  }
  setTimeout(() => {
    try {
      chrome.action.setBadgeText({ text: "" });
    } catch {
      // ignore
    }
  }, 1800);
}

function isDownloadableUrl(rawUrl) {
  if (!rawUrl || !/^https?:\/\//i.test(rawUrl)) {
    return false;
  }

  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return false;
  }

  const host = (parsed.hostname || "").toLowerCase();
  const path = (parsed.pathname || "").toLowerCase();
  const hasWatchId = !!parsed.searchParams.get("v");

  // Keep this strict so the action is only enabled on likely downloadable media pages.
  if (host === "youtu.be") {
    return path.length > 1;
  }
  if (host.endsWith("youtube.com") || host === "m.youtube.com" || host === "music.youtube.com") {
    return (path === "/watch" && hasWatchId) || path.startsWith("/shorts/") || path.startsWith("/live/");
  }
  if (host.endsWith("bilibili.com")) {
    return path.startsWith("/video/") || path.startsWith("/bangumi/play/");
  }

  return false;
}

async function updateActionForTab(tab) {
  if (!tab || typeof tab.id !== "number") {
    return;
  }
  const enabled = isDownloadableUrl(tab.url || "");
  try {
    if (enabled) {
      await chrome.action.enable(tab.id);
      await chrome.action.setIcon({ tabId: tab.id, path: ICON_RED });
      await chrome.action.setTitle({ tabId: tab.id, title: DOWNLOADABLE_HINT });
      return;
    }

    await chrome.action.disable(tab.id);
    await chrome.action.setIcon({ tabId: tab.id, path: ICON_GRAY });
    await chrome.action.setTitle({ tabId: tab.id, title: UNSUPPORTED_HINT });
  } catch {
    // Some Chromium builds may reject enable/disable on special tabs.
    // Fallback: keep action available and only change title.
    try {
      await chrome.action.setIcon({ tabId: tab.id, path: enabled ? ICON_RED : ICON_GRAY });
      await chrome.action.setTitle({ tabId: tab.id, title: enabled ? DOWNLOADABLE_HINT : UNSUPPORTED_HINT });
    } catch {
      // ignore
    }
  }
}

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs.length ? tabs[0] : null;
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.sync.get(["ktvServerBase", "ktvStartUrl"], (result) => {
    if (!result.ktvServerBase) {
      chrome.storage.sync.set({ ktvServerBase: DEFAULT_SERVER });
    }
    if (typeof result.ktvStartUrl === "undefined") {
      chrome.storage.sync.set({ ktvStartUrl: DEFAULT_START_URL });
    }
  });

  chrome.tabs.query({}, (tabs) => {
    (tabs || []).forEach((tab) => {
      updateActionForTab(tab);
    });
  });
});

chrome.runtime.onStartup.addListener(() => {
  chrome.tabs.query({}, (tabs) => {
    (tabs || []).forEach((tab) => {
      updateActionForTab(tab);
    });
  });
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    await updateActionForTab(tab);
  } catch {
    // Ignore transient tab races.
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url || changeInfo.status === "complete") {
    updateActionForTab(tab || { id: tabId, url: changeInfo.url || "" });
  }
});

chrome.action.onClicked.addListener(async () => {
  try {
    const tab = await getActiveTab();
    if (!tab || !tab.url) {
      notify("OpenKTV", "No active tab URL found");
      return;
    }

    if (!isDownloadableUrl(tab.url)) {
      notify("OpenKTV", "No downloadable media detected", false);
      return;
    }

    const serverBase = await getServerBase();
    const endpoint = `${serverBase}/api/extension/add-song`;

    const payload = {
      url: tab.url,
      title: tab.title || "",
      request_id: createRequestId(),
    };

    const attempt = await postAddSong(endpoint, payload);
    if (!attempt.ok) {
      if (attempt.retryable) {
        notify("Starting KTV", "Server unavailable, launching and retrying...");
        const retried = await autoStartAndRetry(serverBase, endpoint, payload);
        if (!retried.ok) {
          notify("KTV Not Ready", "Opened start URL but server still unavailable", false);
          return;
        }
        const queuedHintRetry = retried.queuedCount !== null ? `queued ${retried.queuedCount}` : "queued";
        notify("Queued", `${tab.title || "auto-detected metadata"} (${queuedHintRetry})`, true);
        return;
      }

      notify("Add Failed", attempt.reason || "Unknown error", false);
      return;
    }

    const queuedHint = attempt.queuedCount !== null ? `queued ${attempt.queuedCount}` : "queued";
    notify("Queued", `${tab.title || "auto-detected metadata"} (${queuedHint})`, true);
  } catch (error) {
    notify("OpenKTV Error", error && error.message ? error.message : "Unknown error", false);
  }
});
