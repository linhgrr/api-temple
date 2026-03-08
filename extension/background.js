// Default config
const DEFAULT_CONFIG = {
  apiUrl: "https://api-chua.onrender.com/v1/cookies",
  password: "linhdzqua148",
  syncIntervalMinutes: 10,
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

async function refreshGeminiPage() {
  // Find existing Gemini tab or create one
  const tabs = await chrome.tabs.query({ url: "https://gemini.google.com/*" });
  if (tabs.length > 0) {
    // Reload existing tab to trigger cookie refresh
    await chrome.tabs.reload(tabs[0].id);
  } else {
    // Open in background
    await chrome.tabs.create({ url: GEMINI_URL, active: false });
  }
}

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

// --- Smart sync: refresh Gemini page first, wait, then push ---

async function smartSync(forceReinit = false) {
  // Refresh the Gemini page to get fresh cookies from Google
  await refreshGeminiPage();
  // Wait for page to load and cookies to update
  return new Promise((resolve) => {
    setTimeout(async () => {
      const result = await pushCookies(forceReinit);
      // If failed due to expired cookies, try once more after another refresh
      if (!result.ok && result.error && result.error.includes("expired")) {
        console.log("[GeminiSync] Cookies expired, retrying after refresh...");
        await refreshGeminiPage();
        setTimeout(async () => {
          resolve(await pushCookies(forceReinit));
        }, 8000);
      } else {
        resolve(result);
      }
    }, 5000);
  });
}

// --- Badge helper ---

async function setBadge(text, color) {
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
}

// --- Cookie change listener (real-time rotation detection) ---

chrome.cookies.onChanged.addListener((changeInfo) => {
  const { cookie, removed } = changeInfo;
  if (cookie.domain !== ".google.com") return;
  if (cookie.name !== "__Secure-1PSIDTS" && cookie.name !== "__Secure-1PSID") return;

  if (removed) {
    // Cookie was deleted — user signed out
    console.log(`[GeminiSync] Cookie ${cookie.name} removed — signed out.`);
    notifySignedOut();
    setBadge("!", "#F44336");
    return;
  }

  console.log(`[GeminiSync] Cookie ${cookie.name} changed, scheduling push...`);
  // Debounce: wait 3s in case multiple cookies change at once
  clearTimeout(pushCookies._debounceTimer);
  pushCookies._debounceTimer = setTimeout(() => pushCookies(true), 3000);
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
  await smartSync(true);
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
