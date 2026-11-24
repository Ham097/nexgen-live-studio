const documentURL = "offscreen.html";
let offscreenPromise;
let captureTab;

async function offscreen() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [chrome.runtime.getURL(documentURL)],
  });
  return contexts.length > 0;
}

async function ensureOffscreen() {
  if (await offscreen()) return;
  if (!offscreenPromise) {
    offscreenPromise = chrome.offscreen.createDocument({
      url: documentURL,
      reasons: ["USER_MEDIA"],
      justification: "Receive audio from the teacher-selected Meet tab and restore tab playback.",
    }).finally(() => offscreenPromise = undefined);
  }
  await offscreenPromise;
}

async function bridge(action, extra = {}) {
  return chrome.runtime.sendMessage({target: "offscreen", action, ...extra});
}

const meet = url => /^https:\/\/meet\.google\.com\/([a-z]{3}-[a-z]{4}-[a-z]{3})(?:[/?#].*)?$/.exec(url || "")?.[1];

async function handle({action, tabId, url, token}) {
  if (action === "status") {
    return await offscreen() ? bridge("status") : {message: "Disconnected"};
  }
  if (action === "disconnect") {
    captureTab = undefined;
    await chrome.storage.session.remove("capture");
    return await offscreen() ? bridge("disconnect") : {message: "Disconnected"};
  }
  if (action !== "connect") throw new Error("Unknown bridge action.");
  const meeting = meet(url);
  if (!meeting) throw new Error("Open an active Google Meet call before connecting.");
  if (!token || token.length > 64) throw new Error("Copy the pairing code from Live Studio.");
  await ensureOffscreen();
  await bridge("disconnect");
  const streamId = await chrome.tabCapture.getMediaStreamId({targetTabId: tabId});
  const response = await bridge("connect", {streamId, meeting, token});
  if (response.connected) {
    captureTab = {tabId, meeting};
    await chrome.storage.session.set({capture: captureTab});
  }
  return response;
}

chrome.runtime.onMessage.addListener((request, sender, respond) => {
  if (sender.id !== chrome.runtime.id || request.target !== "worker") return;
  if (!sender.url?.startsWith(chrome.runtime.getURL("popup.html"))) return;
  handle(request).then(respond, error => respond({message: error.message}));
  return true;
});

async function checkTab(tabId, url, navigating = false) {
  const capture = captureTab ?? (await chrome.storage.session.get("capture")).capture;
  if (capture?.tabId === tabId && (navigating || capture.meeting !== meet(url))) {
    await handle({action: "disconnect"});
  }
}

chrome.tabs.onRemoved.addListener(tabId => checkTab(tabId, null).catch(() => {}));
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.url || change.status === "loading") {
    checkTab(tabId, change.url, change.status === "loading").catch(() => {});
  }
});
