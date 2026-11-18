// Background service worker: manages the extension badge when streams are found.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "mediaFound") {
    const tabId = sender.tab?.id;
    if (tabId) {
      chrome.action.setBadgeText({ text: String(msg.count || 1), tabId });
      chrome.action.setBadgeBackgroundColor({ color: "#3b82f6", tabId });
      chrome.action.setTitle({ title: `${msg.count} stream(s) ready for Directarr`, tabId });
    }
  }
  if (msg.action === "clearBadge") {
    const tabId = msg.tabId;
    if (tabId) {
      chrome.action.setBadgeText({ text: "", tabId });
      chrome.action.setTitle({ title: "Directarr Capture", tabId });
    }
  }
});

// Clear badge when popup opens (popup will also request this)
chrome.action.onClicked?.addListener?.(() => {}); // no-op; popup is default
