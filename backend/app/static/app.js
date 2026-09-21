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
const medicinesContainer = document.querySelector("#medicines");
const authGate = document.querySelector("#auth-gate");
const appContent = document.querySelector("#app-content");
const authForm = document.querySelector("#auth-form");
const authResult = document.querySelector("#auth-result");
const medikeepSetup = document.querySelector("#medikeep-setup");
const medikeepForm = document.querySelector("#medikeep-form");
const medikeepSetupResult = document.querySelector("#medikeep-setup-result");
const administrationContainer = document.querySelector("#administration");
const pages = {
  medicines: document.querySelector("#page-medicines"),
  administration: document.querySelector("#page-administration"),
  settings: document.querySelector("#page-settings"),
};
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

function showPage(name) {
  Object.entries(pages).forEach(([pageName, element]) => { element.hidden = pageName !== name; });
  document.querySelectorAll("[data-page-target]").forEach(button => {
    button.classList.toggle("current", button.dataset.pageTarget === name);
  });
  if (name === "administration") loadAdministration();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function showSignedIn(status) {
  authGate.hidden = true;
  appContent.hidden = false;
  document.querySelector("#welcome").textContent = `Signed in as ${status.user.email}. Scan a pack, check its expiry, then keep your stock up to date.`;
  document.querySelector("#account-settings").textContent = `Signed in as ${status.user.email}. Your packs and any MediKeep connection are private to this DoseKeep account.`;
  showPage("medicines");
  await Promise.all([loadMedicines(), loadPacks(), loadMediKeep()]);
  await loadMediKeepConnection(status.medikeep_connected);
}

async function loadAdministration() {
  administrationContainer.textContent = "Loading administration plan…";
  try {
    const [medicines, scheduledDoses] = await Promise.all([
      api("/api/v1/dashboard/medicines"),
      api("/api/v1/doses/today"),
    ]);
    const current = medicines.filter(medicine => !medicine.medikeep_status || medicine.medikeep_status === "active");
    const planned = current.filter(medicine => medicine.regular_times.length || medicine.as_required);
    administrationContainer.replaceChildren();
    if (!planned.length) {
      administrationContainer.textContent = "No administration plans yet. Set a plan from the Medicines page.";
      return;
    }
    const currentProductIds = new Set(current.map(medicine => medicine.product_id).filter(Boolean));
    const groups = new Map([["morning", []], ["midday", []], ["evening", []], ["bedtime", []], ["prn", []]]);
    scheduledDoses
      .filter(dose => currentProductIds.has(dose.product_id))
      .forEach(dose => groups.get(dose.administration_time)?.push(dose));
    const headings = { morning: "Morning", midday: "Midday", evening: "Evening", bedtime: "Bedtime", prn: "As required (PRN)" };
    groups.forEach((dosesAtTime, time) => {
      if (time === "prn" || !dosesAtTime.length) return;
      const section = document.createElement("section");
      const heading = document.createElement("h3");
      heading.textContent = headings[time];
      section.append(heading);
      dosesAtTime.forEach(dose => {
        const item = document.createElement("article");
        item.className = "due-dose";
        const description = document.createElement("p");
        const details = [dose.dosage, dose.route].filter(Boolean).join(" · ");
        description.textContent = `${dose.medicine_name}${details ? ` — ${details}` : ""} · ${dose.status}`;
        item.append(description);
        if (["due", "snoozed"].includes(dose.status)) {
          const actions = document.createElement("div");
          actions.className = "dose-actions";
          [["Taken", "taken"], ["Skip", "skipped"], ["Snooze 15 min", "snooze"]].forEach(([label, action]) => {
            const control = button(label, action === "skipped" ? "outline" : "secondary compact");
            control.addEventListener("click", async () => {
              control.disabled = true;
              try {
                await api(`/api/v1/doses/${dose.id}/action`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ action }),
                });
                await Promise.all([loadAdministration(), loadMedicines(), loadPacks()]);
              } catch (error) {
                alert(`Could not record dose: ${error.message}`);
                control.disabled = false;
              }
            });
            actions.append(control);
          });
          item.append(actions);
        }
        section.append(item);
      });
      administrationContainer.append(section);
    });
    const prnMedicines = planned.filter(medicine => medicine.as_required);
    if (prnMedicines.length) {
      const section = document.createElement("section");
      const heading = document.createElement("h3");
      heading.textContent = headings.prn;
      section.append(heading);
      prnMedicines.forEach(medicine => {
        const item = document.createElement("article");
        item.className = "due-dose";
        const description = document.createElement("p");
        description.textContent = `${medicine.name}${medicine.prn_notes ? ` — ${medicine.prn_notes}` : ""}`;
        item.append(description);
        const control = button("Record PRN dose", "secondary compact");
        control.addEventListener("click", async () => {
          control.disabled = true;
          try {
            await api(`/api/v1/products/${medicine.product_id}/prn`, { method: "POST" });
            await Promise.all([loadAdministration(), loadMedicines(), loadPacks()]);
          } catch (error) {
            alert(`Could not record PRN dose: ${error.message}`);
            control.disabled = false;
          }
        });
        item.append(control);
        section.append(item);
      });
      const recorded = groups.get("prn") || [];
      if (recorded.length) {
        const history = document.createElement("p");
        history.className = "medicine-note";
        history.textContent = `Recorded today: ${recorded.map(dose => `${dose.medicine_name} at ${new Date(dose.actioned_at || dose.scheduled_for).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`).join("; ")}.`;
        section.append(history);
      }
      administrationContainer.append(section);
    }
    const unplanned = current.filter(medicine => !medicine.regular_times.length && !medicine.as_required);
    if (unplanned.length) {
      const note = document.createElement("p");
      note.className = "medicine-note";
      note.textContent = `${unplanned.length} current medicine${unplanned.length === 1 ? " has" : "s have"} no administration plan yet.`;
      administrationContainer.append(note);
    }
  } catch (error) {
    administrationContainer.textContent = `Could not load administration plan: ${error.message}`;
  }
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
  element.type = "button";
  element.textContent = label;
  if (className) element.className = className;
  return element;
}

