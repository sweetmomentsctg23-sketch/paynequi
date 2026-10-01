const loginPanel = document.querySelector("#login-panel");
const loginForm = document.querySelector("#login-form");
const dashboard = document.querySelector("#dashboard");
const loginMessage = document.querySelector("#login-message");
const adminMessage = document.querySelector("#admin-message");
const visitsContainer = document.querySelector("#visits");
const photoDialog = document.querySelector("#photo-dialog");
let csrfToken = "";
let refreshTimer;
let previousVisitStates = new Map();
let audioEnabled = false;
let visitsLoaded = false;
let audioContext;
let refreshInProgress = false;

function setLoginMessage(value) { loginMessage.textContent = value || ""; }

async function api(url, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const response = await fetch(url, { ...options, headers });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "No se pudo completar la solicitud.");
  return body;
}

async function initialize() {
  try {
    const status = await api("/api/admin/session");
    if (status.authenticated) {
      csrfToken = status.csrf_token;
      showDashboard();
    } else {
      const config = await api("/api/admin/readiness");
      if (!config.ready) {
        document.querySelector("#login-description").textContent =
          "El acceso por Telegram todavía no está configurado en el servidor.";
        document.querySelector("#request-code").disabled = true;
      }
    }
  } catch (error) {
    setLoginMessage(error.message);
  }
}

function showDashboard() {
  loginPanel.hidden = true;
  dashboard.hidden = false;
  refreshVisits();
  refreshTimer = setInterval(refreshVisits, 3000);
}

document.querySelector("#request-code").addEventListener("click", async () => {
  setLoginMessage("");
  try {
    await api("/api/admin/request-code", { method: "POST", body: "{}" });
    loginForm.hidden = false;
    setLoginMessage("Código enviado. Vence en 5 minutos.");
  } catch (error) { setLoginMessage(error.message); }
});

loginForm.addEventListener("submit", async event => {
  event.preventDefault();
  setLoginMessage("");
  try {
    const result = await api("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({ code: document.querySelector("#admin-code").value })
    });
    csrfToken = result.csrf_token;
    showDashboard();
  } catch (error) { setLoginMessage(error.message); }
});

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

const iconMarkup = {
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>',
  retake: '<path d="M3 12a9 9 0 0 1 15.4-6.4L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.4 6.4L3 16"/><path d="M3 21v-5h5"/>',
  approve: '<path d="m5 12 4 4L19 6"/>',
  reject: '<path d="m18 6-12 12M6 6l12 12"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  unlock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 7-2"/>',
  delete: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v5M14 11v5"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
};

function button(label, icon, callback, className = "") {
  const node = element("button", undefined, `icon-action ${className}`.trim());
  node.type = "button";
  node.title = label;
  node.setAttribute("aria-label", label);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = iconMarkup[icon];
  node.append(svg, element("span", label));
  node.addEventListener("click", callback);
  return node;
}

async function notify() {
  if (!audioEnabled) return false;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {
    adminMessage.textContent = "Este navegador no permite alertas de sonido.";
    return false;
  }
  try {
    if (!audioContext || audioContext.state === "closed") audioContext = new AudioContextClass();
    if (audioContext.state === "suspended") await audioContext.resume();
    const playTone = (frequency, startAt) => {
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0.001, startAt);
      gain.gain.exponentialRampToValueAtTime(0.16, startAt + 0.025);
      gain.gain.exponentialRampToValueAtTime(0.001, startAt + 0.32);
      oscillator.connect(gain);
      gain.connect(audioContext.destination);
      oscillator.start(startAt);
      oscillator.stop(startAt + 0.34);
    };
    const startAt = audioContext.currentTime;
    playTone(740, startAt);
    playTone(988, startAt + 0.16);
    return true;
  } catch (error) {
    adminMessage.textContent = `No se pudo reproducir el sonido: ${error.message}`;
    return false;
  }
}

