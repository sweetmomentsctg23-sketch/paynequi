import base64
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as application
from PIL import Image


class VisitFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_data_dir = application.DATA_DIR
        self.original_image_dir = application.IMAGE_DIR
        self.original_db_path = application.DB_PATH
        application.DATA_DIR = Path(self.temp_dir.name)
        application.IMAGE_DIR = application.DATA_DIR / "selfies"
        application.DB_PATH = application.DATA_DIR / "test.sqlite3"
        application.init_db()
        application.app.config.update(TESTING=True)
        self.client = application.app.test_client()

    def tearDown(self):
        application.DATA_DIR = self.original_data_dir
        application.IMAGE_DIR = self.original_image_dir
        application.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def create_visit(self, phone="5551234567", site="Sede Centro", consent=True):
        return self.client.post(
            "/api/visits",
            json={"phone": phone, "site": site, "consent": consent},
        )

    def approve_details(self, visit_id):
        with application.get_db() as db:
            db.execute(
                "UPDATE visits SET state = 'details_ok' WHERE id = ?", (visit_id,)
            )

    def test_visit_requires_consent_and_valid_phone(self):
        self.assertEqual(self.client.get("/api/visits/current").json, {"active": False})
        root_page = self.client.get("/")
        self.assertEqual(root_page.status_code, 200)
        self.assertNotIn(b"/admin", root_page.data)
        no_consent = self.client.post(
            "/api/visits",
            json={"phone": "5551234567", "site": "Sede Centro", "consent": False},
        )
        self.assertEqual(no_consent.status_code, 400)
        invalid_phone = self.client.post(
            "/api/visits",
            json={"phone": "not-a-phone", "site": "Sede Centro", "consent": True},
        )
        self.assertEqual(invalid_phone.status_code, 400)
        invalid_site = self.client.post(
            "/api/visits",
            json={"phone": "5551234567", "site": "", "consent": True},
        )
        self.assertEqual(invalid_site.status_code, 400)
        created = self.client.post(
            "/api/visits",
            json={
                "phone": "+1 (555) 123-4567",
                "site": "Sede Centro",
                "consent": True,
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json["state"], "pending_details")
        current = self.client.get("/api/visits/current")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json["phone"], "15551234567")
        self.assertEqual(current.json["site"], "Sede Centro")

    def test_active_phone_cannot_create_duplicate_visit(self):
        first = self.create_visit()
        self.assertEqual(first.status_code, 201)
        second = self.create_visit()
        self.assertEqual(second.status_code, 409)

    def test_rejected_details_can_be_corrected_before_camera_is_unlocked(self):
        created = self.create_visit()
        visit_id = created.json["id"]
        with self.client.session_transaction() as browser_session:
            browser_session["admin_authenticated"] = True
            browser_session["csrf_token"] = "test-csrf"
        headers = {"X-CSRF-Token": "test-csrf"}

        rejected = self.client.post(
            f"/api/admin/visits/{visit_id}/decision",
            json={"action": "details_retry"},
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 200)
        current = self.client.get("/api/visits/current").json
        self.assertEqual(current["state"], "details_rejected")
        self.assertEqual(current["note"], "Revise sus datos. Vuelva a intentarlo.")

        frame = self.client.post(
            "/api/visits/current/check-frame",
            json={"image": "data:image/jpeg;base64,ZmFrZQ=="},
        )
        self.assertEqual(frame.status_code, 409)

        corrected = self.client.post(
            "/api/visits/current/details",
            json={
                "phone": "5557654321",
                "site": "Sede Norte",
                "consent": True,
            },
        )
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(corrected.json["state"], "pending_details")
        current = self.client.get("/api/visits/current").json
        self.assertEqual(current["phone"], "5557654321")
        self.assertEqual(current["site"], "Sede Norte")

    def test_deleted_or_nonexistent_visit_session_returns_inactive_gracefully(self):
        with self.client.session_transaction() as sess:
            sess["visit_id"] = "non-existent-uuid"
        response = self.client.get("/api/visits/current")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"active": False})

    def test_admin_routes_require_authentication(self):
        response = self.client.get("/api/admin/visits")
        self.assertEqual(response.status_code, 401)

    def test_live_face_check_requires_an_active_visit(self):
        response = self.client.post(
            "/api/visits/current/check-frame",
            json={"image": "data:image/jpeg;base64,ZmFrZQ=="},
        )
        self.assertEqual(response.status_code, 404)

    def test_live_face_check_returns_position_guidance_without_saving_frame(self):
        created = self.create_visit()
        self.approve_details(created.json["id"])
        buffer = io.BytesIO()
        Image.new("RGB", (320, 320), "white").save(buffer, format="JPEG")
        image = base64.b64encode(buffer.getvalue()).decode()
        with patch.object(
            application, "inspect_face_bytes",
            return_value=(None, {"ready": False, "message": "Centra tu rostro dentro del óvalo."}),
        ):
            response = self.client.post(
                "/api/visits/current/check-frame",
                json={"image": f"data:image/jpeg;base64,{image}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["ready"])
        self.assertEqual(response.json["accessory_check"], "glasses")
        self.assertFalse(response.json["glasses_detected"])
        with application.get_db() as db:
            row = db.execute(
                "SELECT image_name FROM visits WHERE id = ?", (created.json["id"],)
            ).fetchone()
        self.assertIsNone(row["image_name"])

    def test_live_face_check_requires_removing_detected_glasses(self):
        created = self.create_visit()
        self.approve_details(created.json["id"])
        buffer = io.BytesIO()
        Image.new("RGB", (320, 320), "white").save(buffer, format="JPEG")
        image = base64.b64encode(buffer.getvalue()).decode()
        face_image = Image.new("RGB", (320, 320), "white")
        with patch.object(
            application, "inspect_face_bytes",
            return_value=(face_image, {
                "ready": True, "message": "Rostro centrado.",
                "face_box": [80, 40, 160, 220],
            }),
        ), patch.object(application, "detect_glasses", return_value=True):
            response = self.client.post(
                "/api/visits/current/check-frame",
                json={"image": f"data:image/jpeg;base64,{image}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["ready"])
        self.assertTrue(response.json["glasses_detected"])
        self.assertIn("Quítatelas", response.json["message"])

    def test_live_face_check_allows_capture_when_glasses_are_not_detected(self):
        created = self.create_visit()
        self.approve_details(created.json["id"])
        buffer = io.BytesIO()
        Image.new("RGB", (320, 320), "white").save(buffer, format="JPEG")
        image = base64.b64encode(buffer.getvalue()).decode()
        face_image = Image.new("RGB", (320, 320), "white")
        with patch.object(
            application, "inspect_face_bytes",
            return_value=(face_image, {
                "ready": True, "message": "Rostro centrado.",
                "face_box": [80, 40, 160, 220],
            }),
        ), patch.object(application, "detect_glasses", return_value=False):
            response = self.client.post(
                "/api/visits/current/check-frame",
                json={"image": f"data:image/jpeg;base64,{image}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ready"])
        self.assertFalse(response.json["glasses_detected"])

    def test_final_photo_is_rejected_when_glasses_are_detected(self):
        created = self.create_visit()
        self.approve_details(created.json["id"])
        buffer = io.BytesIO()
        Image.new("RGB", (320, 320), "white").save(buffer, format="JPEG")
        image = base64.b64encode(buffer.getvalue()).decode()
        with patch.object(
            application, "inspect_face_bytes",
            return_value=(Image.new("RGB", (320, 320)), {
                "ready": True, "message": "Rostro centrado.",
                "face_box": [80, 40, 160, 220],
            }),
        ), patch.object(application, "detect_glasses", return_value=True):
            response = self.client.post(
                f"/api/visits/{created.json['id']}/photo",
                json={"image": f"data:image/jpeg;base64,{image}"},
            )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Quítatelas", response.json["error"])

    def test_photo_validator_rejects_frame_without_face(self):
        buffer = io.BytesIO()
        Image.new("RGB", (320, 320), "white").save(buffer, format="JPEG")
        valid, message = application.verify_face(buffer.getvalue())
        self.assertFalse(valid)
        self.assertIn("No se detectó un rostro", message)

    @patch.dict("os.environ", {"FLASK_SECRET_KEY": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
    def test_admin_login_is_unavailable_without_telegram_configuration(self):
        response = self.client.post("/api/admin/request-code", json={})
        self.assertEqual(response.status_code, 503)

    @patch.dict(os.environ, {
        "FLASK_SECRET_KEY": "a-long-test-secret-for-admin-access-123456",
        "TELEGRAM_BOT_TOKEN": "12345:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh",
        "TELEGRAM_CHAT_ID": "123456",
    })
    @patch("app.requests.post")
    def test_telegram_code_authenticates_admin(self, send_message):
        send_message.return_value.json.return_value = {"ok": True}
        requested = self.client.post("/api/admin/request-code", json={})
        self.assertEqual(requested.status_code, 200)
        telegram_text = send_message.call_args.kwargs["json"]["text"]
        code = re.search(r"(\d{6})", telegram_text).group(1)
        login = self.client.post("/api/admin/login", json={"code": code})
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json["csrf_token"])
        self.assertEqual(self.client.get("/api/admin/visits").status_code, 200)

    def test_photo_review_code_decision_and_phone_block(self):
        created = self.create_visit()
        visit_id = created.json["id"]
        with self.client.session_transaction() as browser_session:
            browser_session["admin_authenticated"] = True
            browser_session["csrf_token"] = "test-csrf"
        headers = {"X-CSRF-Token": "test-csrf"}
        details_reviewed = self.client.post(
            f"/api/admin/visits/{visit_id}/decision",
            json={"action": "details_ok"},
            headers=headers,
        )
        self.assertEqual(details_reviewed.status_code, 200)
        admin_visits = self.client.get("/api/admin/visits").json["visits"]
        self.assertEqual(admin_visits[0]["phone"], "5551234567")
        self.assertEqual(admin_visits[0]["site"], "Sede Centro")
        buffer = io.BytesIO()
        Image.new("RGB", (320, 320), "white").save(buffer, format="JPEG")
        image = base64.b64encode(buffer.getvalue()).decode()
        with patch.object(application, "verify_face", return_value=(True, "")):
            photo = self.client.post(
                f"/api/visits/{visit_id}/photo",
                json={"image": f"data:image/jpeg;base64,{image}"},
            )
        self.assertEqual(photo.status_code, 201)

        reviewed = self.client.post(
            f"/api/admin/visits/{visit_id}/decision",
            json={"action": "photo_ok"},
            headers=headers,
        )
        self.assertEqual(reviewed.status_code, 200)
        submitted_code = self.client.post(
            "/api/visits/current/code", json={"code": "123456"}
        )
        self.assertEqual(submitted_code.status_code, 200)
        approved = self.client.post(
            f"/api/admin/visits/{visit_id}/decision",
            json={"action": "code_ok"},
            headers=headers,
        )
        self.assertEqual(approved.status_code, 200)
        blocked = self.client.post(
            f"/api/admin/visits/{visit_id}/block",
            json={"blocked": True},
            headers=headers,
        )
        self.assertEqual(blocked.status_code, 200)
        duplicate = self.create_visit()
        self.assertEqual(duplicate.status_code, 403)

    def test_worker_code_requires_six_digits_and_can_be_reentered_after_rejection(self):
        created = self.create_visit()
        visit_id = created.json["id"]
        with application.get_db() as db:
            db.execute(
                "UPDATE visits SET state = 'awaiting_code' WHERE id = ?", (visit_id,)
            )

        self.assertEqual(
            self.client.post("/api/visits/current/code", json={"code": "12345"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/visits/current/code", json={"code": "12345A"}).status_code,
            400,
        )
        submitted = self.client.post(
            "/api/visits/current/code", json={"code": "123456"}
        )
        self.assertEqual(submitted.status_code, 200)

        with self.client.session_transaction() as browser_session:
            browser_session["admin_authenticated"] = True
            browser_session["csrf_token"] = "test-csrf"
        rejected = self.client.post(
            f"/api/admin/visits/{visit_id}/decision",
            json={"action": "code_reject"},
            headers={"X-CSRF-Token": "test-csrf"},
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json["state"], "code_rejected")
        retry = self.client.post(
            "/api/visits/current/code", json={"code": "654321"}
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json["state"], "pending_code")


if __name__ == "__main__":
    unittest.main()
