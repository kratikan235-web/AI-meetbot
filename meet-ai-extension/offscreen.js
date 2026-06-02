let mediaRecorder = null;
let audioChunks = [];
let recordStream = null;
let tabStream = null;
let micStream = null;
let audioContext = null;
let monitorGain = null;
let recordingStartedAt = 0;
let recordingMode = "none";

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
    startRecording(msg.streamId)
      .then((info) => sendResponse({ ok: true, ...info }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (msg.action === "STOP_RECORDING") {
    stopRecording()
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

async function startRecording(streamId) {
  if (!streamId) throw new Error("Missing stream id");
  if (mediaRecorder?.state === "recording") {
    return { mode: recordingMode, alreadyRecording: true };
  }

  await releaseCapture();

  audioChunks = [];
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
  console.log("[offscreen] MediaRecorder started, mode:", recordingMode);
  return { mode: recordingMode };
}

// Keep audio contexts alive even if the document becomes hidden.
document.addEventListener("visibilitychange", () => {
  if (!audioContext) return;
  if (audioContext.state === "suspended") {
    audioContext.resume().catch(() => {});
  }
});

function stopRecording() {
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
      audioChunks = [];
      recordingStartedAt = 0;

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
        const data = await uploadBlob(blob);
        resolve({ ok: true, data, mode: recordingMode });
      } catch (err) {
        resolve({ ok: false, error: String(err) });
      }
    };

    if (mediaRecorder.state === "recording") mediaRecorder.requestData();
    mediaRecorder.stop();
  });
}

async function uploadBlob(blob) {
  const formData = new FormData();
  formData.append("file", blob, `meeting_${Date.now()}.webm`);

  const res = await fetch(BACKEND_URL, { method: "POST", body: formData });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Backend ${res.status}: ${text}`);
  }
  return res.json();
}