function showPhoto(visit) {
  const url = `/api/admin/visits/${encodeURIComponent(visit.id)}/photo`;
  document.querySelector("#large-photo").src = url;
  const phone = visit.phone.replace(/[^\d]/g, "").slice(-4);
  const workerCode = (visit.worker_code || "").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 32);
  const folder = workerCode ? `trabajador-${workerCode}` : `visita-${phone}`;
  const filename = `selfie-${workerCode || phone}-${visit.id.slice(0, 8)}.jpg`;
  const fallback = document.querySelector("#download-fallback");
  fallback.href = url;
  fallback.download = filename;
  document.querySelector("#save-photo").dataset.url = url;
  document.querySelector("#save-photo").dataset.folder = folder;
  document.querySelector("#save-photo").dataset.filename = filename;
  photoDialog.showModal();
}

function renderVisit(visit) {
  const card = element("article", undefined, "visit-card");
  const photoButton = button(
    visit.has_photo ? "Selfie" : "Sin selfie",
    "image",
    () => visit.has_photo && showPhoto(visit),
    "photo-action"
  );
  photoButton.disabled = !visit.has_photo;
  if (visit.has_photo) {
    const thumbnail = element("img");
    thumbnail.src = `/api/admin/visits/${encodeURIComponent(visit.id)}/photo`;
    thumbnail.alt = "";
    photoButton.prepend(thumbnail);
  }

  const details = element("div", undefined, "visit-details");
  const heading = element("h2", visit.phone);
  const phoneLabel = element("span", "Celular", "visit-phone-label");
  const site = element("p", visit.site || "Sede sin indicar", "visit-site");
  const status = element("span", visit.state_label, "visit-status");
  const statusLine = element("div", undefined, "visit-status-line");
  statusLine.append(status);
  details.append(phoneLabel, heading, site, statusLine);

  const presenceText = visit.left ? "Salió" : visit.online ? "" : "Sin señal";
  const presence = element("span", undefined, `presence-indicator ${visit.left ? "left" : visit.online ? "online" : "offline"}`);
  presence.title = visit.left ? "La persona indicó que salió" : visit.online ? "Actividad reciente" : "Sin actividad reciente";
  presence.setAttribute("role", "img");
  presence.setAttribute("aria-label", presence.title);
  const presenceLabel = element("span", presenceText, "presence-label");
  const presenceGroup = element("span", undefined, "presence-group");
  presenceGroup.append(presence, presenceLabel);
  statusLine.append(presenceGroup);
  const timestamp = new Date(visit.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  details.append(element("p", `Ingreso ${timestamp}`, "visit-time"));

  card.append(photoButton, details);
  if (visit.worker_code) {
    const code = element("code", visit.worker_code, "worker-code-value");
    const codeField = element("div", undefined, "worker-code-field");
    codeField.append(element("span", "Código", "worker-code-label"), code);
    card.append(codeField);
  }
  if (visit.has_photo) {
    card.querySelector(".photo-action").title = "Ver selfie";
  }
  const actions = element("div", undefined, "visit-actions");
  if (!visit.blocked && visit.state === "pending_details") {
    actions.append(
      button("Selfie", "approve", () => decide(visit.id, "details_ok"), "approve-action"),
      button("Relogin", "retake", () => decide(visit.id, "details_retry"), "quiet-action")
    );
  } else if (!visit.blocked && visit.state === "pending_photo") {
    actions.append(
      button("Código", "approve", () => decide(visit.id, "photo_ok"), "approve-action"),
      button("Repetir selfie", "retake", () => decide(visit.id, "photo_retry"), "quiet-action")
    );
  } else if (!visit.blocked && visit.state === "pending_code") {
    actions.append(
      button("Aprobar", "approve", () => decide(visit.id, "code_ok"), "approve-action"),
      button("Rechazar código", "reject", () => decide(visit.id, "code_reject"), "quiet-action")
    );
  }
  const blockButton = button(
    visit.blocked ? "Desbloquear" : "Bloquear",
    visit.blocked ? "unlock" : "lock",
    () => toggleBlock(visit.id, !visit.blocked),
    visit.blocked ? "quiet-action" : "warning-action"
  );
  actions.append(blockButton);
  actions.append(button("Eliminar", "delete", () => removeVisit(visit.id), "delete-action"));
  card.append(actions);
  if (visit.blocked) card.classList.add("is-blocked");
  return card;
}

async function refreshVisits() {
  if (refreshInProgress) return;
  refreshInProgress = true;
  try {
    const result = await api("/api/admin/visits");
    const nextStates = new Map(result.visits.map(visit => [
      visit.id,
      { state: visit.state, hasPhoto: visit.has_photo }
    ]));
    const changes = result.visits.filter(visit => {
      const previous = previousVisitStates.get(visit.id);
      return visitsLoaded && (
        previous === undefined ||
        (!previous.hasPhoto && visit.has_photo) ||
        (previous.state !== visit.state && ["pending_details", "pending_photo", "pending_code"].includes(visit.state))
      );
    });
    previousVisitStates = nextStates;
    visitsLoaded = true;
    visitsContainer.replaceChildren(...result.visits.map(renderVisit));
    if (changes.length) {
      adminMessage.textContent = changes.length === 1
        ? "Nueva solicitud para revisar."
        : `${changes.length} solicitudes nuevas para revisar.`;
      await notify();
    } else {
      adminMessage.textContent = result.visits.length
        ? `${result.visits.length} solicitudes · Actualización automática`
        : "No hay visitas registradas.";
    }
  } catch (error) {
    adminMessage.textContent = error.message;
    if (error.message.includes("iniciar sesión")) {
      clearInterval(refreshTimer);
      dashboard.hidden = true;
      loginPanel.hidden = false;
    }
  } finally {
    refreshInProgress = false;
  }
}

async function decide(id, action) {
  try {
    await api(`/api/admin/visits/${encodeURIComponent(id)}/decision`, {
      method: "POST", body: JSON.stringify({ action })
    });
    await refreshVisits();
  } catch (error) { adminMessage.textContent = error.message; }
}

async function toggleBlock(id, blocked) {
  try {
    await api(`/api/admin/visits/${encodeURIComponent(id)}/block`, {
      method: "POST", body: JSON.stringify({ blocked })
    });
    await refreshVisits();
  } catch (error) { adminMessage.textContent = error.message; }
}

async function removeVisit(id) {
  if (!confirm("Esto eliminará el teléfono, la selfie y el registro. ¿Continuar?")) return;
  try {
    await api(`/api/admin/visits/${encodeURIComponent(id)}`, { method: "DELETE" });
    await refreshVisits();
  } catch (error) { adminMessage.textContent = error.message; }
}

document.querySelector("#sound-button").addEventListener("click", event => {
  const soundButton = event.currentTarget;
  audioEnabled = !audioEnabled;
  soundButton.classList.toggle("is-enabled", audioEnabled);
  soundButton.setAttribute("aria-pressed", String(audioEnabled));
  soundButton.title = audioEnabled ? "Silenciar alertas" : "Activar alertas sonoras";
  soundButton.setAttribute("aria-label", soundButton.title);
  soundButton.querySelector("span").textContent = audioEnabled ? "Sonido activo" : "Sonido";
  if (audioEnabled) notify();
  else if (audioContext && audioContext.state === "running") audioContext.suspend();
});
document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    await api("/api/admin/logout", { method: "POST", body: "{}" });
    clearInterval(refreshTimer);
    if (audioContext && audioContext.state !== "closed") audioContext.close();
    audioEnabled = false;
    visitsLoaded = false;
    previousVisitStates.clear();
    csrfToken = "";
    dashboard.hidden = true;
    loginPanel.hidden = false;
    loginForm.hidden = true;
    setLoginMessage("Sesión cerrada.");
  } catch (error) { adminMessage.textContent = error.message; }
});
document.querySelector("#close-photo").addEventListener("click", () => photoDialog.close());
document.querySelector("#save-photo").addEventListener("click", async event => {
  const button = event.currentTarget;
  const fallback = document.querySelector("#download-fallback");
  if (!window.showDirectoryPicker) {
    fallback.hidden = false;
    fallback.click();
    return;
  }
  try {
    const root = await window.showDirectoryPicker({ mode: "readwrite" });
    const folder = await root.getDirectoryHandle(button.dataset.folder, { create: true });
    const file = await folder.getFileHandle(button.dataset.filename, { create: true });
    const response = await fetch(button.dataset.url);
    if (!response.ok) throw new Error("No se pudo descargar la selfie.");
    const writable = await file.createWritable();
    await writable.write(await response.blob());
    await writable.close();
    adminMessage.textContent = `Selfie guardada en ${button.dataset.folder}.`;
  } catch (error) {
    if (error.name !== "AbortError") adminMessage.textContent = error.message;
  }
});

initialize();
