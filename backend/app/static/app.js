const result = document.querySelector("#result");
const video = document.querySelector("#camera");
const startCamera = document.querySelector("#start-camera");
const stopCamera = document.querySelector("#stop-camera");
const savePack = document.querySelector("#save-pack");
const productMatch = document.querySelector("#product-match");
const quantity = document.querySelector("#quantity");
const saveResult = document.querySelector("#save-result");
let scanControls;
let currentRaw;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

async function findProduct(raw) {
  currentRaw = raw;
  result.textContent = "Reading pack code…";
  savePack.hidden = true;
  try {
    const decoded = await api(`/api/v1/scan/parse?raw=${encodeURIComponent(raw)}`, { method: "POST" });
    const match = await api(`/api/v1/catalogue/fr/${decoded.gtin}`);
    result.textContent = JSON.stringify(decoded, null, 2);
    productMatch.innerHTML = `<strong>${match.name}</strong><br>${match.presentation || match.form || "French catalogue match"}<br><small>${match.holder || ""}</small>`;
    quantity.value = match.quantity_hint || "";
    savePack.hidden = false;
  } catch (error) {
    result.textContent = error.message;
  }
}

document.querySelector("#parse").addEventListener("click", () => findProduct(document.querySelector("#code").value.trim()));

startCamera.addEventListener("click", async () => {
  if (!window.isSecureContext) {
    result.textContent = "Camera access needs HTTPS (or localhost). Open DoseKeep through a secure URL, then try again.";
    return;
  }
  result.textContent = "Starting camera…";
  try {
    const { BrowserMultiFormatReader } = await import("https://cdn.jsdelivr.net/npm/@zxing/browser@0.1.5/+esm");
    const reader = new BrowserMultiFormatReader();
    video.hidden = false;
    startCamera.hidden = true;
    stopCamera.hidden = false;
    scanControls = await reader.decodeFromVideoDevice(undefined, video, (scan) => {
      if (scan) {
        const raw = scan.getText();
        document.querySelector("#code").value = raw;
        scanControls?.stop();
        video.hidden = true;
        startCamera.hidden = false;
        stopCamera.hidden = true;
        findProduct(raw);
      }
    });
  } catch (error) {
    result.textContent = `Camera unavailable: ${error.message}`;
    video.hidden = true;
    startCamera.hidden = false;
    stopCamera.hidden = true;
  }
});

stopCamera.addEventListener("click", () => {
  scanControls?.stop();
  video.hidden = true;
  startCamera.hidden = false;
  stopCamera.hidden = true;
});

document.querySelector("#save").addEventListener("click", async () => {
  saveResult.textContent = "Saving…";
  try {
    const created = await api("/api/v1/packs/from-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw: currentRaw, quantity_initial: Number(quantity.value) }),
    });
    saveResult.textContent = created.created ? `Saved ${created.product.name}.` : "This scanned pack is already recorded.";
  } catch (error) {
    saveResult.textContent = error.message;
  }
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js");
