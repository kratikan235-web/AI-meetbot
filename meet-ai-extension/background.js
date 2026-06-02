// v2.0: recording runs in popup.js (mic works there). Background is minimal.
console.log("[background] Meet AI Notes v2.0 loaded");

chrome.runtime.onInstalled.addListener(() => {
  console.log("[background] extension installed/updated — use popup to record");
});
