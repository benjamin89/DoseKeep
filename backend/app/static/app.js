const result = document.querySelector("#result");
const video = document.querySelector("#camera");
const startCamera = document.querySelector("#start-camera");
const stopCamera = document.querySelector("#stop-camera");
const savePack = document.querySelector("#save-pack");
const productMatch = document.querySelector("#product-match");
const quantity = document.querySelector("#quantity");
const saveResult = document.querySelector("#save-result");
const packsContainer = document.querySelector("#packs");
let scanControls;
let currentRaw;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

function button(label, className = "") {
  const element = document.createElement("button");
  element.textContent = label;
  if (className) element.className = className;
  return element;
}

function formatEvent(event) {
  const labels = {
    taken_from_pack: "Taken from pack",
    taken: "Taken from dosette",
    dosette_fill: "Moved to dosette",
    skipped: "Skipped",
    disposed: "Disposed",
    correction: "Physical count corrected",
  };
  const when = new Date(event.occurred_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  return `${labels[event.event_type] || event.event_type} ×${event.quantity} · ${when}`;
}

async function loadEvents(packId, list) {
  try {
    const events = await api(`/api/v1/packs/${packId}/events`);
    list.replaceChildren();
    if (!events.length) {
      const empty = document.createElement("li");
      empty.textContent = "No recorded activity yet.";
      list.append(empty);
      return;
    }
    events.slice(0, 4).forEach(event => {
      const item = document.createElement("li");
      item.textContent = formatEvent(event);
      list.append(item);
    });
  } catch {
    list.hidden = true;
  }
}

async function loadPacks() {
  packsContainer.textContent = "Loading packs…";
  try {
    const [packs, products] = await Promise.all([api("/api/v1/packs"), api("/api/v1/products")]);
    const productNames = new Map(products.map(product => [product.id, product.name]));
    packsContainer.replaceChildren();
    const active = packs.filter(pack => pack.status === "active");
    if (!active.length) {
      packsContainer.textContent = "No packs yet. Scan your first pack above.";
      return;
    }
    active.forEach(pack => renderPack(pack, productNames.get(pack.product_id) || "Medicine pack"));
  } catch (error) {
    packsContainer.textContent = `Could not load packs: ${error.message}`;
  }
}

function renderPack(pack, productName) {
  const card = document.createElement("article");
  card.className = "pack-card";
  const title = document.createElement("h3");
  title.textContent = productName;
  card.append(title);

  const meta = document.createElement("p");
  meta.className = "pack-meta";
  const expiry = pack.expiry_date ? `Expires ${new Date(`${pack.expiry_date}T00:00:00`).toLocaleDateString()}` : "Expiry not recorded";
  meta.textContent = `${expiry}${pack.batch_number ? ` · Batch ${pack.batch_number}` : ""}`;
  card.append(meta);

  const stock = document.createElement("p");
  stock.className = "stock-number";
  stock.textContent = `${pack.quantity_remaining}`;
  card.append(stock);
  const caption = document.createElement("p");
  caption.className = "stock-caption";
  caption.textContent = `tablets/items physically in pack · original pack: ${pack.quantity_initial}`;
  card.append(caption);

  const stockRow = document.createElement("div");
  stockRow.className = "stock-row";
  const countInput = document.createElement("input");
  countInput.type = "number";
  countInput.min = "0";
  countInput.inputMode = "numeric";
  countInput.value = String(pack.quantity_remaining);
  countInput.setAttribute("aria-label", "Physical count remaining");
  const saveCount = button("Set physical count", "secondary compact");
  saveCount.addEventListener("click", async () => {
    saveCount.disabled = true;
    try {
      await api(`/api/v1/packs/${pack.id}/stock`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ quantity_remaining: Number(countInput.value) }),
      });
      await loadPacks();
    } catch (error) {
      alert(`Could not update count: ${error.message}`);
      saveCount.disabled = false;
    }
  });
  stockRow.append(countInput, saveCount);
  card.append(stockRow);

  const actions = document.createElement("div");
  actions.className = "dose-actions";
  const taken = button("Taken this evening");
  const skipped = button("Skipped this evening", "outline");
  async function record(eventType, control) {
    control.disabled = true;
    try {
      await api(`/api/v1/packs/${pack.id}/events`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: eventType, quantity: 1 }),
      });
      await loadPacks();
    } catch (error) {
      alert(`Could not record this: ${error.message}`);
      control.disabled = false;
    }
  }
  taken.addEventListener("click", () => record("taken_from_pack", taken));
  skipped.addEventListener("click", () => record("skipped", skipped));
  actions.append(taken, skipped);
  card.append(actions);

  const recent = document.createElement("ul");
  recent.className = "recent-events";
  card.append(recent);
  packsContainer.append(card);
  loadEvents(pack.id, recent);
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
document.querySelector("#refresh-packs").addEventListener("click", loadPacks);

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
    await loadPacks();
  } catch (error) {
    saveResult.textContent = error.message;
  }
});

loadPacks();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js");
