const documentURL = "offscreen.html";
let offscreenPromise;
let captureTab;
let nativePort;
let token;
let discovery;
let nativeTick;

function openNative() {
  if (nativePort) return;
  const port = chrome.runtime.connectNative("com.nexgen.live_studio");
  nativePort = port;
  port.onMessage.addListener(message => {
    if (nativePort !== port) return;
    if (token !== message.token) {
      token = message.token;
      discovery?.close();
      discovery = undefined;
    }
    if (token && !discovery) connectDiscovery(token);
  });
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    if (nativePort !== port) return;
    clearInterval(nativeTick);
    nativePort = undefined;
    token = undefined;
    discovery?.close();
    discovery = undefined;
  });
  const read = () => { try { port.postMessage({status: true}); } catch {} };
  read();
  nativeTick = setInterval(read, 1500);
}

const findMeetTabs = () => chrome.tabs.query({url: "https://meet.google.com/*"});

async function reportTabs(socket = discovery) {
  if (socket?.readyState !== WebSocket.OPEN) return;
  const rooms = [...new Set((await findMeetTabs()).map(tab => meet(tab.url)).filter(Boolean))];
  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({rooms}));
}

function connectDiscovery(key) {
  const socket = new WebSocket("ws://127.0.0.1:18763/discover/v2");
  discovery = socket;
  let interval;
  socket.onopen = async () => {
    socket.send(JSON.stringify({token: key}));
    await reportTabs(socket);
    interval = setInterval(() => reportTabs(socket).catch(() => {}), 1900);
  };
  socket.onmessage = async ({data}) => {
    try {
      const {focus} = JSON.parse(data);
      const tab = (await findMeetTabs()).find(candidate => meet(candidate.url) === focus);
      if (tab) {
        await chrome.tabs.update(tab.id, {active: true});
        await chrome.windows.update(tab.windowId, {focused: true});
      }
    } catch {}
  };
  socket.onclose = () => {
    clearInterval(interval);
    if (discovery === socket) discovery = undefined;
  };
}

chrome.runtime.onStartup.addListener(openNative);
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("nexgen-discovery", {periodInMinutes: 0.5});
  openNative();
});
chrome.alarms.onAlarm.addListener(() => openNative());

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

async function handle({action, tabId, url}) {
  if (action === "status") {
    const state = await offscreen() ? await bridge("status") : {};
    return {...state, studio: Boolean(token)};
  }
  if (action === "disconnect") {
    captureTab = undefined;
    await chrome.storage.session.remove("capture");
    return await offscreen() ? bridge("disconnect") : {message: "Disconnected"};
  }
  if (action !== "connect") throw new Error("Unknown bridge action.");
  const meeting = meet(url);
  if (!meeting) throw new Error("Open an active Google Meet call before connecting.");
  if (!token) throw new Error("Open the Windows app and try again in a moment.");
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
  if (request.target !== "worker" || sender.id !== chrome.runtime.id ||
      sender.url !== chrome.runtime.getURL("connect.html")) return;
  const {tabId, url} = request;
  async function allow() {
    if (!meet(url)) throw new Error("Open a Google Meet call, then click this icon.");
    if (captureTab?.tabId === tabId && (await handle({action: "status"})).connected)
      return {connected: true};
    return handle({action: "connect", tabId, url});
  }
  allow().then(response => {
    if (response.connected) {
      chrome.action.setBadgeText({tabId, text: "ON"}).catch(() => {});
      chrome.action.setBadgeBackgroundColor({tabId, color: "#16634d"}).catch(() => {});
    }
    respond(response);
  }, error => respond({message: error.message}));
  return true;
});

async function checkTab(tabId, url, navigating = false) {
  const capture = captureTab ?? (await chrome.storage.session.get("capture")).capture;
  if (capture?.tabId === tabId && (navigating || capture.meeting !== meet(url))) {
    await handle({action: "disconnect"});
    await chrome.action.setBadgeText({tabId, text: ""}).catch(() => {});
  }
}

chrome.tabs.onRemoved.addListener(tabId => checkTab(tabId, null).catch(() => {}));
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.url || change.status === "loading") {
    checkTab(tabId, change.url, change.status === "loading").catch(() => {});
    reportTabs().catch(() => {});
    openNative();
  }
});
chrome.tabs.onActivated.addListener(() => { openNative(); reportTabs().catch(() => {}); });
openNative();
