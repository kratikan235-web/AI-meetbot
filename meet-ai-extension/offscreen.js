let mediaRecorder = null;
let audioChunks = [];
let recordStream = null;
let tabStream = null;
let micStream = null;
let audioContext = null;
let monitorGain = null;
let recordingStartedAt = 0;
let recordingMode = "none";
let meetTabId = null;

const BACKEND_URL = "http://127.0.0.1:8000/upload";
const MIN_BLOB_BYTES = 5_000;
const MIN_RECORD_MS = 3_000;
const MIN_BYTES_PER_SECOND = 800;

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.target !== "offscreen") return false;

  if (msg.action === "PING") {
    sendResponse({ ok: true });
    return false;
  }

  if (msg.action === "GET_STATUS") {
    const hasLiveTracks = (s) => Boolean(s && s.getTracks().some((t) => t.readyState === "live"));
    sendResponse({
      ok: true,
      recording: Boolean(mediaRecorder && mediaRecorder.state === "recording"),
      captureActive: hasLiveTracks(recordStream) || hasLiveTracks(tabStream) || hasLiveTracks(micStream),
      mode: recordingMode,
      startedAt: recordingStartedAt,
    });
    return false;
  }

  if (msg.action === "RELEASE") {
    releaseCapture()
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (msg.action === "START_RECORDING") {
    startRecording(msg.streamId, msg.meetTabId)
      .then((info) => sendResponse({ ok: true, ...info }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (msg.action === "STOP_RECORDING") {
    stopRecording({
      speaker_events: msg.speaker_events,
      participants: msg.participants,
      self_name: msg.self_name,
      recording_started_at_ms: msg.recording_started_at_ms,
      meetTabId: msg.meetTabId,
    })
      .then((result) => sendResponse(result))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  return false;
});

function stopAllTracks(stream) {
  stream?.getTracks().forEach((track) => {
    track.onended = null;
    track.stop();
  });
}

function releaseCapture() {
  return new Promise((resolve) => {
    const finish = () => {
      stopAllTracks(recordStream);
      stopAllTracks(tabStream);
      stopAllTracks(micStream);
      recordStream = null;
      tabStream = null;
      micStream = null;
      mediaRecorder = null;
      audioChunks = [];
      recordingStartedAt = 0;
      recordingMode = "none";
      meetTabId = null;

      if (audioContext) {
        audioContext.close().catch(() => {});
        audioContext = null;
      }
      monitorGain = null;
      console.log("[offscreen] capture released");
      resolve();
    };

    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      try {
        mediaRecorder.onstop = () => finish();
        if (mediaRecorder.state === "recording") mediaRecorder.requestData();
        mediaRecorder.stop();
      } catch {
        finish();
      }
      return;
    }
    finish();
  });
}

/** Quick check that a stream carries non-zero audio (mic not muted). */
async function streamHasAudio(stream, sampleMs = 500) {
  const ctx = new AudioContext();
  try {
    await ctx.resume();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);

    const bins = new Uint8Array(analyser.frequencyBinCount);
    let peak = 0;
    const end = performance.now() + sampleMs;
    while (performance.now() < end) {
      analyser.getByteFrequencyData(bins);
      for (const v of bins) peak = Math.max(peak, v);
      await new Promise((r) => setTimeout(r, 50));
    }
    console.log("[offscreen] audio peak level:", peak);
    return peak > 8;
  } finally {
    await ctx.close();
  }
}

/**
 * Reliable path: record microphone directly.
 * Optionally mix Meet tab audio when AudioContext is resumed (not suspended).
 */
async function buildRecordStream(streamId) {
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
    video: false,
  });

  const micTrack = micStream.getAudioTracks()[0];
  if (!micTrack || micTrack.readyState !== "live") {
    throw new Error("Microphone not available. Allow microphone in Chrome and try again.");
  }

  recordingMode = "mic";
  let stream = micStream;

  try {
    tabStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: "tab",
          chromeMediaSourceId: streamId,
        },
      },
      video: false,
    });

    audioContext = new AudioContext();
    await audioContext.resume();

    const destination = audioContext.createMediaStreamDestination();
    audioContext.createMediaStreamSource(tabStream).connect(destination);
    audioContext.createMediaStreamSource(micStream).connect(destination);

    // Ensure the user can still hear the meeting while we capture tab audio.
    // Some capture paths effectively "steal" audio output from the tab.
    // We intentionally do NOT route microphone to speakers to avoid feedback.
    monitorGain = audioContext.createGain();
    monitorGain.gain.value = 1.0;
    audioContext.createMediaStreamSource(tabStream).connect(monitorGain).connect(audioContext.destination);

    const mixed = destination.stream;
    const mixLive = await streamHasAudio(mixed, 400);
    if (mixLive) {
      stream = mixed;
      recordingMode = "mic+tab";
      console.log("[offscreen] recording mic + Meet tab");
    } else {
      console.warn("[offscreen] tab mix silent — using microphone only");
      stopAllTracks(tabStream);
      tabStream = null;
      await audioContext.close();
      audioContext = null;
      monitorGain = null;
    }
  } catch (err) {
    console.warn("[offscreen] tab capture skipped:", err);
    stopAllTracks(tabStream);
    tabStream = null;
  }

  return stream;
}

