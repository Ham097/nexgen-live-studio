const status = document.querySelector("#status");
const retry = document.querySelector("#retry");

async function allow() {
  retry.hidden = true;
  status.textContent = "Connecting Meet audio…";
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const result = await chrome.runtime.sendMessage({target: "worker", tabId: tab?.id, url: tab?.url});
    const failure = result?.message?.includes("activeTab") ?
      "Close this menu, then click the Live Studio icon while the Meet tab is in front." : result?.message;
    status.textContent = result?.connected ? "Meet audio connected. Return to Live Studio." :
      failure || "Open the Windows app, then try again.";
    retry.hidden = Boolean(result?.connected);
  } catch {
    retry.hidden = false;
    status.textContent = "Open the Windows app and try again.";
  }
}

retry.addEventListener("click", allow);
allow();
