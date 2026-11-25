let active;

async function disconnect() {
  const current = active;
  active = undefined;
  if (!current) return {message: "Disconnected"};
  clearInterval(current.heartbeat);
  current.socket.close();
  current.stream?.getTracks().forEach(track => track.stop());
  if (current.context) await current.context.close();
  return {message: "Disconnected"};
}

function pair(socket, token, meeting) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Live Studio did not respond.")), 3500);
    socket.onopen = () => socket.send(JSON.stringify({token, meet: meeting}));
    socket.onmessage = event => {
      clearTimeout(timeout);
      resolve(JSON.parse(event.data));
    };
    socket.onerror = () => {
      clearTimeout(timeout);
      reject(new Error("Open the Live Studio Windows app first."));
    };
    socket.onclose = event => {
      clearTimeout(timeout);
      reject(new Error(event.reason || "Pairing ended. Check the code and tab."));
    };
  });
}

async function connect({token, meeting, streamId}) {
  await disconnect();
  const current = {socket: new WebSocket("ws://127.0.0.1:18763/capture/v1")};
  active = current;
  try {
    const initial = await pair(current.socket, token, meeting);
    current.socket.onclose = () => { if (active === current) disconnect().catch(() => {}); };
    current.socket.onmessage = event => current.gate = JSON.parse(event.data);
    current.gate = initial;
    current.stream = await navigator.mediaDevices.getUserMedia({
      audio: {mandatory: {chromeMediaSource: "tab", chromeMediaSourceId: streamId}},
      video: false,
    });
    if (active !== current) {
      current.stream.getTracks().forEach(track => track.stop());
      return {message: "Pairing ended. Reconnect the tab."};
    }
    current.context = new AudioContext();
    const source = current.context.createMediaStreamSource(current.stream);
    source.connect(current.context.destination);
    await current.context.audioWorklet.addModule("pcm.js");
    if (active !== current) return {message: "Pairing ended. Reconnect the tab."};
    const worklet = new AudioWorkletNode(current.context, "studio-pcm", {numberOfOutputs: 0});
    source.connect(worklet);
    worklet.port.postMessage(current.gate);
    current.socket.onmessage = event => {
      current.gate = JSON.parse(event.data);
      worklet.port.postMessage(current.gate);
    };
    worklet.port.onmessage = event => {
      if (active !== current || current.socket.readyState !== WebSocket.OPEN) return;
      if (!current.gate.capture || new DataView(event.data).getUint32(0, true) !== current.gate.generation) return;
      if (current.socket.bufferedAmount > 32000) {
        disconnect().catch(() => {});
        return;
      }
      current.socket.send(event.data);
    };
    current.stream.getAudioTracks()[0].addEventListener("ended", () => {
      if (active === current) disconnect().catch(() => {});
    });
    current.heartbeat = setInterval(() => {
      if (current.socket.readyState === WebSocket.OPEN) current.socket.send("heartbeat");
    }, 450);
    await current.context.resume();
    return {connected: true, message: "Meet tab connected. Use Live Studio to start."};
  } catch (error) {
    if (active === current) await disconnect();
    return {message: error.message};
  }
}

chrome.runtime.onMessage.addListener((request, sender, respond) => {
  if (sender.id !== chrome.runtime.id || request.target !== "offscreen") return;
  if (request.action === "status") {
    respond({connected: Boolean(active?.stream), message: active?.stream ? "Meet audio connected" : ""});
    return;
  }
  const operation = request.action === "disconnect" ? disconnect() : connect(request);
  operation.then(respond, error => respond({message: error.message}));
  return true;
});