function sendContentMessage(tabId, message) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, { target: "content", ...message }, (resp) => {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(resp || { ok: false, error: "No response from content script" });
    });
  });
}

async function ensureContentScriptOnTab(tabId) {
  const ping = await sendContentMessage(tabId, { action: "PING" });
  if (ping?.ok) return true;
  if (!chrome.scripting?.executeScript) return false;
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  } catch {
    return false;
  }
  const ping2 = await sendContentMessage(tabId, { action: "PING" });
  return Boolean(ping2?.ok);
}

/** Tell Meet tab to track speakers (must run after MediaRecorder.start). */
async function notifyMeetRecordingStarted(tabId, startedAtMs) {
  if (!tabId) {
    console.warn("[offscreen] notifyMeetRecordingStarted: no meetTabId");
    return false;
  }
  const ready = await ensureContentScriptOnTab(tabId);
  if (!ready) {
    console.warn("[offscreen] content script not ready on tab", tabId);
    return false;
  }
  const res = await sendContentMessage(tabId, {
    action: "RECORDING_STARTED",
    startedAtMs,
  });
  console.log("[offscreen] RECORDING_STARTED → tab", tabId, res);
  return Boolean(res?.ok);
}

async function fetchSpeakerDataFromMeetTab(preferredTabId, startedAtMs) {
  const empty = { speaker_events: [], participants: [], self_name: null, recording_started_at_ms: startedAtMs };
  if (!chrome.tabs?.query) return empty;

  let tabId = preferredTabId || meetTabId;
  let tab = null;

  if (tabId) {
    try {
      tab = await chrome.tabs.get(tabId);
    } catch {
      tabId = null;
    }
  }

  if (!tabId || !tab?.url?.includes("meet.google.com")) {
    try {
      const tabs = await chrome.tabs.query({ url: "https://meet.google.com/*" });
      tab = tabs[0] || null;
      tabId = tab?.id || null;
    } catch {
      return empty;
    }
  }

  if (!tabId) return empty;

  const ready = await ensureContentScriptOnTab(tabId);
  if (!ready) return empty;

  const data = await sendContentMessage(tabId, { action: "GET_SPEAKER_DATA" });
  await sendContentMessage(tabId, { action: "CLEAR_SPEAKER_DATA" });

  if (!data?.ok) {
    console.warn("[offscreen] GET_SPEAKER_DATA failed on tab", tabId, data);
    return empty;
  }

  console.log("[offscreen] participants:", JSON.stringify(data.participants || []));
  console.log("[offscreen] speaker_events:", JSON.stringify(data.speaker_events || []));
  console.log("[offscreen] self_name:", data.self_name || "(none)");

  return {
    speaker_events: data.speaker_events || [],
    participants: data.participants || [],
    self_name: data.self_name || null,
    recording_started_at_ms: startedAtMs,
  };
}

async function startRecording(streamId, tabId) {
  if (!streamId) throw new Error("Missing stream id");
  if (mediaRecorder?.state === "recording") {
    return { mode: recordingMode, alreadyRecording: true };
  }

  await releaseCapture();

  audioChunks = [];
  meetTabId = typeof tabId === "number" ? tabId : null;
  recordingStartedAt = Date.now();

  recordStream = await buildRecordStream(streamId);

  const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus"
    : "audio/webm";

  mediaRecorder = new MediaRecorder(recordStream, {
    mimeType,
    audioBitsPerSecond: 128000,
  });

  mediaRecorder.ondataavailable = (event) => {
    if (event.data?.size > 0) {
      audioChunks.push(event.data);
      console.log("[offscreen] chunk bytes:", event.data.size);
    }
  };

  mediaRecorder.start(500);
  console.log("[offscreen] MediaRecorder started, mode:", recordingMode, "meetTabId:", meetTabId);
  await notifyMeetRecordingStarted(meetTabId, recordingStartedAt);
  return { mode: recordingMode };
}

