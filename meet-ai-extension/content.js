// Meet speaker tracking v3.0 — real participant names only.
// Solo meeting: only your Google account name (e.g. Jane Smith).

const POLL_MS = 400;
const ROSTER_REFRESH_MS = 3000;
const BUILD = "content-3.2";

let lastSpeakerName = null;
let lastEmittedAt = 0;
let lastRosterRefresh = 0;
let selfName = null;
let lastCaptionText = "";
let recordingStartMs = 0;
let collectedSpeakerEvents = [];
/** @type {{ name: string, source: string }[]} */
let rosterParticipants = [];

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== "content") return false;

  if (msg.action === "PING") {
    sendResponse({ ok: true, build: BUILD });
    return false;
  }

  if (msg.action === "RECORDING_STARTED") {
    collectedSpeakerEvents = [];
    rosterParticipants = [];
    lastSpeakerName = null;
    lastEmittedAt = 0;
    lastCaptionText = "";
    lastRosterRefresh = 0;
    recordingStartMs = typeof msg.startedAtMs === "number" ? msg.startedAtMs : Date.now();
    selfName = findSelfNameFromAccount();
    refreshRosterParticipants(true);
    applySoloMeetingPolicy();
    if (selfName) {
      collectedSpeakerEvents = [{ t: 0, name: selfName, source: "recording_start" }];
    }
    sendResponse({
      ok: true,
      self_name: selfName,
      solo: isSoloMeeting(),
      roster: rosterParticipants,
    });
    return false;
  }

  if (msg.action === "GET_SPEAKER_DATA") {
    finalizeSpeakerData();
    ensureMinimumSpeakerPayload();
    const rosterNames = rosterParticipants
      .map((p) => p.name)
      .filter((n) => isHumanParticipantName(n));
    const participantSet = new Set(rosterNames);
    if (selfName && isHumanParticipantName(selfName)) {
      participantSet.add(selfName);
    }
    const payload = {
      ok: true,
      build: BUILD,
      speaker_events: collectedSpeakerEvents.slice(-2000),
      participants: [...participantSet],
      self_name: selfName,
      solo: isSoloMeeting(),
    };
    sendResponse(payload);
    return false;
  }

  if (msg.action === "CLEAR_SPEAKER_DATA") {
    collectedSpeakerEvents = [];
    rosterParticipants = [];
    recordingStartMs = 0;
    sendResponse({ ok: true });
    return false;
  }

  return false;
});

function debugLog() {}

function relMs() {
  return Math.max(0, Date.now() - (recordingStartMs || Date.now()));
}

function normalizeText(s) {
  return String(s || "").replace(/\u00A0/g, " ").trim();
}

/** Meet UI action labels — NOT people (appear inside People panel as listitems). */
const MEET_ACTION_LABELS = new Set([
  "hand raises",
  "raise hand",
  "lower hand",
  "backgrounds and effects",
  "backgrounds & effects",
  "pin",
  "unpin",
  "mute",
  "unmute",
  "present now",
  "more options",
  "remove from meeting",
  "add people",
  "more",
  "add",
  "dial",
  "others",
  "your",
]);

function isMeetUiChromeLabel(name) {
  const lower = normalizeText(name).toLowerCase();
  if (!lower) return true;
  if (MEET_ACTION_LABELS.has(lower)) return true;
  if (/^hand\s+raises?$/i.test(lower)) return true;
  if (/^backgrounds?\s+(and\s+)?effects?$/i.test(lower)) return true;
  if (/^(raise|lower)\s+hand$/i.test(lower)) return true;
  if (/\b(reaction|caption|microphone|camera|present|toolbar|effects|backgrounds|raises?)\b/i.test(lower)) {
    return true;
  }
  return false;
}

