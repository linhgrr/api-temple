// Default config
const DEFAULT_CONFIG = {
  apiUrl: "https://api-chua.onrender.com/v1/cookies",
  password: "linhdzqua148",
  syncIntervalMinutes: 60,
};

const GEMINI_URL = "https://gemini.google.com";

// --- Notifications ---

function notifySignedOut() {
  chrome.notifications.create("gemini-signout", {
    type: "basic",
    iconUrl: "icon.png",
    title: "Gemini Sign-In Required",
    message: "You have been signed out of Gemini. Click to sign in again.",
    priority: 2,
    requireInteraction: true,
  });
}

chrome.notifications.onClicked.addListener((id) => {
  if (id === "gemini-signout") {
    chrome.tabs.create({ url: GEMINI_URL, active: true });
    chrome.notifications.clear(id);
  }
});

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

// --- Refresh cookies by visiting Gemini ---
// NOTE: Removed automatic page reloading. chrome.cookies.get() reads cookies
// directly from the cookie store — no page load needed. Frequent reloads
// trigger Google's bot detection and cause account sign-outs.

// --- API push ---

async function pushCookies(forceReinit = false) {
  const config = await chrome.storage.local.get(DEFAULT_CONFIG);
  const cookies = await getGeminiCookies();

  if (!cookies["__Secure-1PSID"] || !cookies["__Secure-1PSIDTS"]) {
    console.log("[GeminiSync] Cookies not found — user likely signed out.");
    await setBadge("!", "#F44336");
    notifySignedOut();
    return { ok: false, error: "Signed out — please sign in to Gemini" };
  }

  // Check if cookies actually changed since last successful sync
  const prev = await chrome.storage.local.get(["lastPushedPSID", "lastPushedPSIDTS"]);
  const psidChanged = cookies["__Secure-1PSID"] !== prev.lastPushedPSID;
  const psidtsChanged = cookies["__Secure-1PSIDTS"] !== prev.lastPushedPSIDTS;

  if (!psidChanged && !psidtsChanged && !forceReinit) {
    console.log("[GeminiSync] Cookies unchanged, skipping push.");
    await setBadge("✓", "#4CAF50");
    return { ok: true, skipped: true };
  }

  // Suppress cookie change events briefly to avoid feedback loop
  _suppressCookieEvents = true;
  setTimeout(() => { _suppressCookieEvents = false; }, 15000);

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
        reinitialize: forceReinit,
      }),
    });

    const data = await resp.json();

    if (resp.ok && (data.gemini_connected || data.success)) {
      console.log("[GeminiSync] Cookies synced successfully.");
      await setBadge("✓", "#4CAF50");
      await chrome.storage.local.set({
        lastSync: Date.now(),
        lastError: null,
        lastPushedPSID: cookies["__Secure-1PSID"],
        lastPushedPSIDTS: cookies["__Secure-1PSIDTS"],
      });
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

// --- Smart sync: read cookies directly and push ---

async function smartSync(forceReinit = false) {
  // Read cookies directly from cookie store — no page reload needed
  const result = await pushCookies(forceReinit);
  if (!result.ok && result.error && result.error.includes("Signed out")) {
    console.log("[GeminiSync] User signed out of Gemini.");
    notifySignedOut();
  }
  return result;
}

// --- Badge helper ---

async function setBadge(text, color) {
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
}

// --- Cookie change listener (real-time rotation detection) ---

// Flag to suppress onChanged events right after a push (avoids feedback loop)
let _suppressCookieEvents = false;

chrome.cookies.onChanged.addListener((changeInfo) => {
  const { cookie, removed, cause } = changeInfo;
  if (cookie.domain !== ".google.com") return;
  if (cookie.name !== "__Secure-1PSIDTS" && cookie.name !== "__Secure-1PSID") return;

  if (removed && cause === "explicit") {
    // Cookie was explicitly deleted — user signed out
    console.log(`[GeminiSync] Cookie ${cookie.name} explicitly removed — signed out.`);
    notifySignedOut();
    setBadge("!", "#F44336");
    return;
  }

  if (removed) {
    // Cookie removed due to expiry or overwrite — ignore, new one should follow
    return;
  }

  if (_suppressCookieEvents) {
    console.log(`[GeminiSync] Cookie ${cookie.name} changed (suppressed, avoiding loop).`);
    return;
  }

  console.log(`[GeminiSync] Cookie ${cookie.name} changed, scheduling push...`);
  // Debounce: wait 10s to batch multiple cookie changes and avoid rapid pushes
  clearTimeout(pushCookies._debounceTimer);
  pushCookies._debounceTimer = setTimeout(() => {
    // Don't force reinit — only push if values actually changed (handled inside pushCookies)
    pushCookies(false);
  }, 10000);
});
pushCookies._debounceTimer = null;

// --- Alarms (periodic sync as fallback) ---

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "sync-cookies") {
    await smartSync();
  }
});

async function setupAlarm() {
  const config = await chrome.storage.local.get(DEFAULT_CONFIG);
  await chrome.alarms.clear("sync-cookies");
  chrome.alarms.create("sync-cookies", {
    periodInMinutes: config.syncIntervalMinutes,
  });
}

// --- Startup: open Gemini tab + first sync ---

chrome.runtime.onStartup.addListener(async () => {
  await setupAlarm();
  // Delay first sync by 30s to let browser fully start and cookies stabilize
  setTimeout(() => smartSync(true), 30000);
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
    smartSync(true).then(sendResponse);
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