// Keep audio contexts alive even if the document becomes hidden.
document.addEventListener("visibilitychange", () => {
  if (!audioContext) return;
  if (audioContext.state === "suspended") {
    audioContext.resume().catch(() => {});
  }
});

function hasSpeakerPayload(meta) {
  return Boolean(
    meta?.self_name ||
      (Array.isArray(meta?.speaker_events) && meta.speaker_events.length) ||
      (Array.isArray(meta?.participants) && meta.participants.length),
  );
}

function stopRecording(prefetched = null) {
  return new Promise((resolve) => {
    if (!mediaRecorder || mediaRecorder.state === "inactive") {
      resolve({ ok: false, error: "Not recording. Click Start first." });
      return;
    }

    const elapsed = Date.now() - recordingStartedAt;
    if (elapsed < MIN_RECORD_MS) {
      resolve({
        ok: false,
        error: `Wait ${Math.ceil((MIN_RECORD_MS - elapsed) / 1000)}s more, then Stop.`,
      });
      return;
    }

    mediaRecorder.onstop = async () => {
      const blob = new Blob(audioChunks, { type: "audio/webm" });
      const durationSec = elapsed / 1000;
      const startedAtMs = recordingStartedAt;
      const tabForSpeaker = meetTabId;
      audioChunks = [];

      let speakerMeta = {
        speaker_events: Array.isArray(prefetched?.speaker_events) ? prefetched.speaker_events : [],
        participants: Array.isArray(prefetched?.participants) ? prefetched.participants : [],
        self_name: prefetched?.self_name || null,
        recording_started_at_ms:
          typeof prefetched?.recording_started_at_ms === "number" && prefetched.recording_started_at_ms > 0
            ? prefetched.recording_started_at_ms
            : startedAtMs,
      };

      if (!hasSpeakerPayload(speakerMeta)) {
        try {
          const fromTab = await fetchSpeakerDataFromMeetTab(
            prefetched?.meetTabId || tabForSpeaker,
            startedAtMs,
          );
          if (hasSpeakerPayload(fromTab)) speakerMeta = fromTab;
        } catch (err) {
          console.warn("[offscreen] speaker fetch fallback failed:", err);
        }
      }

      console.log("[offscreen] speaker_events uploaded:", JSON.stringify(speakerMeta.speaker_events));
      console.log("[offscreen] participants uploaded:", JSON.stringify(speakerMeta.participants));

      recordingStartedAt = 0;
      meetTabId = null;

      const bps = blob.size / durationSec;
      console.log(`[offscreen] blob ${blob.size} bytes, ${durationSec.toFixed(1)}s, ${bps.toFixed(0)} B/s, mode=${recordingMode}`);

      await releaseCapture();

      if (blob.size < MIN_BLOB_BYTES) {
        resolve({
          ok: false,
          error: `File too small (${blob.size} bytes). Allow microphone and speak louder.`,
        });
        return;
      }

      if (bps < MIN_BYTES_PER_SECOND) {
        resolve({
          ok: false,
          error: `Recording is silent (${Math.round(bps)} bytes/s). Allow microphone in Chrome settings, pick the correct input device, then try again.`,
        });
        return;
      }

      try {
        const data = await uploadBlob(blob, speakerMeta);
        resolve({ ok: true, data, mode: recordingMode });
      } catch (err) {
        resolve({ ok: false, error: String(err) });
      }
    };

    if (mediaRecorder.state === "recording") mediaRecorder.requestData();
    mediaRecorder.stop();
  });
}

async function uploadBlob(blob, extra = null) {
  const formData = new FormData();
  formData.append("file", blob, `meeting_${Date.now()}.webm`);
  const events = extra?.speaker_events ?? [];
  const parts = extra?.participants ?? [];
  const startedAt = extra?.recording_started_at_ms ?? 0;
  formData.append("speaker_events", JSON.stringify(events));
  formData.append("participants", JSON.stringify(parts));
  formData.append("recording_started_at_ms", String(startedAt));
  if (extra?.self_name) {
    formData.append("self_name", String(extra.self_name));
  }

  console.log(
    `[offscreen] POST /upload participants=${parts.length} events=${events.length} startedAt=${startedAt} self_name=${extra?.self_name || "(none)"}`,
  );
  console.log("[offscreen] upload speaker_events:", JSON.stringify(events));
  console.log("[offscreen] upload participants:", JSON.stringify(parts));

  const res = await fetch(BACKEND_URL, { method: "POST", body: formData });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Backend ${res.status}: ${text}`);
  }
  return res.json();
}
