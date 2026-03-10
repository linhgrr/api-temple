// Default config
const DEFAULT_CONFIG = {
  apiUrl: "https://api-chua.onrender.com/v1/cookies",
  password: "linhdzqua148",
  syncIntervalMinutes: 10,
  pullIntervalMinutes: 5,
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

// --- API push (browser → server) ---

async function pushCookies(forceReinit = false) {
  const config = await chrome.storage.local.get(DEFAULT_CONFIG);
  const cookies = await getGeminiCookies();

  if (!cookies["__Secure-1PSID"] || !cookies["__Secure-1PSIDTS"]) {
    console.log("[GeminiSync] Cookies not found — user likely signed out.");
    await setBadge("!", "#F44336");
    notifySignedOut();
    return { ok: false, error: "Signed out — please sign in to Gemini" };
  }

  const prev = await chrome.storage.local.get(["lastPushedPSID", "lastPushedPSIDTS"]);
  const psidChanged = cookies["__Secure-1PSID"] !== prev.lastPushedPSID;
  const psidtsChanged = cookies["__Secure-1PSIDTS"] !== prev.lastPushedPSIDTS;

  if (!psidChanged && !psidtsChanged && !forceReinit) {
    console.log("[GeminiSync] Cookies unchanged, skipping push.");
    await setBadge("✓", "#4CAF50");
    return { ok: true, skipped: true };
  }

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
      console.log("[GeminiSync] Cookies pushed successfully.");
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

// --- API pull (server → browser) ---
// Fetches the server's current cookies and writes them back to the browser
// if the server has a newer __Secure-1PSIDTS (e.g. from another sync source).

async function pullAndWriteBack() {
  const config = await chrome.storage.local.get(DEFAULT_CONFIG);

  try {
    const resp = await fetch(config.apiUrl, {
      method: "GET",
      headers: {
        Accept: "application/json",
        "X-Password": config.password,
      },
    });

    if (!resp.ok) {
      console.log(`[GeminiSync:pull] Server returned ${resp.status}, skipping.`);
      return;
    }

    const data = await resp.json();
    if (!data.secure_1psid || !data.secure_1psidts) {
      console.log("[GeminiSync:pull] Server has no cookies configured.");
      return;
    }

    const browserCookies = await getGeminiCookies();
    const browserPSIDTS = browserCookies["__Secure-1PSIDTS"] || "";
    const browserPSID = browserCookies["__Secure-1PSID"] || "";

    // Only write back if the server has different cookies
    const psidDiff = data.secure_1psid !== browserPSID;
    const psidtsDiff = data.secure_1psidts !== browserPSIDTS;

    if (!psidDiff && !psidtsDiff) {
      console.log("[GeminiSync:pull] Browser and server cookies match.");
      return;
    }

    console.log("[GeminiSync:pull] Server has different cookies, writing back to browser...");

    // Suppress onChanged events to avoid feedback loop
    _suppressCookieEvents = true;
    setTimeout(() => { _suppressCookieEvents = false; }, 15000);

    if (psidtsDiff && data.secure_1psidts) {
      await chrome.cookies.set({
        url: GEMINI_URL,
        name: "__Secure-1PSIDTS",
        value: data.secure_1psidts,
        domain: ".google.com",
        path: "/",
        secure: true,
        httpOnly: true,
        sameSite: "no_restriction",
        expirationDate: Math.floor(Date.now() / 1000) + 365 * 86400,
      });
      console.log("[GeminiSync:pull] __Secure-1PSIDTS written back to browser.");
    }

    if (psidDiff && data.secure_1psid) {
      await chrome.cookies.set({
        url: GEMINI_URL,
        name: "__Secure-1PSID",
        value: data.secure_1psid,
        domain: ".google.com",
        path: "/",
        secure: true,
        httpOnly: true,
        sameSite: "no_restriction",
        expirationDate: Math.floor(Date.now() / 1000) + 365 * 86400,
      });
      console.log("[GeminiSync:pull] __Secure-1PSID written back to browser.");
    }

    // Update local tracking to avoid re-pushing what we just pulled
    await chrome.storage.local.set({
      lastPushedPSID: data.secure_1psid,
      lastPushedPSIDTS: data.secure_1psidts,
    });

    await setBadge("⇅", "#2196F3");
    setTimeout(() => setBadge("✓", "#4CAF50"), 3000);
  } catch (e) {
    console.log(`[GeminiSync:pull] Error: ${e.message}`);
  }
}

// --- Smart sync: push then pull ---

async function smartSync(forceReinit = false) {
  const result = await pushCookies(forceReinit);

  if (!result.ok && result.error && result.error.includes("Signed out")) {
    // Browser has no cookies — try pulling from server to restore them
    console.log("[GeminiSync] Browser signed out, attempting pull from server...");
    await pullAndWriteBack();
    return result;
  }

  // After a successful push, pull to check if server has newer cookies
  if (result.ok && !result.skipped) {
    setTimeout(() => pullAndWriteBack(), 5000);
  }

  return result;
}

// --- Badge helper ---

async function setBadge(text, color) {
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
}

// --- Cookie change listener (real-time rotation detection) ---

let _suppressCookieEvents = false;

chrome.cookies.onChanged.addListener((changeInfo) => {
  const { cookie, removed, cause } = changeInfo;
  if (cookie.domain !== ".google.com") return;
  if (cookie.name !== "__Secure-1PSIDTS" && cookie.name !== "__Secure-1PSID") return;

  if (removed && cause === "explicit") {
    console.log(`[GeminiSync] Cookie ${cookie.name} explicitly removed — signed out.`);
    notifySignedOut();
    setBadge("!", "#F44336");
    return;
  }

  if (removed) return;

  if (_suppressCookieEvents) {
    console.log(`[GeminiSync] Cookie ${cookie.name} changed (suppressed, avoiding loop).`);
    return;
  }

  console.log(`[GeminiSync] Cookie ${cookie.name} changed, scheduling push...`);
  clearTimeout(pushCookies._debounceTimer);
  pushCookies._debounceTimer = setTimeout(() => {
    pushCookies(false);
  }, 10000);
});
pushCookies._debounceTimer = null;

// --- Alarms ---

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "sync-cookies") {
    await smartSync();
  }
  if (alarm.name === "pull-cookies") {
    await pullAndWriteBack();
  }
});

async function setupAlarms() {
  const config = await chrome.storage.local.get(DEFAULT_CONFIG);

  await chrome.alarms.clear("sync-cookies");
  chrome.alarms.create("sync-cookies", {
    periodInMinutes: config.syncIntervalMinutes,
  });

  // Pull alarm runs more frequently to catch server-side cookie updates quickly
  await chrome.alarms.clear("pull-cookies");
  chrome.alarms.create("pull-cookies", {
    periodInMinutes: config.pullIntervalMinutes,
  });
}

// --- Startup ---

chrome.runtime.onStartup.addListener(async () => {
  await setupAlarms();
  setTimeout(() => smartSync(true), 30000);
});

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.set(DEFAULT_CONFIG);
  await setupAlarms();
  await setBadge("…", "#9E9E9E");
});

// --- Message handler for popup ---

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.action === "sync-now") {
    smartSync(true).then(sendResponse);
    return true;
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
