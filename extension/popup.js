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
chrome.storage.local.get(["apiUrl", "password", "syncIntervalMinutes"], (c) => {
  $("#apiUrl").value = c.apiUrl || "";
  $("#password").value = c.password || "";
  $("#interval").value = c.syncIntervalMinutes || 10;
});

// Sync button
$("#syncBtn").addEventListener("click", () => {
  $("#syncBtn").disabled = true;
  $("#syncBtn").textContent = "Syncing...";
  chrome.runtime.sendMessage({ action: "sync-now" }, (res) => {
    $("#syncBtn").disabled = false;
    $("#syncBtn").textContent = res.ok ? "✓ Synced!" : "✗ Failed";
    setTimeout(() => { $("#syncBtn").textContent = "⚡ Sync Now"; }, 2000);
    // Refresh status
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
  chrome.storage.local.set({
    apiUrl: $("#apiUrl").value.trim(),
    password: $("#password").value,
    syncIntervalMinutes: parseInt($("#interval").value) || 10,
  }, () => {
    $("#saveBtn").textContent = "✓ Saved!";
    setTimeout(() => { $("#saveBtn").textContent = "💾 Save Settings"; }, 1500);
    // Reset alarm with new interval
    chrome.alarms.clear("sync-cookies", () => {
      chrome.alarms.create("sync-cookies", {
        periodInMinutes: parseInt($("#interval").value) || 10,
      });
    });
  });
});
