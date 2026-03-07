// Default config
const DEFAULT_CONFIG = {
  apiUrl: "https://api-chua.onrender.com/v1/cookies",
  password: "linhdzqua148",
  syncIntervalMinutes: 10,
};

const GEMINI_URL = "https://gemini.google.com";
const COOKIE_DOMAIN = ".google.com";

// --- Cookie extraction ---

async function getGeminiCookies() {
  const names = ["__Secure-1PSID", "__Secure-1PSIDTS"];
  const cookies = {};

  for (const name of names) {
    const cookie = await chrome.cookies.get({ url: GEMINI_URL, name });
    if (cookie) cookies[name] = cookie.value;
  }

  return cookies;
}

// --- API push ---

async function pushCookies() {
  const config = await chrome.storage.local.get(DEFAULT_CONFIG);
  const cookies = await getGeminiCookies();

  if (!cookies["__Secure-1PSID"] || !cookies["__Secure-1PSIDTS"]) {
    console.log("[GeminiSync] Cookies not found, skipping push.");
    await setBadge("!", "#F44336");
    return { ok: false, error: "Cookies not found in browser" };
  }

  try {
    const resp = await fetch(config.apiUrl, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-Password": config.password,
      },
      body: JSON.stringify({
        secure_1psid: cookies["__Secure-1PSID"],
        secure_1psidts: cookies["__Secure-1PSIDTS"],
        reinitialize: true,
      }),
    });

    const data = await resp.json();

    if (resp.ok && data.gemini_connected) {
      console.log("[GeminiSync] Cookies synced successfully.");
      await setBadge("✓", "#4CAF50");
      await chrome.storage.local.set({ lastSync: Date.now(), lastError: null });
      return { ok: true, data };
    } else {
      const err = data.message || data.detail || resp.statusText;
      console.error("[GeminiSync] Server rejected:", err);
      await setBadge("!", "#FF9800");
      await chrome.storage.local.set({ lastError: err });
      return { ok: false, error: err };
    }
  } catch (e) {
    console.error("[GeminiSync] Network error:", e.message);
    await setBadge("✗", "#F44336");
    await chrome.storage.local.set({ lastError: e.message });
    return { ok: false, error: e.message };
  }
}

// --- Badge helper ---

async function setBadge(text, color) {
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
}

// --- Alarms (periodic sync) ---

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "sync-cookies") {
    await pushCookies();
  }
});

async function setupAlarm() {
  const config = await chrome.storage.local.get(DEFAULT_CONFIG);
  await chrome.alarms.clear("sync-cookies");
  chrome.alarms.create("sync-cookies", {
    periodInMinutes: config.syncIntervalMinutes,
  });
}

// --- Startup: open Gemini tab (background) + first sync ---

chrome.runtime.onStartup.addListener(async () => {
  // Open Gemini in background so cookies get refreshed
  chrome.tabs.create({ url: GEMINI_URL, active: false });

  // Wait a few seconds for cookies to settle, then sync
  setTimeout(() => pushCookies(), 5000);
  await setupAlarm();
});

// Extension installed / updated
chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.set(DEFAULT_CONFIG);
  await setupAlarm();
  await setBadge("…", "#9E9E9E");
});

// --- Message handler for popup ---

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.action === "sync-now") {
    pushCookies().then(sendResponse);
    return true; // keep channel open for async response
  }
  if (msg.action === "get-status") {
    (async () => {
      const cookies = await getGeminiCookies();
      const store = await chrome.storage.local.get(["lastSync", "lastError"]);
      sendResponse({
        hasCookies: !!(cookies["__Secure-1PSID"] && cookies["__Secure-1PSIDTS"]),
        psidPreview: cookies["__Secure-1PSID"]
          ? cookies["__Secure-1PSID"].slice(0, 12) + "..."
          : "—",
        lastSync: store.lastSync || null,
        lastError: store.lastError || null,
      });
    })();
    return true;
  }
});
