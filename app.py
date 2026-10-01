import base64
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import cv2
import numpy as np
import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request, send_file, session
from PIL import Image, UnidentifiedImageError


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.environ.get("SELFIE_DATA_DIR", BASE_DIR / "data")).resolve()
IMAGE_DIR = DATA_DIR / "selfies"
DB_PATH = DATA_DIR / "visits.sqlite3"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PRESENCE_TIMEOUT_SECONDS = 60
OTP_TTL_SECONDS = 300
OTP_RESEND_SECONDS = 60
GLASSES_PROBABILITY_THRESHOLD = 0.5
_glasses_classifier = None
_glasses_classifier_lock = threading.Lock()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    TEMPLATES_AUTO_RELOAD=True,
    SEND_FILE_MAX_AGE_DEFAULT=0,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=((MAX_IMAGE_BYTES + 2) // 3) * 4 + 64 * 1024,
)

STATES = {
    "pending_details": "Revisión de datos",
    "details_rejected": "Datos por corregir",
    "details_ok": "Datos verificados",
    "pending_photo": "Revisión de selfie",
    "photo_rejected": "Selfie por repetir",
    "awaiting_code": "Esperando código",
    "pending_code": "Revisión de código",
    "code_rejected": "Código por volver a ingresar",
    "approved": "Pago aprobado",
    "denied": "Pago rechazado",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS visits (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                site TEXT NOT NULL DEFAULT '',
                image_name TEXT,
                worker_code TEXT,
                state TEXT NOT NULL,
                consented_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen REAL NOT NULL,
                blocked INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                left_at TEXT
            );
            CREATE INDEX IF NOT EXISTS visits_created_at ON visits(created_at);
            CREATE TABLE IF NOT EXISTS admin_codes (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                code_hash TEXT NOT NULL,
                expires_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                requested_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intake_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS intake_events_ip_time ON intake_events(ip_hash, created_at);
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(visits)")}
        if "site" not in columns:
            db.execute("ALTER TABLE visits ADD COLUMN site TEXT NOT NULL DEFAULT ''")
        if "left_at" not in columns:
            db.execute("ALTER TABLE visits ADD COLUMN left_at TEXT")
        if "consented_at" not in columns:
            db.execute("ALTER TABLE visits ADD COLUMN consented_at TEXT NOT NULL DEFAULT ''")


def admin_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return jsonify(error="Se requiere iniciar sesión."), 401
        if not app.secret_key:
            return jsonify(error="Configura FLASK_SECRET_KEY antes de iniciar."), 503
        return function(*args, **kwargs)

    return wrapped


def json_error(message, status):
    return jsonify(error=message), status


def decode_jpeg(data_url):
    if not isinstance(data_url, str):
        return None, "Envía una imagen JPEG válida."
    match = re.fullmatch(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
        return None, "Envía una imagen JPEG válida."
    try:
        image_bytes = base64.b64decode(match.group(1), validate=True)
        if not image_bytes:
            return None, "La imagen está vacía."
        if len(image_bytes) > MAX_IMAGE_BYTES:
            return None, "La imagen debe pesar menos de 5 MB."
        return image_bytes, ""
    except (ValueError, base64.binascii.Error):
        return None, "La imagen está dañada."


def inspect_face(image):
    if image.width < 320 or image.height < 320:
        return {"ready": False, "message": "Acerca un poco la cámara; la imagen debe tener al menos 320 × 320 píxeles."}
    if image.width > 4000 or image.height > 4000:
        return {"ready": False, "message": "La imagen supera el tamaño permitido."}

    pixels = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    detector = cv2.CascadeClassifier(
        str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    )
    faces = detector.detectMultiScale(
        pixels, scaleFactor=1.1, minNeighbors=5, minSize=(80, 80)
    )
    if len(faces) == 0:
        return {"ready": False, "message": "No se detectó un rostro. Mira al frente y busca más luz."}
    if len(faces) > 1:
        return {"ready": False, "message": "Solo debe aparecer una persona. Deja a los demás fuera del encuadre."}

    x, y, width, height = (int(value) for value in faces[0])
    frame_width, frame_height = image.size
    center_x = (x + width / 2) / frame_width
    center_y = (y + height / 2) / frame_height
    width_ratio = width / frame_width
    height_ratio = height / frame_height
    centered = 0.35 <= center_x <= 0.65 and 0.28 <= center_y <= 0.70
    face_size_ok = 0.16 <= width_ratio <= 0.62 and 0.25 <= height_ratio <= 0.82
    if not centered:
        return {"ready": False, "message": "Centra tu rostro dentro del óvalo."}
    if not face_size_ok:
        return {"ready": False, "message": "Ajusta la distancia: acerca o aleja el teléfono para que tu rostro quepa en el óvalo."}
    face = pixels[y:y + height, x:x + width]
    sharpness = cv2.Laplacian(face, cv2.CV_64F).var()
    if sharpness < 24:
        return {"ready": False, "message": "La imagen se ve borrosa. Mantén el teléfono quieto y limpia la cámara."}
    return {
        "ready": True,
        "message": "Rostro centrado. Mantén la posición para tomar la selfie.",
        "face_box": [x, y, width, height],
    }


def inspect_face_bytes(image_bytes):
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.verify()
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None, {"ready": False, "message": "El archivo no es una imagen válida."}
    return image, inspect_face(image)


def verify_face(image_bytes):
    image, result = inspect_face_bytes(image_bytes)
    if result["ready"]:
        try:
            if detect_glasses(image, result["face_box"]):
                return False, "Detectamos gafas. Quítatelas y toma otra selfie."
        except RuntimeError:
            return False, "No se pudo verificar el uso de gafas. Intenta nuevamente o pide ayuda al personal."
    return result["ready"], result["message"]


def detect_glasses(image, face_box):
    global _glasses_classifier
    with _glasses_classifier_lock:
        if _glasses_classifier is None:
            try:
                from glasses_detector import GlassesClassifier

                _glasses_classifier = GlassesClassifier(
                    kind="anyglasses", size="small", device="cpu"
                )
            except (ImportError, OSError, RuntimeError, ValueError, TypeError) as error:
                app.logger.error(
                    "No se pudo cargar el modelo local de gafas (%s).",
                    type(error).__name__,
                )
                raise RuntimeError("No se pudo cargar el detector de gafas.") from error

        x, y, width, height = face_box
        margin_x = int(width * 0.12)
        margin_y = int(height * 0.08)
        left = max(0, x - margin_x)
        top = max(0, y - margin_y)
        right = min(image.width, x + width + margin_x)
        bottom = min(image.height, y + height + margin_y)
        face_crop = image.crop((left, top, right, bottom)).convert("RGB")
        try:
            probability = _glasses_classifier.predict(face_crop, format="proba")
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            app.logger.error(
                "Falló la inferencia local de gafas (%s).",
                type(error).__name__,
            )
            raise RuntimeError("No se pudo analizar la imagen para detectar gafas.") from error
    if not isinstance(probability, (float, int)):
        raise RuntimeError("El detector de gafas devolvió un resultado no válido.")
    return float(probability) >= GLASSES_PROBABILITY_THRESHOLD


def serialize_visit(row):
    last_seen = float(row["last_seen"])
    left = bool(row["left_at"])
    online = not left and time.time() - last_seen <= PRESENCE_TIMEOUT_SECONDS
    return {
        "id": row["id"],
        "phone": row["phone"],
        "site": row["site"],
        "worker_code": row["worker_code"],
        "state": row["state"],
        "state_label": STATES.get(row["state"], row["state"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "online": online,
        "left": left,
        "blocked": bool(row["blocked"]),
        "has_photo": bool(row["image_name"]),
        "note": row["note"],
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/admin")
def admin():
    return render_template("admin.html")


@app.get("/api/admin/readiness")
def admin_readiness():
    return jsonify(ready=telegram_configured())


@app.post("/api/visits")
def create_visit():
    payload = request.get_json(silent=True) or {}
    phone, site, error = validate_visit_details(payload)
    if error:
        return json_error(error, 400)
    if payload.get("consent") is not True:
        return json_error("Debes aceptar el aviso de privacidad para continuar.", 400)
    visit_id = str(uuid.uuid4())
    now = utc_now()
    remote_ip = request.remote_addr or "unknown"
    ip_hash = hmac.new(app.secret_key.encode(), remote_ip.encode(), hashlib.sha256).hexdigest()
    with get_db() as db:
        db.execute("DELETE FROM intake_events WHERE created_at < ?", (time.time() - 600,))
        recent = db.execute(
            "SELECT COUNT(*) FROM intake_events WHERE ip_hash = ? AND created_at >= ?",
            (ip_hash, time.time() - 600),
        ).fetchone()[0]
        if recent >= 30:
            return json_error("Se alcanzó el límite de solicitudes. Intenta más tarde.", 429)
        db.execute(
            "INSERT INTO intake_events (ip_hash, created_at) VALUES (?, ?)",
            (ip_hash, time.time()),
        )
        blocked = db.execute(
            "SELECT 1 FROM visits WHERE phone = ? AND blocked = 1 LIMIT 1", (phone,)
        ).fetchone()
        if blocked:
            return json_error("Este teléfono no puede iniciar una solicitud. Contacta al personal.", 403)
        active = db.execute(
            "SELECT 1 FROM visits WHERE phone = ? AND blocked = 0 "
            "AND state IN ('pending_details', 'details_rejected', 'details_ok', "
            "'pending_photo', 'photo_rejected', 'awaiting_code', 'pending_code', 'code_rejected') "
            "AND left_at IS NULL LIMIT 1",
            (phone,),
        ).fetchone()
        if active:
            return json_error("Ya existe una solicitud activa con este teléfono.", 409)
        db.execute(
            "INSERT INTO visits (id, phone, site, state, consented_at, created_at, updated_at, last_seen) "
            "VALUES (?, ?, ?, 'pending_details', ?, ?, ?, ?)",
            (visit_id, phone, site, now, now, now, time.time()),
        )
    session["visit_id"] = visit_id
    return jsonify(id=visit_id, state="pending_details"), 201


def validate_visit_details(payload):
    if not isinstance(payload, dict):
        return "", "", "Envía los datos de la visita en un formato válido."
    phone = re.sub(r"[\s()+.-]", "", str(payload.get("phone", "")))
    if not re.fullmatch(r"\+?\d{8,15}", phone):
        return "", "", "Ingresa un teléfono válido (8 a 15 dígitos)."
    site_value = payload.get("site", "")
    if not isinstance(site_value, str):
        return "", "", "Ingresa una sede válida."
    site = " ".join(site_value.split())
    if not 2 <= len(site) <= 80:
        return "", "", "Ingresa una sede de 2 a 80 caracteres."
    return phone, site, ""


@app.post("/api/visits/current/details")
def update_visit_details():
    visit_id = session.get("visit_id")
    if not visit_id:
        return json_error("No hay una solicitud activa para corregir.", 404)
    payload = request.get_json(silent=True) or {}
    phone, site, error = validate_visit_details(payload)
    if error:
        return json_error(error, 400)
    if payload.get("consent") is not True:
        return json_error("Debes aceptar el aviso de privacidad para continuar.", 400)

    now = utc_now()
    with get_db() as db:
        blocked = db.execute(
            "SELECT 1 FROM visits WHERE phone = ? AND blocked = 1 LIMIT 1", (phone,)
        ).fetchone()
        if blocked:
            return json_error("Este teléfono no puede iniciar una solicitud. Contacta al personal.", 403)
        active = db.execute(
            "SELECT 1 FROM visits WHERE phone = ? AND id != ? AND blocked = 0 "
            "AND state IN ('pending_details', 'details_rejected', 'details_ok', "
            "'pending_photo', 'photo_rejected', 'awaiting_code', 'pending_code', 'code_rejected') "
            "AND left_at IS NULL LIMIT 1",
            (phone, visit_id),
        ).fetchone()
        if active:
            return json_error("Ya existe una solicitud activa con este teléfono.", 409)
        result = db.execute(
            "UPDATE visits SET phone = ?, site = ?, state = 'pending_details', note = NULL, "
            "updated_at = ?, last_seen = ? WHERE id = ? AND state = 'details_rejected' "
            "AND blocked = 0 AND left_at IS NULL",
            (phone, site, now, time.time(), visit_id),
        )
    if result.rowcount != 1:
        return json_error("La solicitud ya no está disponible para corregir.", 409)
    return jsonify(state="pending_details")


@app.post("/api/visits/<visit_id>/photo")
def submit_photo(visit_id):
    if session.get("visit_id") != visit_id:
        return json_error("Esta sesión no corresponde a la visita.", 403)
    payload = request.get_json(silent=True) or {}
    image_bytes, error = decode_jpeg(payload.get("image", ""))
    if image_bytes is None:
        status = 413 if error == "La imagen debe pesar menos de 5 MB." else 400
        return json_error(error, status)
    valid, message = verify_face(image_bytes)
    if not valid:
        return json_error(message, 422)

    name = f"{uuid.uuid4()}.jpg"
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    image.save(IMAGE_DIR / name, format="JPEG", quality=90, optimize=True)
    with get_db() as db:
        previous = db.execute(
            "SELECT image_name FROM visits WHERE id = ?", (visit_id,)
        ).fetchone()
        result = db.execute(
            "UPDATE visits SET image_name = ?, state = 'pending_photo', updated_at = ?, "
            "last_seen = ? WHERE id = ? AND state IN ('details_ok', 'photo_rejected') "
            "AND blocked = 0",
            (name, utc_now(), time.time(), visit_id),
        )
        if result.rowcount != 1:
            (IMAGE_DIR / name).unlink(missing_ok=True)
            return json_error("No se puede recibir una foto para esta visita.", 409)
        if previous and previous["image_name"]:
            (IMAGE_DIR / previous["image_name"]).unlink(missing_ok=True)
    return jsonify(state="pending_photo"), 201


@app.post("/api/visits/current/check-frame")
def check_frame():
    visit_id = session.get("visit_id")
    if not visit_id:
        return json_error("No hay una visita activa.", 404)
    with get_db() as db:
        row = db.execute(
            "SELECT state, blocked FROM visits WHERE id = ?", (visit_id,)
        ).fetchone()
    if not row or row["blocked"] or row["state"] not in ("details_ok", "photo_rejected"):
        return json_error("La visita no puede validar una imagen ahora.", 409)
    payload = request.get_json(silent=True) or {}
    image_bytes, error = decode_jpeg(payload.get("image", ""))
    if image_bytes is None:
        status = 413 if error == "La imagen debe pesar menos de 5 MB." else 400
        return json_error(error, status)
    _image, result = inspect_face_bytes(image_bytes)
    if result["ready"]:
        try:
            result["glasses_detected"] = detect_glasses(
                _image, result["face_box"]
            )
        except RuntimeError as error:
            app.logger.error("No fue posible verificar las gafas en la cámara (%s).",
                             type(error).__name__)
            return json_error(
                "No podemos verificar si llevas gafas ahora. Intenta de nuevo o pide ayuda al personal.",
                503,
            )
        if result["glasses_detected"]:
            result["ready"] = False
            result["message"] = "Detectamos gafas. Quítatelas y mira de nuevo a la cámara."
        else:
            result["message"] = "Rostro centrado y sin gafas detectadas. Mantén la posición."
    else:
        result["glasses_detected"] = False
    result["accessory_check"] = "glasses"
    result.pop("face_box", None)
    return jsonify(result)


@app.get("/api/visits/current")
def current_visit():
    visit_id = session.get("visit_id")
    if not visit_id:
        return jsonify(active=False)
    with get_db() as db:
        row = db.execute("SELECT * FROM visits WHERE id = ?", (visit_id,)).fetchone()
    if not row:
        session.pop("visit_id", None)
        return jsonify(active=False)
    if row["blocked"]:
        session.pop("visit_id", None)
        return json_error("Esta solicitud fue bloqueada. Contacta al personal.", 403)
    if request.method == "POST":
        with get_db() as db:
            db.execute(
                "UPDATE visits SET last_seen = ?, updated_at = ? WHERE id = ? AND blocked = 0",
                (time.time(), utc_now(), visit_id),
            )
    visit = serialize_visit(row)
    visit["active"] = True
    return jsonify(visit)


@app.post("/api/visits/current/heartbeat")
def heartbeat():
    visit_id = session.get("visit_id")
    if not visit_id:
        return json_error("No hay una visita activa.", 404)
    with get_db() as db:
        result = db.execute(
            "UPDATE visits SET last_seen = ? WHERE id = ? AND blocked = 0",
            (time.time(), visit_id),
        )
    if result.rowcount != 1:
        return json_error("Esta visita ya no está activa.", 403)
    return jsonify(ok=True)


@app.post("/api/visits/current/leave")
def leave_visit():
    visit_id = session.get("visit_id")
    if not visit_id:
        return json_error("No hay una visita activa.", 404)
    with get_db() as db:
        result = db.execute(
            "UPDATE visits SET left_at = ?, updated_at = ? WHERE id = ? AND blocked = 0",
            (utc_now(), utc_now(), visit_id),
        )
    session.pop("visit_id", None)
    if result.rowcount != 1:
        return json_error("Esta visita ya no está activa.", 403)
    return jsonify(ok=True)


@app.post("/api/visits/current/code")
def submit_worker_code():
    visit_id = session.get("visit_id")
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()
    if not re.fullmatch(r"\d{6}", code):
        return json_error("El código debe tener exactamente 6 dígitos.", 400)
    with get_db() as db:
        result = db.execute(
            "UPDATE visits SET worker_code = ?, state = 'pending_code', updated_at = ?, "
            "note = NULL, last_seen = ? WHERE id = ? "
            "AND state IN ('awaiting_code', 'code_rejected') AND blocked = 0",
            (code, utc_now(), time.time(), visit_id),
        )
    if result.rowcount != 1:
        return json_error("La visita todavía no está lista para recibir el código.", 409)
    return jsonify(state="pending_code")


def telegram_configured():
    secret = os.environ.get("FLASK_SECRET_KEY", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    return (
        len(secret) >= 32
        and not secret.startswith("replace-with-")
        and bool(re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", bot_token))
        and bool(re.fullmatch(r"-?\d{5,}", chat_id))
    )


@app.post("/api/admin/request-code")
def request_admin_code():
    if not telegram_configured():
        return json_error("El acceso de administración no está configurado. Revisa .env.", 503)
    now = time.time()
    with get_db() as db:
        previous = db.execute("SELECT requested_at FROM admin_codes WHERE id = 1").fetchone()
        if previous and now - previous["requested_at"] < OTP_RESEND_SECONDS:
            return json_error("Espera un minuto antes de solicitar otro código.", 429)
        code = f"{secrets.randbelow(1_000_000):06d}"
        digest = hmac.new(app.secret_key.encode(), code.encode(), hashlib.sha256).hexdigest()
        db.execute(
            "INSERT OR REPLACE INTO admin_codes (id, code_hash, expires_at, attempts, requested_at) "
            "VALUES (1, ?, ?, 0, ?)",
            (digest, now + OTP_TTL_SECONDS, now),
        )
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": os.environ["TELEGRAM_CHAT_ID"],
                  "text": f"Código de acceso al panel de visitas: {code} (válido 5 minutos)."},
            timeout=8,
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise requests.RequestException("Telegram rechazó el mensaje.")
    except requests.RequestException as error:
        app.logger.error("No se pudo enviar el código de acceso por Telegram (%s).",
                         type(error).__name__)
        return json_error("No se pudo enviar el código a Telegram.", 502)
    return jsonify(ok=True)


@app.post("/api/admin/login")
def admin_login():
    if not telegram_configured():
        return json_error("El acceso de administración no está configurado.", 503)
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()
    with get_db() as db:
        row = db.execute("SELECT * FROM admin_codes WHERE id = 1").fetchone()
        if not row or row["attempts"] >= 5 or time.time() > row["expires_at"]:
            return json_error("El código venció o ya no es válido. Solicita otro.", 401)
        digest = hmac.new(app.secret_key.encode(), code.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, row["code_hash"]):
            db.execute("UPDATE admin_codes SET attempts = attempts + 1 WHERE id = 1")
            return json_error("Código incorrecto.", 401)
        db.execute("DELETE FROM admin_codes WHERE id = 1")
    session.clear()
    session["admin_authenticated"] = True
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify(ok=True, csrf_token=session["csrf_token"])


@app.post("/api/admin/logout")
@admin_required
def admin_logout():
    session.clear()
    return jsonify(ok=True)


@app.before_request
def require_admin_csrf():
    if request.path.startswith("/api/admin/") and request.method in ("POST", "PATCH", "DELETE"):
        if request.path in ("/api/admin/request-code", "/api/admin/login"):
            return None
        token = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(token, expected):
            return json_error("Solicitud no válida. Recarga el panel.", 403)


@app.get("/api/admin/session")
def admin_session():
    if not session.get("admin_authenticated"):
        return jsonify(authenticated=False)
    return jsonify(authenticated=True, csrf_token=session.get("csrf_token"))


@app.get("/api/admin/visits")
@admin_required
def admin_visits():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM visits ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    return jsonify(visits=[serialize_visit(row) for row in rows])


@app.get("/api/admin/visits/<visit_id>/photo")
@admin_required
def admin_photo(visit_id):
    with get_db() as db:
        row = db.execute(
            "SELECT image_name FROM visits WHERE id = ?", (visit_id,)
        ).fetchone()
    if not row or not row["image_name"]:
        abort(404)
    path = IMAGE_DIR / row["image_name"]
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="image/jpeg", as_attachment=False)


@app.post("/api/admin/visits/<visit_id>/decision")
@admin_required
def admin_decision(visit_id):
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    transitions = {
        "details_ok": ("pending_details", "details_ok", None),
        "details_retry": (
            "pending_details",
            "details_rejected",
            "Revise sus datos. Vuelva a intentarlo.",
        ),
        "photo_ok": ("pending_photo", "awaiting_code", None),
        "photo_retry": ("pending_photo", "photo_rejected", "Toma otra selfie sin gafas ni gorra."),
        "code_ok": ("pending_code", "approved", None),
        "code_reject": ("pending_code", "code_rejected", "El código no coincide. Ingresa nuevamente los 6 dígitos."),
    }
    if action not in transitions:
        return json_error("Decisión no válida.", 400)
    expected, state, note = transitions[action]
    with get_db() as db:
        result = db.execute(
            "UPDATE visits SET state = ?, note = ?, updated_at = ? "
            "WHERE id = ? AND state = ? AND blocked = 0 AND left_at IS NULL "
            "AND (? != 'photo_ok' OR image_name IS NOT NULL)",
            (state, note, utc_now(), visit_id, expected, action),
        )
    if result.rowcount != 1:
        return json_error("La solicitud cambió o ya fue atendida.", 409)
    return jsonify(state=state)


@app.post("/api/admin/visits/<visit_id>/block")
@admin_required
def admin_block(visit_id):
    payload = request.get_json(silent=True) or {}
    blocked = bool(payload.get("blocked"))
    with get_db() as db:
        row = db.execute("SELECT phone FROM visits WHERE id = ?", (visit_id,)).fetchone()
        if not row:
            return json_error("No se encontró la visita.", 404)
        result = db.execute(
            "UPDATE visits SET blocked = ?, updated_at = ? WHERE phone = ?",
            (int(blocked), utc_now(), row["phone"]),
        )
    return jsonify(blocked=blocked)


@app.delete("/api/admin/visits/<visit_id>")
@admin_required
def delete_visit(visit_id):
    with get_db() as db:
        row = db.execute(
            "SELECT image_name FROM visits WHERE id = ?", (visit_id,)
        ).fetchone()
        if not row:
            return json_error("No se encontró la visita.", 404)
        if row["image_name"]:
            (IMAGE_DIR / row["image_name"]).unlink(missing_ok=True)
        db.execute("DELETE FROM visits WHERE id = ?", (visit_id,))
    return jsonify(ok=True)


@app.errorhandler(413)
def too_large(_error):
    return json_error("La solicitud supera el tamaño máximo permitido.", 413)


init_db()

if __name__ == "__main__":
    if not os.environ.get("FLASK_SECRET_KEY"):
        app.logger.warning("Usando una clave de sesión temporal; configura FLASK_SECRET_KEY.")
    debug_mode = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=debug_mode)
