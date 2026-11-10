import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from PIL import Image

from pleitos import create_app
from pleitos.extensions import db
from pleitos.models import AccessLog, Admin, Audience, Participant, utcnow
from pleitos.services import decrypt, digest, encrypt


PNG_BUFFER = io.BytesIO()
Image.new("RGB", (16, 16), "white").save(PNG_BUFFER, format="PNG")
VALID_PNG = PNG_BUFFER.getvalue()


class PleitosApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key-with-at-least-32-characters",
                "DATA_HASH_KEY": "test-independent-hmac-key-with-at-least-32-characters",
                "APP_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
                "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                "UPLOAD_FOLDER": str(Path(self.temp_dir.name) / "private"),
                "WTF_CSRF_ENABLED": False,
                "ADMIN_USERNAME": "test-admin",
                "ADMIN_PASSWORD": "test-password-12345",
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add(Admin.create("test-admin", "test-password-12345"))
        db.session.commit()
        self.client = self.app.test_client()
        response = self.client.post(
            "/api/admin/login",
            json={"username": "test-admin", "password": "test-password-12345"},
        )
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json["csrf_token"]
        self.headers = {"X-CSRF-Token": self.csrf}
        self.audience = self.create_audience("Audiência de teste", "2027-05-01")

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.temp_dir.cleanup()

    def create_audience(self, name, day):
        response = self.client.post(
            "/api/admin/audiences",
            json={"name": name, "date": day, "time": "14:30"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["id"]

    def register(
        self, audience_id=None, cpf="52998224725", email="teste@example.com", client=None
    ):
        response = (client or self.client).post(
            "/api/participants",
            data={
                "audience_id": audience_id or self.audience,
                "cpf": cpf,
                "category": "Convidado",
                "name": "Pessoa de Teste",
                "city": "Salvador",
                "role": "Representante",
                "phone": "71999999999",
                "email": email,
                "consent": "true",
                "photo": (io.BytesIO(VALID_PNG), "selfie.png", "image/png"),
            },
            content_type="multipart/form-data",
        )
        return response

    def test_registration_duplicate_per_audience_and_photo_storage(self):
        response = self.register()
        self.assertEqual(response.status_code, 201, response.json)
        participant = Participant.query.one()
        self.assertEqual(decrypt(participant.cpf_encrypted), "52998224725")
        self.assertEqual(decrypt(participant.phone_encrypted), "71999999999")
        photo = Path(self.app.config["UPLOAD_FOLDER"]) / participant.photo_filename
        self.assertTrue(photo.is_file())
        self.assertNotIn("static", photo.parts)
        other_device = self.app.test_client()
        self.assertEqual(self.register(client=other_device).status_code, 409)

        other_audience = self.create_audience("Audiência seguinte", "2027-05-02")
        second = self.register(audience_id=other_audience)
        self.assertEqual(second.status_code, 201, second.json)
        self.assertEqual(Participant.query.count(), 2)

    def test_audience_edit_and_registration_status(self):
        created = self.client.post(
            "/api/admin/audiences",
            json={"name": "Audiência editável", "date": "2027-06-01", "time": "09:00"},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201)
        audience_id = created.json["id"]
        self.assertTrue(created.json["active"])
        self.assertEqual(len(self.client.get("/api/audiences").json), 2)

        updated = self.client.patch(
            f"/api/admin/audiences/{audience_id}",
            json={"name": "Audiência atualizada", "date": "2027-06-02", "time": "10:30"},
            headers=self.headers,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json["name"], "Audiência atualizada")
        self.assertEqual(updated.json["date"], "2027-06-02")
        self.assertEqual(updated.json["time"], "10:30")

        closed = self.client.patch(
            f"/api/admin/audiences/{audience_id}",
            json={"active": False},
            headers=self.headers,
        )
        self.assertFalse(closed.json["active"])
        self.assertEqual(
            [audience["id"] for audience in self.client.get("/api/audiences").json],
            [self.audience],
        )

        reopened = self.client.patch(
            f"/api/admin/audiences/{audience_id}",
            json={"active": True},
            headers=self.headers,
        )
        self.assertTrue(reopened.json["active"])
        self.assertEqual(len(self.client.get("/api/audiences").json), 2)

    def test_admin_page_has_audience_editor_and_accessible_check_in_feedback(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="audienceEditDialog"', response.data)
        self.assertIn(b'id="audienceEditForm"', response.data)
        self.assertIn(b'aria-live="assertive"', response.data)
        self.assertIn(b'aria-label="Pesquisar participantes', response.data)
        self.assertIn(b'enterkeyhint="go"', response.data)
        self.assertNotIn(b'id="scanCode" autocomplete="off" autofocus', response.data)

    def test_invalid_cpf_and_reception_permissions(self):
        rejected = self.register(cpf="11111111111")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(Participant.query.count(), 0)

        registration = self.register()
        receptionist = Admin.create("reception-user", "reception-password-12345", role="reception")
        db.session.add(receptionist)
        db.session.commit()
        client = self.app.test_client()
        login = client.post(
            "/api/admin/login",
            json={"username": "reception-user", "password": "reception-password-12345"},
        )
        self.assertEqual(login.status_code, 200)
        csrf = login.json["csrf_token"]
        private_data = client.get("/api/admin/participants")
        self.assertEqual(private_data.status_code, 403)
        checked_in = client.post(
            "/api/admin/check-in",
            json={"code": registration.json["credential"], "method": "qr"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(checked_in.status_code, 200)

    def test_server_qr_validation_single_entry_and_dashboard(self):
        registration = self.register()
        token = registration.json["credential"]
        qr = self.client.post("/api/credentials/qr", json={"credential": token})
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.mimetype, "image/png")
        invalid_qr = self.client.post("/api/credentials/qr", json={"credential": "invalid"})
        self.assertEqual(invalid_qr.status_code, 404)

        invalid = self.client.post(
            "/api/admin/check-in",
            json={"code": "invalid", "method": "qr"},
            headers=self.headers,
        )
        self.assertEqual(invalid.status_code, 404)
        valid = self.client.post(
            "/api/admin/check-in",
            json={"code": token, "method": "qr"},
            headers=self.headers,
        )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.json["status"], "valid")
        duplicate = self.client.post(
            "/api/admin/check-in",
            json={"code": token, "method": "usb"},
            headers=self.headers,
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json["status"], "duplicate")

        dashboard = self.client.get("/api/admin/dashboard")
        self.assertEqual(dashboard.json["registered"], 1)
        self.assertEqual(dashboard.json["present"], 1)
        self.assertEqual(dashboard.json["invalid_attempts"], 1)
        self.assertEqual(AccessLog.query.filter_by(outcome="duplicate").count(), 1)

    def test_manual_release_requires_justification_and_is_single_use(self):
        registration = self.register()
        participant_id = registration.json["participant"]["id"]
        missing_reason = self.client.post(
            f"/api/admin/participants/{participant_id}/manual-check-in",
            json={"reason": "no"},
            headers=self.headers,
        )
        self.assertEqual(missing_reason.status_code, 400)
        released = self.client.post(
            f"/api/admin/participants/{participant_id}/manual-check-in",
            json={"reason": "Documento conferido presencialmente"},
            headers=self.headers,
        )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.json["status"], "valid")

    def test_credential_lookup_requires_email_code(self):
        registration = self.register()
        participant = Participant.query.one()
        with patch("pleitos.routes.public.send_lookup_code") as send_code:
            response = self.client.post(
                "/api/credentials/lookup", json={"cpf": "529.982.247-25"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("credential", response.json)
        sent_email, code = send_code.call_args.args
        self.assertEqual(sent_email, "teste@example.com")
        verified = self.client.post(
            "/api/credentials/lookup/verify",
            json={"challenge_id": response.json["challenge_id"], "code": code},
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json["credential"], registration.json["credential"])

    def test_protected_photo_edit_exports_and_deletion(self):
        registration = self.register()
        participant_id = registration.json["participant"]["id"]
        unauthenticated = self.app.test_client().get(
            f"/api/admin/participants/{participant_id}/photo"
        )
        self.assertEqual(unauthenticated.status_code, 401)
        photo = self.client.get(f"/api/admin/participants/{participant_id}/photo")
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.mimetype, "image/jpeg")
        photo.close()

        edited = self.client.patch(
            f"/api/admin/participants/{participant_id}",
            json={"name": "Nome Atualizado"},
            headers=self.headers,
        )
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json["name"], "Nome Atualizado")

        for output, mimetype in (
            ("csv", "text/csv"),
            ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ("pdf", "application/pdf"),
        ):
            exported = self.client.get(f"/api/admin/export?format={output}")
            self.assertEqual(exported.status_code, 200)
            self.assertEqual(exported.mimetype, mimetype)
            self.assertGreater(len(exported.data), 10)

        deleted = self.client.delete(
            f"/api/admin/participants/{participant_id}",
            json={"reason": "Solicitação formal do titular"},
            headers=self.headers,
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(Participant.query.count(), 0)
        self.assertEqual(AccessLog.query.filter(AccessLog.participant_id == participant_id).count(), 0)

    def test_home_page_offers_participant_and_admin_access(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"PLEITOS", response.data)
        self.assertIn(b'href="/cadastro"', response.data)
        self.assertIn(b'href="/admin"', response.data)
        self.assertIn("Área do participante".encode("utf-8"), response.data)
        self.assertIn("Área administrativa".encode("utf-8"), response.data)

    def test_public_registration_page_keeps_existing_ui_and_replaces_local_storage_code(self):
        response = self.client.get("/cadastro")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"PLEITOS", response.data)
        self.assertIn(b"/static/js/public.js", response.data)
        self.assertIn(b"/static/css/public-layout.css", response.data)
        self.assertIn(b'class="btn home-link" href="/">', response.data)
        self.assertNotIn(b"const KEY='pleitos_access_v1'", response.data)
        self.assertNotIn(b"localStorage", response.data)
        self.assertNotIn(b"let people=", response.data)
        self.assertIn(b"personDocument", response.data)

    def test_database_data_survives_recreating_the_application(self):
        database_path = Path(self.temp_dir.name) / "persistent.sqlite"
        database_url = f"sqlite:///{database_path.as_posix()}"
        config = {
            "SECRET_KEY": "persistence-test-secret-with-at-least-32-characters",
            "DATA_HASH_KEY": "persistence-test-independent-hmac-key-0123456789",
            "APP_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
            "SQLALCHEMY_DATABASE_URI": database_url,
            "UPLOAD_FOLDER": str(Path(self.temp_dir.name) / "persistent-photos"),
        }
        first_app = create_app(config)
        with first_app.app_context():
            db.create_all()
            db.session.add(Audience(name="Persistente", date=date(2027, 8, 1), time=time(9, 0)))
            db.session.commit()
            db.session.remove()
            db.engine.dispose()
        second_app = create_app(config)
        with second_app.app_context():
            self.assertEqual(Audience.query.filter_by(name="Persistente").count(), 1)
            db.session.remove()
            db.engine.dispose()

    def test_concurrent_check_in_only_releases_once(self):
        database_path = Path(self.temp_dir.name) / "concurrent.sqlite"
        config = {
            "TESTING": True,
            "SECRET_KEY": "concurrency-test-secret-with-at-least-32-characters",
            "DATA_HASH_KEY": "concurrency-test-hmac-key-with-at-least-32-characters",
            "APP_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path.as_posix()}",
            "UPLOAD_FOLDER": str(Path(self.temp_dir.name) / "concurrent-photos"),
        }
        concurrent_app = create_app(config)
        credential = "single-use-concurrent-credential"
        with concurrent_app.app_context():
            db.create_all()
            admin = Admin.create("concurrency-admin", "concurrency-password-12345")
            audience = Audience(name="Concorrência", date=date(2027, 9, 1), time=time(10, 0))
            db.session.add_all([admin, audience])
            db.session.flush()
            db.session.add(
                Participant(
                    audience_id=audience.id,
                    cpf_hash=digest("52998224725"),
                    cpf_encrypted=encrypt("52998224725"),
                    category="Convidado",
                    name="Participante Concorrente",
                    city="Salvador",
                    role="Convidado",
                    phone_encrypted=encrypt("71999999999"),
                    photo_filename="not-used.jpg",
                    credential_hash=digest(credential),
                    credential_encrypted=encrypt(credential),
                    consent_at=utcnow(),
                    consent_version="test",
                )
            )
            db.session.commit()
            db.session.remove()

        clients = [concurrent_app.test_client(), concurrent_app.test_client()]
        csrf_tokens = []
        for client in clients:
            login = client.post(
                "/api/admin/login",
                json={
                    "username": "concurrency-admin",
                    "password": "concurrency-password-12345",
                },
            )
            csrf_tokens.append(login.json["csrf_token"])
        barrier = threading.Barrier(2)

        def check_in(index):
            barrier.wait(timeout=5)
            return clients[index].post(
                "/api/admin/check-in",
                json={"code": credential, "method": "qr"},
                headers={"X-CSRF-Token": csrf_tokens[index]},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(check_in, range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        with concurrent_app.app_context():
            self.assertIsNotNone(Participant.query.one().checked_at)
            self.assertEqual(AccessLog.query.filter_by(outcome="valid").count(), 1)
            db.session.remove()
            db.engine.dispose()

    def test_production_requires_postgresql_and_secure_session_cookie(self):
        config = {
            "SECRET_KEY": "production-test-secret-with-at-least-32-characters",
            "APP_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
            "DATA_HASH_KEY": "production-test-hmac-key-with-at-least-32-characters",
            "APP_ENV": "production",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///not-for-production.sqlite",
        }
        with self.assertRaisesRegex(RuntimeError, "Produção exige PostgreSQL"):
            create_app(config)
        config["SQLALCHEMY_DATABASE_URI"] = "postgresql+psycopg://user:password@localhost/pleitos"
        with self.assertRaisesRegex(RuntimeError, "SESSION_COOKIE_SECURE=1"):
            create_app(config)
        config["SESSION_COOKIE_SECURE"] = True
        production_app = create_app(config)
        self.assertTrue(production_app.config["SESSION_COOKIE_SECURE"])


if __name__ == "__main__":
    unittest.main()
