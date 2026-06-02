const BACKEND_URL = "http://127.0.0.1:8000/upload";
const MIN_RECORD_MS = 3000;
const MIN_BLOB_BYTES = 8000;
const MIN_BYTES_PER_SECOND = 800;

const statusEl = document.getElementById("status");
let mediaRecorder = null;
let audioChunks = [];
let streamsToStop = [];
let audioContext = null;
let recordingStartedAt = 0;
let recordMode = "none";

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.style.color = isError ? "#b00020" : "#1b5e20";
}

function stopAllStreams() {
  streamsToStop.forEach((stream) => stream.getTracks().forEach((t) => t.stop()));
  streamsToStop = [];
  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
  }
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

async function requestMicrophone() {
  return navigator.mediaDevices.getUserMedia({ audio: true, video: false });
}

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
      stopAllStreams();
      if (mediaRecorder?.state === "recording") mediaRecorder.stop();
      mediaRecorder = null;
      audioChunks = [];
      recordingStartedAt = Date.now();

      const micStream = await requestMicrophone();
      streamsToStop.push(micStream);

      let recordStream = micStream;
      recordMode = "mic";

      try {
        const streamId = await new Promise((resolve, reject) => {
          chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id }, (id) => {
            if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
            else if (!id) reject(new Error("No tab audio"));
            else resolve(id);
          });
        });

        const tabStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            mandatory: {
              chromeMediaSource: "tab",
              chromeMediaSourceId: streamId,
            },
          },
          video: false,
        });
        streamsToStop.push(tabStream);

        audioContext = new AudioContext();
        await audioContext.resume();
        const destination = audioContext.createMediaStreamDestination();
        audioContext.createMediaStreamSource(micStream).connect(destination);
        audioContext.createMediaStreamSource(tabStream).connect(destination);
        recordStream = destination.stream;
        recordMode = "mic+tab";
      } catch {
        // mic only is fine
      }

      const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";

      mediaRecorder = new MediaRecorder(recordStream, {
        mimeType,
        audioBitsPerSecond: 128000,
      });

      mediaRecorder.ondataavailable = (event) => {
        if (event.data?.size > 0) audioChunks.push(event.data);
      };

      mediaRecorder.start(500);
      setStatus("Recording…");
    } catch (err) {
      stopAllStreams();
      setStatus(mapMicError(err), true);
    }
  });
});

document.getElementById("stop").addEventListener("click", () => {
  if (!mediaRecorder || mediaRecorder.state === "inactive") {
    setStatus("Not recording. Click Start first.", true);
    return;
  }

  const elapsed = Date.now() - recordingStartedAt;
  if (elapsed < MIN_RECORD_MS) {
    setStatus(`Wait ${Math.ceil((MIN_RECORD_MS - elapsed) / 1000)}s more.`, true);
    return;
  }

  setStatus("Uploading…");

  mediaRecorder.onstop = async () => {
    const blob = new Blob(audioChunks, { type: "audio/webm" });
    const durationSec = elapsed / 1000;
    const bps = blob.size / durationSec;
    audioChunks = [];
    mediaRecorder = null;
    stopAllStreams();

    if (blob.size < MIN_BLOB_BYTES || bps < MIN_BYTES_PER_SECOND) {
      setStatus("Recording too quiet. Check microphone and try again.", true);
      return;
    }

    try {
      const formData = new FormData();
      formData.append("file", blob, `meeting_${Date.now()}.webm`);
      const res = await fetch(BACKEND_URL, { method: "POST", body: formData });
      const text = await res.text();
      if (!res.ok) {
        let detail = text;
        try {
          detail = JSON.parse(text).detail || text;
        } catch {
          // keep text
        }
        setStatus(String(detail), true);
        return;
      }
      const data = JSON.parse(text);
      setStatus(`Done. MOM saved: ${data.file_saved || "mom_reports/"}`);
    } catch (err) {
      setStatus(err.message, true);
    }
  };

  mediaRecorder.requestData();
  mediaRecorder.stop();
});
