const code = document.querySelector("#code");
const status = document.querySelector("#status");
const buttons = [...document.querySelectorAll("button")];

async function send(action, additional = {}) {
  buttons.forEach(button => button.disabled = true);
  try {
    const response = await chrome.runtime.sendMessage({target: "worker", action, ...additional});
    status.textContent = response?.message ?? "Open Live Studio and try again.";
  } catch {
    status.textContent = "The Meet bridge could not be reached.";
  } finally {
    buttons.forEach(button => button.disabled = false);
  }
}

document.querySelector("form").addEventListener("submit", async event => {
  event.preventDefault();
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const token = code.value.trim();
  await chrome.storage.session.set({token});
  await send("connect", {token, tabId: tab.id, url: tab.url});
});
document.querySelector("#disconnect").addEventListener("click", () => send("disconnect"));
chrome.storage.session.get("token").then(value => code.value = value.token || "");
send("status");
