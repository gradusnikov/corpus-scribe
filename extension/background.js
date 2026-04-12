// Background service worker — handles notification display from popup.js
chrome.runtime.onMessage.addListener((request, _sender, _sendResponse) => {
  if (request.action === "showNotification") {
    chrome.notifications.create(`scribe-${Date.now()}`, {
      type: "basic",
      iconUrl: "assets/icons/128.png",
      title: request.title,
      message: request.message,
    });
  }
});
