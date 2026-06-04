const MIN_RECORD_MS = 3000;
const MIN_BLOB_BYTES = 8000;
const MIN_BYTES_PER_SECOND = 800;

const statusEl = document.getElementById("status");
let recordingStartedAt = 0;
let recordMode = "none";
let meetTabId = null;

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.style.color = isError ? "#b00020" : "#1b5e20";
}

function mapMicError(err) {
  const name = err?.name || "";
  const msg = (err?.message || "").toLowerCase();
  if (
    name === "NotAllowedError" ||
    name === "PermissionDeniedError" ||
    msg.includes("permission dismissed") ||
    msg.includes("permission denied")
  ) {
    return "Microphone blocked. Click Allow when Chrome asks, or allow mic in Chrome settings.";
  }
  if (name === "NotFoundError") {
    return "No microphone found. Check system sound settings.";
  }
  return err?.message || String(err);
}

function callBackground(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ target: "background", ...message }, (resp) => {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(resp || { ok: false, error: "No response from background" });
    });
  });
}

async function ensureOffscreenDocument() {
  // If an offscreen document already exists, do not try to create another.
  try {
    if (chrome.runtime.getContexts) {
      const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
      if (Array.isArray(contexts) && contexts.length > 0) return;
    }
  } catch {
    // getContexts not available / failed; fall back to createDocument try/catch.
  }

  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA", "AUDIO_PLAYBACK"],
      justification: "Record Meet audio in background and keep audio audible.",
    });
  } catch (e) {
    const msg = String(e?.message || e);
    const m = msg.toLowerCase();
    // Chrome errors vary by version.
    if (
      m.includes("only a single offscreen document") ||
      m.includes("only one offscreen document") ||
      m.includes("single offscreen document")
    ) {
      return;
    }
    throw e;
  }
}

function callOffscreen(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ target: "offscreen", ...message }, (resp) => {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(resp || { ok: false, error: "No response from offscreen" });
    });
  });
}

function callContentScript(tabId, message) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, { target: "content", ...message }, (resp) => {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(resp || { ok: false, error: "No response from content script" });
    });
  });
}

/** Popup has reliable tab access — fetch speaker data here at Stop. */
async function fetchSpeakerDataFromMeetTab(tabId) {
  if (!tabId) return null;

  let ping = await callContentScript(tabId, { action: "PING" });
  if (!ping?.ok) {
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
      await new Promise((r) => setTimeout(r, 100));
      ping = await callContentScript(tabId, { action: "PING" });
    } catch {
      console.warn("[popup] could not inject content.js");
    }
  }
  if (!ping?.ok) return null;

  const data = await callContentScript(tabId, { action: "GET_SPEAKER_DATA" });
  await callContentScript(tabId, { action: "CLEAR_SPEAKER_DATA" });

  if (!data?.ok) return null;

  return data;
}

async function refreshStatus() {
  try {
    await ensureOffscreenDocument();
    const st = await callOffscreen({ action: "GET_STATUS" });
    if (st?.ok && st.recording) {
      recordMode = st.mode || "recording";
      setStatus(`Recording… (${recordMode})`);
      return true;
    }
  } catch {
    // ignore
  }
  return false;
}

async function releaseIfCaptureActive() {
  await ensureOffscreenDocument();
  const st = await callOffscreen({ action: "GET_STATUS" });
  if (st?.ok && st.captureActive && !st.recording) {
    await callOffscreen({ action: "RELEASE" });
  }
}

// When popup opens, show current recording state (popup can close during Meet interactions).
refreshStatus();

document.getElementById("start").addEventListener("click", () => {
  chrome.tabs.query({ active: true, currentWindow: true }, async (tabs) => {
    const tab = tabs[0];
    if (!tab?.id) {
      setStatus("No active tab.", true);
      return;
    }
    if (!tab.url?.includes("meet.google.com")) {
      setStatus("Open your meeting tab first.", true);
      return;
    }

    setStatus("Starting…");

    try {
      // If recording already running, don't try to capture again.
      const already = await refreshStatus();
      if (already) return;

      // If a previous capture is still alive (even without recording), release it.
      // Otherwise Chrome throws: "Cannot capture a tab with an active stream."
      await releaseIfCaptureActive();

      recordingStartedAt = Date.now();
      recordMode = "starting";

      // Acquire the tab stream id in the popup (user gesture + active tab).
      const streamId = await new Promise((resolve, reject) => {
        chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id }, (id) => {
          if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
          else if (!id) reject(new Error("No tab audio stream id"));
          else resolve(id);
        });
      });

      meetTabId = tab.id;

      await ensureOffscreenDocument();
      // Prefer direct offscreen messaging (avoids SW wake/race issues).
      let res = await callOffscreen({ action: "START_RECORDING", streamId, meetTabId: tab.id });
      if (!res?.ok) {
        res = await callBackground({ action: "START_RECORDING", streamId, meetTabId: tab.id });
      }
      if (!res?.ok) {
        setStatus(res?.error || "Failed to start recording.", true);
        return;
      }

      recordMode = res.mode || "recording";
      if (res.alreadyRecording) setStatus(`Already recording… (${recordMode})`);
      else setStatus(`Recording… (${recordMode})`);
    } catch (err) {
      setStatus(mapMicError(err), true);
    }
  });
});

document.getElementById("stop").addEventListener("click", () => {
  const elapsed = Date.now() - recordingStartedAt;
  if (elapsed < MIN_RECORD_MS) {
    setStatus(`Wait ${Math.ceil((MIN_RECORD_MS - elapsed) / 1000)}s more.`, true);
    return;
  }

  setStatus("Uploading…");

  ensureOffscreenDocument()
    .then(async () => {
      const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
      const tab = tabs[0];
      const tabId = meetTabId || tab?.id || null;

      let speakerPayload = {
        recording_started_at_ms: recordingStartedAt,
        meetTabId: tabId,
      };

      if (tabId && tab?.url?.includes("meet.google.com")) {
        const speakerData = await fetchSpeakerDataFromMeetTab(tabId);
        if (speakerData) {
          speakerPayload = {
            speaker_events: speakerData.speaker_events || [],
            participants: speakerData.participants || [],
            self_name: speakerData.self_name || null,
            recording_started_at_ms: recordingStartedAt,
            meetTabId: tabId,
          };
        }
      }

      let res = await callOffscreen({ action: "STOP_RECORDING", ...speakerPayload });
      if (!res?.ok) {
        res = await callBackground({ action: "STOP_RECORDING", ...speakerPayload });
      }
      return res;
    })
    .then((res) => {
      if (!res?.ok) {
        setStatus(res?.error || "Failed to stop recording.", true);
        return;
      }
      const saved = res?.data?.file_saved || "mom_reports/";
      setStatus(`Done. MOM saved: ${saved}`);
    })
    .catch((err) => setStatus(String(err), true));
});