function medicineSummary(medication) {
  return [medication.dosage, medication.route, medication.frequency].filter(Boolean).join(" · ");
}

async function loadMedicines() {
  medicinesContainer.textContent = "Loading medicines…";
  try {
    const medicines = await api("/api/v1/dashboard/medicines");
    medicinesContainer.replaceChildren();
    if (!medicines.length) {
      medicinesContainer.textContent = "No medicines yet. Scan a pack or connect MediKeep to import your active list.";
      return;
    }
    medicines.forEach(renderMedicine);
  } catch (error) {
    medicinesContainer.textContent = `Could not load medicines: ${error.message}`;
  }
}

function renderMedicine(medicine) {
  const card = document.createElement("article");
  card.className = "medicine-card";
  const title = document.createElement("h3");
  title.textContent = medicine.name;
  const directions = document.createElement("p");
  directions.className = "medicine-directions";
  directions.textContent = medicineSummary(medicine) || "Directions not recorded yet";
  const stock = document.createElement("div");
  stock.className = "medicine-stock";
  const amount = document.createElement("strong");
  amount.textContent = String(medicine.quantity_remaining + medicine.quantity_in_dosette);
  const caption = document.createElement("span");
  caption.textContent = medicine.active_pack_count ? "tablets/items available" : "no pack recorded";
  stock.append(amount, caption);
  const detail = document.createElement("p");
  detail.className = "medicine-note";
  const notes = [];
  if (medicine.regular_times.length) notes.push(`regular: ${medicine.regular_times.join(", ")}`);
  if (medicine.as_required) notes.push(`when required${medicine.prn_notes ? ` — ${medicine.prn_notes}` : ""}`);
  if (medicine.active_pack_count) notes.push(`${medicine.active_pack_count} active pack${medicine.active_pack_count === 1 ? "" : "s"}`);
  if (medicine.quantity_in_dosette) notes.push(`${medicine.quantity_in_dosette} in dosette`);
  if (medicine.linked_to_medikeep) notes.push("linked to MediKeep");
  if (medicine.medikeep_status && medicine.medikeep_status !== "active") notes.push(`MediKeep: ${medicine.medikeep_status}`);
  detail.textContent = notes.join(" · ") || "Scan a pack to start stock tracking.";
  card.append(title, directions, stock, detail);
  if (!medicine.product_id && medicine.medikeep_medication_id) {
    const importButton = button("Track in DoseKeep", "secondary compact");
    importButton.addEventListener("click", async () => {
      importButton.disabled = true;
      try {
        await api(`/api/v1/medikeep/import/${medicine.medikeep_medication_id}`, { method: "POST" });
        await Promise.all([loadMedicines(), loadPacks()]);
      } catch (error) {
        alert(`Could not add medicine: ${error.message}`);
        importButton.disabled = false;
      }
    });
    card.append(importButton);
  }
  if (medicine.product_id) {
    const scheduleButton = button(
      medicine.regular_times.length || medicine.as_required ? "Edit administration plan" : "Set administration plan",
      "secondary compact",
    );
    const editor = document.createElement("div");
    editor.className = "schedule-editor";
    editor.hidden = true;
    const label = document.createElement("strong");
    label.textContent = "Regular administration times";
    editor.append(label);
    const importedDirections = document.createElement("p");
    importedDirections.className = "hint";
    importedDirections.textContent = medicine.linked_to_medikeep
      ? `MediKeep directions: ${medicineSummary(medicine) || "No directions recorded in MediKeep"}`
      : "No MediKeep directions linked. Choose the times you use this medicine.";
    editor.append(importedDirections);
    const selected = new Set(medicine.regular_times);
    ["morning", "midday", "evening", "bedtime"].forEach(time => {
      const wrapper = document.createElement("label");
      wrapper.className = "schedule-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = time;
      input.checked = selected.has(time);
      wrapper.append(input, document.createTextNode(time[0].toUpperCase() + time.slice(1)));
      editor.append(wrapper);
    });
    const prnLabel = document.createElement("label");
    prnLabel.className = "schedule-option";
    const prn = document.createElement("input");
    prn.type = "checkbox";
    prn.checked = medicine.as_required;
    prnLabel.append(prn, document.createTextNode("As required (PRN)"));
    editor.append(prnLabel);
    const prnNotes = document.createElement("input");
    prnNotes.placeholder = "Optional PRN guidance, e.g. maximum dose";
    prnNotes.value = medicine.prn_notes || "";
    editor.append(prnNotes);
    const saveSchedule = button("Save administration plan", "compact");
    saveSchedule.addEventListener("click", async () => {
      saveSchedule.disabled = true;
      editor.hidden = true;
      editor.remove();
      const regular_times = [...editor.querySelectorAll('input[type="checkbox"]')]
        .filter(input => input !== prn && input.checked)
        .map(input => input.value);
      try {
        const updated = await api(`/api/v1/products/${medicine.product_id}/schedule`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ regular_times, as_required: prn.checked, prn_notes: prnNotes.value.trim() || null }),
        });
        medicine.regular_times = updated.regular_times;
        medicine.as_required = updated.as_required;
        medicine.prn_notes = updated.prn_notes;
        const updatedNotes = [];
        if (medicine.regular_times.length) updatedNotes.push(`regular: ${medicine.regular_times.join(", ")}`);
        if (medicine.as_required) updatedNotes.push(`when required${medicine.prn_notes ? ` — ${medicine.prn_notes}` : ""}`);
        if (medicine.active_pack_count) updatedNotes.push(`${medicine.active_pack_count} active pack${medicine.active_pack_count === 1 ? "" : "s"}`);
        if (medicine.quantity_in_dosette) updatedNotes.push(`${medicine.quantity_in_dosette} in dosette`);
        if (medicine.linked_to_medikeep) updatedNotes.push("linked to MediKeep");
        if (medicine.medikeep_status && medicine.medikeep_status !== "active") updatedNotes.push(`MediKeep: ${medicine.medikeep_status}`);
        detail.textContent = updatedNotes.join(" · ") || "Scan a pack to start stock tracking.";
        scheduleButton.textContent = "Edit administration plan";
      } catch (error) {
        alert(`Could not save administration plan: ${error.message}`);
        saveSchedule.disabled = false;
      }
    });
    editor.append(saveSchedule);
    scheduleButton.addEventListener("click", () => { editor.hidden = !editor.hidden; });
    card.append(scheduleButton, editor);
  }
  medicinesContainer.append(card);
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
          await Promise.all([loadMedicines(), loadPacks()]);
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
      await Promise.all([loadPacks(), loadMedicines()]);
    } catch (error) {
      alert(`Could not update count: ${error.message}`);
      saveCount.disabled = false;
    }
  });
  stockRow.append(countInput, saveCount);
  card.append(stockRow);

  const actions = document.createElement("div");
  actions.className = "dose-actions";
  const taken = button("Taken from pack");
  const skipped = button("Skipped dose", "outline");
  async function record(eventType, control) {
    control.disabled = true;
    try {
      await api(`/api/v1/packs/${pack.id}/events`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: eventType, quantity: 1 }),
      });
      await Promise.all([loadPacks(), loadMedicines()]);
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
document.querySelector("#refresh-medicines").addEventListener("click", loadMedicines);
document.querySelector("#refresh-administration").addEventListener("click", loadAdministration);
document.querySelector("#refresh-medikeep").addEventListener("click", loadMediKeep);
document.querySelectorAll("[data-page-target]").forEach(button => {
  button.addEventListener("click", () => showPage(button.dataset.pageTarget));
});
document.querySelector("#manage-medikeep").addEventListener("click", () => {
  medikeepSetup.hidden = false;
  showPage("settings");
});
document.querySelector("#edit-medikeep-settings").addEventListener("click", () => {
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
    await Promise.all([loadMediKeep(), loadMedicines(), loadAdministration()]);
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
    await Promise.all([loadPacks(), loadMedicines()]);
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
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js?v=0.16.0");