function isHumanParticipantName(name) {
  const n = normalizeText(name);
  if (!n || n.length < 2 || n.length > 40) return false;
  if (isMeetUiChromeLabel(n)) return false;
  if (/[.!?,:;@]/.test(n)) return false;
  if (/https?:\/\//i.test(n)) return false;

  const lower = n.toLowerCase();
  if (MEET_ACTION_LABELS.has(lower)) return false;
  if (/\b(raises?|effects?|backgrounds?|hand|pin|unpin|mute|unmute|present|notification|feature|panel|control|caption|language|font|meeting)\b/i.test(n)) {
    return false;
  }

  const words = n.split(/\s+/);
  if (words.length > 4) return false;

  // Real names: First Last (2+ words). Single words are Meet buttons (More, Add, Dial).
  if (words.length >= 2) {
    if (!words.every((w) => /^[A-Z][a-zA-Z'-]+$/.test(w))) return false;
  } else {
    return false;
  }

  return true;
}

function isInsideToolbarOrControls(el) {
  return Boolean(
    el?.closest?.(
      '[role="toolbar"], [role="menu"], [role="menuitem"], [data-is-tooltip-wrapper], [jsname="vMIvv"]',
    ),
  );
}

function extractNameFromParticipantTile(tile) {
  if (isInsideToolbarOrControls(tile)) return null;

  const fromAttr = cleanRosterDisplayName(tile.getAttribute("data-self-name"));
  if (fromAttr && isHumanParticipantName(fromAttr)) return fromAttr;

  for (const el of tile.querySelectorAll("span, div")) {
    if (isInsideToolbarOrControls(el)) continue;
    const t = cleanRosterDisplayName(el.textContent);
    if (!t || t.length > 35 || t.includes("\n")) continue;
    const words = t.split(/\s+/);
    if (words.length >= 2 && words.length <= 4 && isHumanParticipantName(t)) {
      return t;
    }
  }

  const aria = normalizeText(tile.getAttribute("aria-label") || "");
  if (aria) {
    if (/\(you\)/i.test(aria)) {
      const youName = cleanRosterDisplayName(tile.getAttribute("data-self-name"));
      if (youName && isHumanParticipantName(youName)) return youName;
      const ym = aria.match(/^(.+?)\s*\(you\)/i);
      if (ym && isHumanParticipantName(ym[1])) return ym[1].trim();
    }
    const stripped = aria
      .replace(/['']s\s+(microphone|camera|presentation|screen).*/i, "")
      .replace(/\s*\(.*\)\s*$/, "")
      .trim();
    const m = stripped.match(/^([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+)?)/);
    if (m && isHumanParticipantName(m[1])) return m[1].trim();
  }

  for (const sel of ["[data-self-name]", "[data-participant-name]", "span"]) {
    const el = tile.querySelector(sel);
    if (!el) continue;
    const t = cleanRosterDisplayName(el.textContent || el.getAttribute("data-self-name"));
    if (t && isHumanParticipantName(t) && t.split(/\s+/).length >= 2) return t;
  }
  return null;
}

function countOtherParticipantsOnPage() {
  let count = 0;
  for (const tile of document.querySelectorAll("[data-participant-id]")) {
    const aria = normalizeText(tile.getAttribute("aria-label") || "");
    if (/\(you\)/i.test(aria)) continue;
    const name = extractNameFromParticipantTile(tile);
    if (name && name !== selfName && isHumanParticipantName(name)) count++;
  }
  return count;
}

function isSoloMeeting() {
  if (!selfName || !isHumanParticipantName(selfName)) return false;
  if (countOtherParticipantsOnPage() > 0) return false;
  const humans = rosterParticipants.filter((p) => isHumanParticipantName(p.name));
  const others = humans.filter((p) => p.name !== selfName);
  return others.length === 0;
}

function applySoloMeetingPolicy() {
  if (!isSoloMeeting()) return;
  rosterParticipants = [{ name: selfName, source: "google_account_solo" }];
  debugLog("roster", "solo meeting — only attendee", selfName);
}

function rosterNameSet() {
  return new Set(rosterParticipants.map((p) => p.name));
}

function rosterHasName(name) {
  return rosterNameSet().has(normalizeText(name));
}

function addRosterParticipant(name, source) {
  const n = normalizeText(name);
  if (!isHumanParticipantName(n)) return;
  if (rosterHasName(n)) return;
  rosterParticipants.push({ name: n, source });
  debugLog("participant", `added "${n}"`, source);
}

function findSelfNameFromAccount() {
  for (const el of document.querySelectorAll("button[aria-label], a[aria-label]")) {
    const label = normalizeText(el.getAttribute("aria-label"));
    const m = label.match(/google account:\s*([^,(]+)/i);
    if (m && isHumanParticipantName(m[1])) {
      debugLog("self", "google account", m[1].trim());
      return m[1].trim();
    }
  }
  for (const tile of document.querySelectorAll("[data-participant-id]")) {
    const aria = normalizeText(tile.getAttribute("aria-label") || "");
    if (!/\(you\)/i.test(aria)) continue;
    const fromAttr = cleanRosterDisplayName(tile.getAttribute("data-self-name"));
    if (fromAttr && isHumanParticipantName(fromAttr)) {
      debugLog("self", "participant tile (you)", fromAttr);
      return fromAttr;
    }
    const m = aria.match(/^(.+?)\s*\(you\)/i);
    if (m && isHumanParticipantName(m[1])) {
      debugLog("self", "aria-label (you)", m[1].trim());
      return m[1].trim();
    }
  }
  return null;
}

function cleanRosterDisplayName(raw) {
  return normalizeText(raw)
    .replace(/\s*\(you\)\s*$/i, "")
    .replace(/\s*\(host\)\s*$/i, "")
    .replace(/\s*\(presenter\)\s*$/i, "");
}

function scanVisibleNameLabels() {
  const stage =
    document.querySelector("[data-allocation-index]")?.closest("div") ||
    document.querySelector("[jsname='EaZ7Sd']") ||
    document.body;
  for (const el of stage.querySelectorAll("span.notranslate, div.notranslate, [data-self-name]")) {
    if (isInsideToolbarOrControls(el)) continue;
    const t = cleanRosterDisplayName(el.textContent || el.getAttribute("data-self-name"));
    if (t && isHumanParticipantName(t)) addRosterParticipant(t, "visible_label");
  }
}

/** Read [data-participant-id] tiles on the meeting stage (not toolbar). */
function refreshRosterParticipants(forceLog) {
  const preserved = rosterParticipants.map((p) => ({ ...p }));
  rosterParticipants = [];

  const tiles = document.querySelectorAll("[data-participant-id]");
  debugLog("roster", `scanning ${tiles.length} participant tiles`);
  for (const tile of tiles) {
    if (isInsideToolbarOrControls(tile)) continue;
    const name = extractNameFromParticipantTile(tile);
    if (name) addRosterParticipant(name, "participant_tile");
  }

  scanVisibleNameLabels();
  for (const p of preserved) {
    addRosterParticipant(p.name, p.source);
  }
  if (selfName) addRosterParticipant(selfName, "google_account");
  applySoloMeetingPolicy();

  if (forceLog) {
    debugLog("roster", `attendees (${rosterParticipants.length})`, rosterParticipants.map((p) => p.name).join(", "));
  }
}

function findActiveSpeakerName() {
  if (isSoloMeeting()) return selfName;

  const speaking = [];
  for (const tile of document.querySelectorAll("[data-participant-id]")) {
    const speakingFlag =
      tile.getAttribute("data-is-speaking") === "true" ||
      tile.querySelector("[data-is-speaking='true']") ||
      tile.matches?.("[data-is-speaking='true']");
    if (!speakingFlag) continue;

    const name = extractNameFromParticipantTile(tile);
    if (!name || !isHumanParticipantName(name)) continue;
    if (rosterNameSet().size > 0 && !rosterHasName(name)) continue;

    const aria = normalizeText(tile.getAttribute("aria-label") || "");
    const isYou = /\b\(you\)\b/i.test(aria) || (selfName && name === selfName);
    speaking.push({ name, isYou });
  }

  if (!speaking.length) return null;
  const you = speaking.find((s) => s.isYou);
  if (you) return you.name;
  return speaking[0].name;
}

function pushSpeakerEvent(name, source, tOverride) {
  if (isSoloMeeting() && selfName && !hasMultipleSpeakersInCaptions()) {
    name = selfName;
    source = "solo_meeting";
  }

  const n = normalizeText(name);
  if (!isHumanParticipantName(n)) return;
  if (!isSoloMeeting() && rosterNameSet().size > 0 && !rosterHasName(n)) {
    addRosterParticipant(n, "speaker_late");
  }

  const t = typeof tOverride === "number" ? tOverride : relMs();
  const last = collectedSpeakerEvents[collectedSpeakerEvents.length - 1];
  if (last && last.name === n && Math.abs(t - last.t) < 2000) return;

  if (lastSpeakerName !== n) {
    debugLog("active_speaker_changed", `${(t / 1000).toFixed(1)}s -> ${n}`, source);
  }
  collectedSpeakerEvents.push({ t, name: n, source });
  lastSpeakerName = n;
  lastEmittedAt = Date.now();
  debugLog("speaker_event", `${(t / 1000).toFixed(1)}s ${n}`, source);
}

/** Speaker label on its own line in Meet transcript (not a sentence). */
function isSpeakerOnlyLine(line) {
  const n = normalizeText(line);
  if (!n || n.length > 45) return false;
  if (isMeetUiChromeLabel(n)) return false;
  if (/^you$/i.test(n)) return true;
  if (/\b(the|and|because|having|everything|mute|afternoon|evening|hello|thank)\b/i.test(n)) {
    return false;
  }
  if (/[.!?]/.test(n)) return false;
  return isHumanParticipantName(n);
}

function countSpeakerLinesInRoots(roots) {
  let count = 0;
  for (const root of roots) {
    for (const line of parseCaptionLinesFromRoot(root)) {
      if (isSpeakerOnlyLine(line) && resolveCaptionSpeaker(line)) count++;
    }
  }
  return count;
}

function hasMultipleSpeakersInCaptions() {
  return countSpeakerLinesInRoots(findMeetCaptionRoots()) >= 2;
}

function ensureMinimumSpeakerPayload() {
  if (!selfName) selfName = findSelfNameFromAccount();
  if (!rosterParticipants.length && selfName) {
    rosterParticipants = [{ name: selfName, source: "minimum_guarantee" }];
  }
  if (!collectedSpeakerEvents.length && isSoloMeeting()) {
    const fallback =
      selfName || (rosterParticipants[0] && rosterParticipants[0].name) || null;
    if (fallback) {
      collectedSpeakerEvents = [{ t: 0, name: fallback, source: "minimum_guarantee" }];
      debugLog("guarantee", "minimum speaker event (solo)", fallback);
    }
  }
}

function resolveCaptionSpeaker(label) {
  const raw = normalizeText(label);
  if (!raw) return null;
  if (/^you$/i.test(raw)) return selfName;
  if (isMeetUiChromeLabel(raw)) return null;
  if (!isHumanParticipantName(raw)) return null;
  return raw;
}

function scrubCollectedEvents() {
  collectedSpeakerEvents = collectedSpeakerEvents.filter((e) =>
    isHumanParticipantName(e.name),
  );
}

function reconcileRecorderMonologue() {
  if (!selfName) return;
  const names = new Set(collectedSpeakerEvents.map((e) => e.name));
  if (names.size !== 1 || names.has(selfName)) return;

  let otherCaptionSpeakers = 0;
  for (const root of findMeetCaptionRoots()) {
    for (const line of parseCaptionLinesFromRoot(root)) {
      if (!isSpeakerOnlyLine(line)) continue;
      const sp = resolveCaptionSpeaker(line);
      if (sp && sp !== selfName) otherCaptionSpeakers++;
    }
  }
  if (otherCaptionSpeakers > 0) return;

  collectedSpeakerEvents = [{ t: 0, name: selfName, source: "recorder_monologue_fix" }];
}

function rescaleTranscriptBlockEvents() {
  const blocks = collectedSpeakerEvents.filter((e) => e.source === "transcript_block");
  if (!blocks.length) return;
  const duration = Math.max(relMs(), 1000);
  const n = blocks.length;
  for (let i = 0; i < n; i++) {
    blocks[i].t = n === 1 ? 0 : Math.floor((i / n) * duration);
  }
}

function finalizeSpeakerData() {
  if (!selfName) selfName = findSelfNameFromAccount();

  pollCaptionsForSpeakers(true);
  parseMeetTranscriptSpeakerBlocks(true);
  rescaleTranscriptBlockEvents();
  reconcileRecorderMonologue();
  scrubCollectedEvents();

  refreshRosterParticipants(true);
  applySoloMeetingPolicy();

  const otherCount = countOtherParticipantsOnPage();
  const rosterOthers = rosterParticipants.filter((p) => p.name !== selfName);
  debugLog(
    "finalize",
    `other tiles=${otherCount} roster=${JSON.stringify(rosterParticipants.map((p) => p.name))}`,
  );

  if (
    isSoloMeeting() &&
    selfName &&
    rosterOthers.length === 0 &&
    !hasMultipleSpeakersInCaptions() &&
    countSpeakerLinesInRoots(findMeetCaptionRoots()) < 2
  ) {
    collectedSpeakerEvents = [{ t: 0, name: selfName, source: "solo_meeting_all" }];
    rosterParticipants = [{ name: selfName, source: "google_account_solo" }];
    debugLog("finalize", "solo — all speech attributed to", selfName);
    return;
  }

  const rosterSize = rosterNameSet().size;
  collectedSpeakerEvents = collectedSpeakerEvents.filter((e) => {
    if (!isHumanParticipantName(e.name)) return false;
    if (rosterSize === 0) return true;
    if (rosterHasName(e.name)) return true;
    addRosterParticipant(e.name, "caption_late");
    return true;
  });

  const hasCaptionSpeakers = collectedSpeakerEvents.some((e) =>
    ["transcript_block", "caption", "captions_final"].includes(e.source),
  );
  const active = findActiveSpeakerName();
  if (active && !hasCaptionSpeakers) {
    pushSpeakerEvent(active, "active_speaker_final");
  }

  if (!collectedSpeakerEvents.length && selfName) {
    pushSpeakerEvent(selfName, "self_fallback");
  }

  if (!collectedSpeakerEvents.length && rosterParticipants.length) {
    pushSpeakerEvent(rosterParticipants[0].name, "roster_fallback");
  }

  debugLog("finalize", `events=${collectedSpeakerEvents.length}`, JSON.stringify(collectedSpeakerEvents));
}

function poll() {
  if (!recordingStartMs) return;

  if (Date.now() - lastRosterRefresh > ROSTER_REFRESH_MS) {
    refreshRosterParticipants(false);
    lastRosterRefresh = Date.now();
  }

  if (!selfName) selfName = findSelfNameFromAccount();

  if (isSoloMeeting() && !hasMultipleSpeakersInCaptions()) {
    const captionText = findLatestCaptionText();
    if (captionText && captionText !== lastCaptionText && selfName) {
      lastCaptionText = captionText;
      pushSpeakerEvent(selfName, "solo_caption");
    }
    return;
  }

  pollCaptionsForSpeakers(false);
  parseMeetTranscriptSpeakerBlocks(false);

  const speaker = findActiveSpeakerName();
  if (speaker) {
    pushSpeakerEvent(speaker, "active_speaker");
  }
}

function isBottomToolbarRect(rect) {
  return rect.top > window.innerHeight - 100 && rect.height < 90;
}

function findMeetCaptionRoots() {
  const roots = [];
  const seen = new Set();

  const tryAdd = (el) => {
    if (!el || seen.has(el)) return;
    if (isInsideToolbarOrControls(el)) return;
    const rect = el.getBoundingClientRect();
    if (rect.width < 80 || rect.height < 16) return;
    if (isBottomToolbarRect(rect)) return;

    const text = normalizeText(el.innerText || "").slice(0, 800);
    if (isMeetUiChromeLabel(text)) return;
    const hasYou = /\byou\b/i.test(text);
    const hasNameLine = /\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b/.test(text);
    const hasSpeech = text.length > 40;
    if (!hasSpeech && !hasYou && !hasNameLine) return;

    seen.add(el);
    roots.push(el);
  };

  for (const sel of [
    '[aria-live="polite"]',
    '[aria-live="assertive"]',
    '[role="log"]',
    '[jsname="dsyhDe"]',
    '[class*="iOzk7"]',
    '[class*="a4cQT"]',
  ]) {
    for (const el of document.querySelectorAll(sel)) {
      tryAdd(el);
    }
  }

  // Only climb from "You" or full participant names — not toolbar tokens (More, Add).
  for (const el of document.querySelectorAll("span, div")) {
    const t = normalizeText(el.textContent);
    if (t === "You" || (selfName && t === selfName)) {
      let p = el;
      for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
        tryAdd(p);
      }
    }
  }

  debugLog("captions", `found ${roots.length} transcript roots`);
  return roots;
}

function parseCaptionLinesFromRoot(root) {
  const lines = [];
  const raw = normalizeText(root.innerText || root.textContent || "");
  if (!raw) return lines;
  for (const line of raw.split(/\n+/).map((l) => normalizeText(l)).filter(Boolean)) {
    lines.push(line);
  }
  for (const el of root.querySelectorAll("div, span")) {
    if (isInsideToolbarOrControls(el)) continue;
    const t = normalizeText(el.textContent);
    if (!t || t.length > 80) continue;
    if (el.children.length > 2) continue;
    lines.push(t);
  }
  return lines;
}

function parseMeetTranscriptSpeakerBlocks(finalPass) {
  if (!recordingStartMs && !finalPass) return;

  const roots = findMeetCaptionRoots();
  let blockIdx = 0;

  for (const root of roots) {
    const lines = [...new Set(parseCaptionLinesFromRoot(root))];
    debugLog("transcript", `lines=${lines.length}`, lines.slice(0, 8).join(" | "));

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!isSpeakerOnlyLine(line)) continue;

      const speaker = resolveCaptionSpeaker(line);
      if (!speaker) continue;

      const t = finalPass ? blockIdx * 3000 : relMs();
      addRosterParticipant(speaker, "transcript_block");
      pushSpeakerEvent(speaker, "transcript_block", t);
      blockIdx++;

      while (i + 1 < lines.length && !isSpeakerOnlyLine(lines[i + 1])) {
        i++;
      }
    }
  }
}

function pollCaptionsForSpeakers(finalPass) {
  if (!recordingStartMs && !finalPass) return;

  const roots = findMeetCaptionRoots();
  for (const root of roots) {
    const lines = parseCaptionLinesFromRoot(root);
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (isSpeakerOnlyLine(line)) continue;

      let speaker = null;
      let m = line.match(/^([A-Za-z][a-zA-Z'-]+(?:\s+[A-Za-z][a-zA-Z'-]+){0,3})\s*:\s*(.+)$/);
      if (m) speaker = resolveCaptionSpeaker(m[1]);

      if (!speaker) {
        m = line.match(/^(You)\s+(.+)$/i);
        if (m) speaker = resolveCaptionSpeaker(m[1]);
      }

      if (!speaker) continue;
      addRosterParticipant(speaker, "caption");
      pushSpeakerEvent(speaker, "captions_final");
    }
  }
}

function findLatestCaptionText() {
  for (const el of document.querySelectorAll("[aria-live], [role='log']")) {
    const t = normalizeText(el.textContent || "");
    if (t && t.length < 800) return t;
  }
  return "";
}

setInterval(poll, POLL_MS);
