const result = document.querySelector("#result");
const video = document.querySelector("#camera");
const startCamera = document.querySelector("#start-camera");
const stopCamera = document.querySelector("#stop-camera");
const savePack = document.querySelector("#save-pack");
const productMatch = document.querySelector("#product-match");
const quantity = document.querySelector("#quantity");
const saveResult = document.querySelector("#save-result");
const packsContainer = document.querySelector("#packs");
const productCategory = document.querySelector("#product-category");
const medikeepLink = document.querySelector("#medikeep-link");
const medikeepMedication = document.querySelector("#medikeep-medication");
const medikeepSection = document.querySelector("#medikeep-section");
const medikeepMedicines = document.querySelector("#medikeep-medicines");
const authGate = document.querySelector("#auth-gate");
const appContent = document.querySelector("#app-content");
const authForm = document.querySelector("#auth-form");
const authResult = document.querySelector("#auth-result");
const medikeepSetup = document.querySelector("#medikeep-setup");
const medikeepForm = document.querySelector("#medikeep-form");
const medikeepSetupResult = document.querySelector("#medikeep-setup-result");
let scanControls;
let currentRaw;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

function showSignedOut() {
  authGate.hidden = false;
  appContent.hidden = true;
}

async function showSignedIn(status) {
  authGate.hidden = true;
  appContent.hidden = false;
  document.querySelector("#welcome").textContent = `Signed in as ${status.user.email}. Scan a pack, check its expiry, then keep your stock up to date.`;
  await Promise.all([loadPacks(), loadMediKeep()]);
  await loadMediKeepConnection(status.medikeep_connected);
}

async function loadMediKeepConnection(knownConnected = false) {
  try {
    const connection = await api("/api/v1/medikeep/connection");
    medikeepSetup.hidden = connection.configured;
    if (connection.configured) {
      document.querySelector("#medikeep-url").value = connection.base_url || "";
    }
  } catch {
    medikeepSetup.hidden = !knownConnected;
  }
}

async function authenticate(mode) {
  const email = document.querySelector("#auth-email").value.trim();
  const password = document.querySelector("#auth-password").value;
  authResult.textContent = mode === "register" ? "Creating account…" : "Signing in…";
  try {
    const status = await api(`/api/auth/${mode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    await showSignedIn(status);
  } catch (error) {
    authResult.textContent = error.message;
  }
}

function button(label, className = "") {
  const element = document.createElement("button");
  element.textContent = label;
  if (className) element.className = className;
  return element;
}

function medicineSummary(medication) {
  return [medication.dosage, medication.route, medication.frequency].filter(Boolean).join(" · ");
}

async function loadMediKeep() {
  try {
    const medications = await api("/api/v1/medikeep/active-medications");
    medikeepSection.hidden = false;
    medikeepMedicines.replaceChildren();
    if (!medications.length) {
      medikeepMedicines.textContent = "No active MediKeep medicines found.";
      return;
    }
    medications.forEach(medication => {
      const card = document.createElement("div");
      card.className = "medikeep-card";
      const description = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = medication.name;
      const details = document.createElement("p");
      details.textContent = medicineSummary(medication) || "Active medication";
      description.append(title, details);
      const importButton = button("Import/link", "secondary compact");
      importButton.addEventListener("click", async () => {
        importButton.disabled = true;
        try {
          const imported = await api(`/api/v1/medikeep/import/${medication.id}`, { method: "POST" });
          importButton.textContent = imported.created ? "Imported" : "Linked";
          await loadPacks();
        } catch (error) {
          alert(`Could not import: ${error.message}`);
          importButton.disabled = false;
        }
      });
      card.append(description, importButton);
      medikeepMedicines.append(card);
    });
  } catch {
    // MediKeep is deliberately optional: do not show a broken integration
    // panel when its connection has not been configured.
    medikeepSection.hidden = true;
  }
}

async function showMediKeepSuggestions(productName) {
  medikeepMedication.replaceChildren();
  medikeepLink.hidden = true;
  try {
    const suggestions = await api(`/api/v1/medikeep/suggestions?name=${encodeURIComponent(productName)}`);
    if (!suggestions.length) return;
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "No MediKeep link — OTC, new prescription or decide later";
    medikeepMedication.append(none);
    suggestions.forEach(medication => {
      const option = document.createElement("option");
      option.value = String(medication.id);
      option.textContent = `${medication.name}${medicineSummary(medication) ? ` — ${medicineSummary(medication)}` : ""}`;
      medikeepMedication.append(option);
    });
    medikeepLink.hidden = false;
  } catch {
    medikeepLink.hidden = true;
  }
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
    productCategory.value = "medicine";
    await showMediKeepSuggestions(match.name);
    savePack.hidden = false;
  } catch (error) {
    result.textContent = error.message;
  }
}

document.querySelector("#parse").addEventListener("click", () => findProduct(document.querySelector("#code").value.trim()));
document.querySelector("#refresh-packs").addEventListener("click", loadPacks);
document.querySelector("#refresh-medikeep").addEventListener("click", loadMediKeep);
document.querySelector("#manage-medikeep").addEventListener("click", () => {
  medikeepSetup.hidden = false;
  medikeepSetup.scrollIntoView({ behavior: "smooth", block: "start" });
});

authForm.addEventListener("submit", event => {
  event.preventDefault();
  authenticate("login");
});
document.querySelector("#create-account").addEventListener("click", () => authenticate("register"));
document.querySelector("#sign-out").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  authForm.reset();
  authResult.textContent = "";
  showSignedOut();
});

medikeepForm.addEventListener("submit", async event => {
  event.preventDefault();
  medikeepSetupResult.textContent = "Testing MediKeep connection…";
  const payload = {
    base_url: document.querySelector("#medikeep-url").value.trim(),
    patient_id: Number(document.querySelector("#medikeep-patient-id").value),
    token: document.querySelector("#medikeep-token").value.trim() || null,
    username: document.querySelector("#medikeep-username").value.trim() || null,
    password: document.querySelector("#medikeep-password").value || null,
  };
  try {
    await api("/api/v1/medikeep/connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    medikeepSetupResult.textContent = "MediKeep connected. Your settings are encrypted in DoseKeep.";
    document.querySelector("#medikeep-token").value = "";
    document.querySelector("#medikeep-password").value = "";
    medikeepSetup.hidden = true;
    await loadMediKeep();
  } catch (error) {
    medikeepSetupResult.textContent = error.message;
  }
});

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
      body: JSON.stringify({
        raw: currentRaw,
        quantity_initial: Number(quantity.value),
        category: productCategory.value,
        medikeep_medication_id: medikeepMedication.value ? Number(medikeepMedication.value) : null,
      }),
    });
    saveResult.textContent = created.created ? `Saved ${created.product.name}.` : "This scanned pack is already recorded.";
    await loadPacks();
  } catch (error) {
    saveResult.textContent = error.message;
  }
});

async function bootstrap() {
  try {
    const status = await api("/api/auth/status");
    if (status.authenticated) {
      await showSignedIn(status);
    } else {
      showSignedOut();
    }
  } catch {
    authResult.textContent = "DoseKeep could not load. Check the server connection and try again.";
    showSignedOut();
  }
}

bootstrap();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js?v=0.5.0");
