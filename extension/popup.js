const $ = (s) => document.querySelector(s);

function timeAgo(ts) {
  if (!ts) return "never";
  const sec = Math.floor((Date.now() - ts) / 1000);
  if (sec < 60) return sec + "s ago";
  if (sec < 3600) return Math.floor(sec / 60) + "m ago";
  return Math.floor(sec / 3600) + "h ago";
}

// Load status
chrome.runtime.sendMessage({ action: "get-status" }, (s) => {
  $("#status").innerHTML = `
    <div><span class="label">Cookies:</span> <span class="value">${s.hasCookies ? "✓ Found" : "✗ Not found"}</span></div>
    <div><span class="label">1PSID:</span> <span class="value">${s.psidPreview}</span></div>
    <div><span class="label">Last sync:</span> <span class="value">${timeAgo(s.lastSync)}</span></div>
    ${s.lastError ? `<div><span class="label">Error:</span> <span class="error">${s.lastError}</span></div>` : ""}
  `;
});

// Load settings
chrome.storage.local.get(["apiUrl", "password", "syncIntervalMinutes", "pullIntervalMinutes"], (c) => {
  $("#apiUrl").value = c.apiUrl || "";
  $("#password").value = c.password || "";
  $("#interval").value = c.syncIntervalMinutes || 10;
  $("#pullInterval").value = c.pullIntervalMinutes || 5;
});

// Sync button
$("#syncBtn").addEventListener("click", () => {
  $("#syncBtn").disabled = true;
  $("#syncBtn").textContent = "Syncing...";
  chrome.runtime.sendMessage({ action: "sync-now" }, (res) => {
    $("#syncBtn").disabled = false;
    $("#syncBtn").textContent = res.ok ? "✓ Synced!" : "✗ Failed";
    setTimeout(() => { $("#syncBtn").textContent = "⚡ Sync Now"; }, 2000);
    chrome.runtime.sendMessage({ action: "get-status" }, (s) => {
      $("#status").innerHTML = `
        <div><span class="label">Cookies:</span> <span class="value">${s.hasCookies ? "✓ Found" : "✗ Not found"}</span></div>
        <div><span class="label">1PSID:</span> <span class="value">${s.psidPreview}</span></div>
        <div><span class="label">Last sync:</span> <span class="value">${timeAgo(s.lastSync)}</span></div>
        ${s.lastError ? `<div><span class="label">Error:</span> <span class="error">${s.lastError}</span></div>` : ""}
      `;
    });
  });
});

// Save button
$("#saveBtn").addEventListener("click", () => {
  const syncMin = parseInt($("#interval").value) || 10;
  const pullMin = parseInt($("#pullInterval").value) || 5;

  chrome.storage.local.set({
    apiUrl: $("#apiUrl").value.trim(),
    password: $("#password").value,
    syncIntervalMinutes: syncMin,
    pullIntervalMinutes: pullMin,
  }, () => {
    $("#saveBtn").textContent = "✓ Saved!";
    setTimeout(() => { $("#saveBtn").textContent = "💾 Save Settings"; }, 1500);

    chrome.alarms.clear("sync-cookies", () => {
      chrome.alarms.create("sync-cookies", { periodInMinutes: syncMin });
    });
    chrome.alarms.clear("pull-cookies", () => {
      chrome.alarms.create("pull-cookies", { periodInMinutes: pullMin });
    });
  });
});
