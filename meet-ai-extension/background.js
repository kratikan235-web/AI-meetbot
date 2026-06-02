console.log("[background] Meet AI Notes loaded");

async function ensureOffscreenDocument() {
  // If an offscreen document already exists, do not try to create another.
  try {
    if (chrome.runtime.getContexts) {
      const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
      if (Array.isArray(contexts) && contexts.length > 0) return;
    }
  } catch {
    // ignore
  }

  // If already created, this will throw in some Chrome versions; just ignore.
  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA", "AUDIO_PLAYBACK"],
      justification: "Record Meet audio in background and keep audio audible.",
    });
  } catch (e) {
    const msg = String(e?.message || e);
    const m = msg.toLowerCase();
    if (
      m.includes("only a single offscreen document") ||
      m.includes("only one offscreen document") ||
      m.includes("single offscreen document")
    ) {
      return;
    }
    if (!m.includes("only one offscreen document")) {
      // Other errors are actionable.
      throw e;
    }
  }
}

function callOffscreen(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ target: "offscreen", ...message }, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
      } else {
        resolve(resp || { ok: false, error: "No response from offscreen" });
      }
    });
  });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== "background") return false;

  (async () => {
    if (msg.action === "PING") return { ok: true };

    if (msg.action === "START_RECORDING") {
      await ensureOffscreenDocument();
      const res = await callOffscreen({ action: "START_RECORDING", streamId: msg.streamId });
      return res;
    }

    if (msg.action === "STOP_RECORDING") {
      const res = await callOffscreen({ action: "STOP_RECORDING" });
      return res;
    }

    if (msg.action === "RELEASE") {
      const res = await callOffscreen({ action: "RELEASE" });
      return res;
    }

    return { ok: false, error: "Unknown action" };
  })()
    .then((resp) => sendResponse(resp))
    .catch((err) => sendResponse({ ok: false, error: String(err) }));

  return true;
});

chrome.runtime.onInstalled.addListener(() => {
  console.log("[background] extension installed/updated");
});
