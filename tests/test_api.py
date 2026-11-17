import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

from cryptography.fernet import Fernet
from PIL import Image

from pleitos import create_app
from pleitos.extensions import db
from pleitos.models import (
    AccessLog,
    Admin,
    Audience,
    EmailNotification,
    Event,
    EventParticipant,
    Participant,
    RegistrationInvite,
    utcnow,
)
from pleitos.services import activity_today, decrypt, digest, encrypt


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
        self.email_sender = patch("pleitos.services.send_notification_email")
        self.email_sender.start()
        self.addCleanup(self.email_sender.stop)
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

    def create_audience(self, name, day, capacity=None):
        response = self.client.post(
            "/api/admin/audiences",
            json={"name": name, "date": day, "time": "14:30", "capacity": capacity},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["id"]

    def create_event(self, name="Evento de teste", day="2027-05-10", capacity=None):
        response = self.client.post(
            "/api/admin/events",
            json={
                "name": name,
                "date": day,
                "time": "09:00",
                "end_time": "18:00",
                "location": "Salvador",
                "capacity": capacity,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["id"]

    def register_event(self, event_id, cpf="52998224725"):
        return self.client.post(
            "/api/event-participants",
            data={
                "event_id": event_id,
                "cpf": cpf,
                "category": "Convidado",
                "name": "Pessoa de Evento",
                "city": "Salvador",
                "role": "Representante",
                "phone": "71999999999",
                "email": "evento@example.com",
                "instagram": "@pessoa",
                "consent": "true",
                "photo": (io.BytesIO(VALID_PNG), "selfie.png", "image/png"),
            },
            content_type="multipart/form-data",
        )

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

    def create_invite(self, cpf="52998224725", replace_active=False):
        return self.client.post(
            "/api/admin/convites",
            json={"cpf": cpf, "replace_active": replace_active},
            headers=self.headers,
        )

    def invite_token(self, response):
        return urlparse(response.json["url"]).path.rsplit("/", 1)[-1]

    def invited_registration(
        self,
        token,
        cpf="11144477735",
        activity_type="audience",
        activity_id=None,
    ):
        return self.client.post(
            f"/api/cadastro/{token}",
            data={
                "cpf": cpf,
                "activity_type": activity_type,
                "activity_id": activity_id or self.audience,
                "category": "Convidado",
                "name": "Pessoa Convidada",
                "city": "Salvador",
                "role": "Representante",
                "phone": "71999999999",
                "email": "convidado@example.com",
                "consent": "true",
                "photo": (io.BytesIO(VALID_PNG), "selfie.png", "image/png"),
            },
            content_type="multipart/form-data",
        )

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

    def test_event_registration_and_check_in_are_independent_from_audience(self):
        event_id = self.create_event()
        registration = self.register_event(event_id)
        self.assertEqual(registration.status_code, 201, registration.json)
        event_participant = EventParticipant.query.one()
        self.assertEqual(Event.query.count(), 1)
        self.assertEqual(Participant.query.count(), 0)
        self.assertEqual(registration.json["participant"]["event"]["name"], "Evento de teste")

        duplicate = self.register_event(event_id)
        self.assertEqual(duplicate.status_code, 409)

        credential = registration.json["credential"]
        qr = self.client.post("/api/credentials/qr", json={"credential": credential})
        self.assertEqual(qr.status_code, 200)
        checked_in = self.client.post(
            "/api/admin/check-in",
            json={"code": credential, "method": "qr"},
            headers=self.headers,
        )
        self.assertEqual(checked_in.status_code, 200)
        self.assertEqual(checked_in.json["participant"]["event"]["name"], "Evento de teste")
        self.assertIsNotNone(event_participant.checked_at)
        access_log = AccessLog.query.filter_by(event_participant_id=event_participant.id).one()
        self.assertEqual(access_log.outcome, "valid")
        self.assertEqual(self.client.get("/api/admin/dashboard").json["registered"], 0)
        event_dashboard = self.client.get(f"/api/admin/events/{event_id}/dashboard")
        self.assertEqual(event_dashboard.json["registered"], 1)
        self.assertEqual(event_dashboard.json["present"], 1)
        self.assertEqual(event_dashboard.json["absent"], 0)

        roster = self.client.get(f"/api/admin/event-participants?event_id={event_id}")
        self.assertEqual(roster.status_code, 200)
        self.assertEqual(roster.json[0]["cpf"], "52998224725")
        export = self.client.get(f"/api/admin/events/{event_id}/export")
        self.assertEqual(export.status_code, 200)
        self.assertIn(b"Pessoa de Evento", export.data)

    def test_audience_capacity_waitlist_fifo_promotion_and_cancellation(self):
        created = self.create_audience("Audiência com vagas", "2027-05-15", capacity=1)
        first = self.register(audience_id=created, cpf="52998224725")
        second = self.register(audience_id=created, cpf="11144477735", email="segundo@example.com")
        third = self.register(audience_id=created, cpf="12345678909", email="terceiro@example.com")
        self.assertEqual(first.json["registration_status"], "confirmed")
        self.assertEqual(second.json["registration_status"], "waitlisted")
        self.assertIsNone(second.json["credential"])
        self.assertEqual(third.json["registration_status"], "waitlisted")

        first_id = first.json["participant"]["id"]
        cancelled = self.client.post(
            f"/api/admin/participants/{first_id}/cancel",
            json={"reason": "Participante desistiu"},
            headers=self.headers,
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json["promoted"], 1)
        second_row = db.session.get(Participant, second.json["participant"]["id"])
        third_row = db.session.get(Participant, third.json["participant"]["id"])
        self.assertEqual(second_row.status, "active")
        self.assertEqual(third_row.status, "waitlisted")
        self.assertEqual(EmailNotification.query.filter_by(notification_type="promoted").count(), 1)

        cancelled_activity = self.client.patch(
            f"/api/admin/audiences/{created}",
            json={"status": "cancelled"},
            headers=self.headers,
        )
        self.assertEqual(cancelled_activity.status_code, 200)
        self.assertEqual(cancelled_activity.json["cancellation_notices"], 2)
        check_in = self.client.post(
            "/api/admin/check-in",
            json={"code": decrypt(second_row.credential_encrypted), "method": "qr"},
            headers=self.headers,
        )
        self.assertEqual(check_in.status_code, 409)
        self.assertEqual(check_in.json["message"], "Atividade cancelada")

    def test_event_capacity_waitlist_is_promoted_after_cancellation(self):
        event_id = self.create_event(day="2027-05-16", capacity=1)
        first = self.register_event(event_id, cpf="52998224725")
        second = self.register_event(event_id, cpf="11144477735")
        self.assertEqual(first.json["registration_status"], "confirmed")
        self.assertEqual(second.json["registration_status"], "waitlisted")

        cancelled = self.client.post(
            f"/api/admin/event-participants/{first.json['participant']['id']}/cancel",
            json={"reason": "Participante desistiu"},
            headers=self.headers,
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json["promoted"], 1)
        promoted = db.session.get(EventParticipant, second.json["participant"]["id"])
        self.assertEqual(promoted.status, "active")

    def test_reminders_are_scheduled_once_and_process_endpoint_delivers_them(self):
        reminder_day = (activity_today() + timedelta(days=7)).isoformat()
        audience_id = self.create_audience("Audiência com lembrete", reminder_day)
        registration = self.register(audience_id=audience_id)

        processed = self.client.post(
            "/api/admin/notifications/process",
            json={},
            headers=self.headers,
        )
        self.assertEqual(processed.status_code, 200)
        self.assertEqual(processed.json["queued"], 1)
        self.assertEqual(processed.json["sent"], 1)
        self.assertEqual(processed.json["failed"], 0)
        reminder = EmailNotification.query.filter_by(
            participant_id=registration.json["participant"]["id"],
            notification_type="reminder_7d",
        ).one()
        self.assertIsNotNone(reminder.sent_at)

        repeated = self.client.post(
            "/api/admin/notifications/process",
            json={},
            headers=self.headers,
        )
        self.assertEqual(repeated.json["queued"], 0)
        self.assertEqual(
            EmailNotification.query.filter_by(
                participant_id=registration.json["participant"]["id"],
                notification_type="reminder_7d",
            ).count(),
            1,
        )
    def test_event_registration_lookup_and_registration_closure(self):
        event_id = self.create_event("Evento recuperável")
        registration = self.register_event(event_id)
        with patch("pleitos.routes.public.send_lookup_code") as send_code:
            response = self.client.post(
                "/api/event-credentials/lookup",
                json={"cpf": "529.982.247-25", "event_id": event_id},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("credential", response.json)
        sent_email, code = send_code.call_args.args
        self.assertEqual(sent_email, "evento@example.com")
        verified = self.client.post(
            "/api/event-credentials/lookup/verify",
            json={"challenge_id": response.json["challenge_id"], "code": code},
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json["credential"], registration.json["credential"])

        closed = self.client.patch(
            f"/api/admin/events/{event_id}",
            json={"active": False},
            headers=self.headers,
        )
        self.assertEqual(closed.status_code, 200)
        self.assertFalse(closed.json["active"])
        self.assertEqual(self.client.get("/api/events").json[0]["active"], False)
        rejected_registration = self.register_event(event_id, cpf="11144477735")
        self.assertEqual(rejected_registration.status_code, 400)

        invalid_schedule = self.client.post(
            "/api/admin/events",
            json={
                "name": "Horário inválido",
                "date": "2027-05-11",
                "time": "18:00",
                "end_time": "09:00",
                "location": "Salvador",
            },
            headers=self.headers,
        )
        self.assertEqual(invalid_schedule.status_code, 400)

    def test_admin_page_has_audience_editor_and_accessible_check_in_feedback(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="audienceEditDialog"', response.data)
        self.assertIn(b'id="audienceEditForm"', response.data)
        self.assertIn(b'aria-live="assertive"', response.data)
        self.assertIn(b'aria-label="Pesquisar participantes', response.data)
        self.assertIn(b'enterkeyhint="go"', response.data)
        self.assertNotIn(b'id="scanCode" autocomplete="off" autofocus', response.data)
        self.assertIn(b'data-admin-tab="eventAdminTab"', response.data)
        self.assertIn(b'id="registrationInviteForm"', response.data)

    def test_invite_page_requires_a_valid_token_before_showing_the_form(self):
        response = self.client.get("/cadastro/example-token")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="inviteFormPanel" hidden', response.data)
        self.assertIn(b'data-invite-token="example-token"', response.data)
        self.assertIn(b'id="inviteStartPhotoCamera"', response.data)
        self.assertIn(b'id="inviteCapturePhoto"', response.data)
        self.assertIn(b'id="invitePhotoFile"', response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_invite_generation_requires_an_admin_or_organizer_session(self):
        anonymous = self.app.test_client()
        response = anonymous.post(
            "/api/admin/convites",
            json={"cpf": "52998224725"},
        )
        self.assertEqual(response.status_code, 401)

        receptionist = Admin.create("reception-invites", "reception-password-12345", role="reception")
        db.session.add(receptionist)
        db.session.commit()
        response = self.client.post(
            "/api/admin/logout",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        login = self.client.post(
            "/api/admin/login",
            json={"username": "reception-invites", "password": "reception-password-12345"},
        )
        response = self.client.post(
            "/api/admin/convites",
            json={"cpf": "52998224725"},
            headers={"X-CSRF-Token": login.json["csrf_token"]},
        )
        self.assertEqual(response.status_code, 403)

        organizer = Admin.create("registration-organizer", "organizer-password-12345", role="organizer")
        db.session.add(organizer)
        db.session.commit()
        self.client.post(
            "/api/admin/logout",
            headers={"X-CSRF-Token": login.json["csrf_token"]},
        )
        organizer_login = self.client.post(
            "/api/admin/login",
            json={"username": "registration-organizer", "password": "organizer-password-12345"},
        )
        response = self.client.post(
            "/api/admin/convites",
            json={"cpf": "52998224725"},
            headers={"X-CSRF-Token": organizer_login.json["csrf_token"]},
        )
        self.assertEqual(response.status_code, 201, response.json)

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

    def test_invite_generation_rejects_invalid_cpf_and_reuses_only_by_explicit_replacement(self):
        invalid = self.create_invite(cpf="11111111111")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(RegistrationInvite.query.count(), 0)

        first = self.create_invite()
        self.assertEqual(first.status_code, 201, first.json)
        first_token = self.invite_token(first)
        invite = RegistrationInvite.query.one()
        self.assertEqual(invite.token_hash, digest(first_token))
        self.assertNotIn(first_token, invite.token_hash)
        self.assertNotIn("52998224725", invite.cpf_encrypted)

        duplicate = self.create_invite()
        self.assertEqual(duplicate.status_code, 409)
        self.assertTrue(duplicate.json["active_invite"])
        self.assertNotIn("url", duplicate.json)

        replacement = self.create_invite(replace_active=True)
        self.assertEqual(replacement.status_code, 201, replacement.json)
        second_token = self.invite_token(replacement)
        self.assertNotEqual(first_token, second_token)
        self.assertIsNotNone(invite.invalidated_at)
        self.assertEqual(self.client.get(f"/api/cadastro/{first_token}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/cadastro/{second_token}").status_code, 200)

    def test_invite_invalid_expired_and_used_tokens_have_the_same_response(self):
        cpf_expired = "52998224725"
        cpf_used = "11144477735"
        expired_token = "expired-invite-token"
        used_token = "used-invite-token"
        admin_id = Admin.query.filter_by(username="test-admin").one().id
        db.session.add_all(
            [
                RegistrationInvite(
                    cpf_hash=digest(cpf_expired),
                    cpf_encrypted=encrypt(cpf_expired),
                    token_hash=digest(expired_token),
                    created_at=utcnow() - timedelta(days=8),
                    expires_at=utcnow() - timedelta(days=1),
                    created_by=admin_id,
                ),
                RegistrationInvite(
                    cpf_hash=digest(cpf_used),
                    cpf_encrypted=encrypt(cpf_used),
                    token_hash=digest(used_token),
                    created_at=utcnow(),
                    expires_at=utcnow() + timedelta(days=7),
                    used_at=utcnow(),
                    created_by=admin_id,
                ),
            ]
        )
        db.session.commit()

        invalid = self.client.get("/api/cadastro/not-a-token")
        expired = self.client.get(f"/api/cadastro/{expired_token}")
        used = self.client.get(f"/api/cadastro/{used_token}")
        self.assertEqual([invalid.status_code, expired.status_code, used.status_code], [404] * 3)
        self.assertEqual(invalid.json, expired.json)
        self.assertEqual(invalid.json, used.json)
        self.assertEqual(
            self.client.post(f"/api/cadastro/{expired_token}", data={}).json,
            invalid.json,
        )

    def test_invite_registration_uses_link_cpf_and_consumes_invite_once(self):
        generated = self.create_invite()
        self.assertEqual(generated.status_code, 201)
        token = self.invite_token(generated)
        validated = self.client.get(f"/api/cadastro/{token}")
        self.assertEqual(validated.status_code, 200)
        self.assertEqual(validated.json["cpf"], "52998224725")

        registration = self.invited_registration(token, cpf="11144477735")
        self.assertEqual(registration.status_code, 201, registration.json)
        participant = Participant.query.one()
        self.assertEqual(decrypt(participant.cpf_encrypted), "52998224725")
        self.assertEqual(participant.cpf_hash, digest("52998224725"))
        self.assertNotEqual(participant.cpf_hash, digest("11144477735"))
        self.assertIsNotNone(RegistrationInvite.query.one().used_at)
        self.assertEqual(
            self.client.post(
                f"/api/cadastro/{token}",
                data={"cpf": "52998224725"},
            ).status_code,
            404,
        )

    def test_invite_registration_checks_duplicates_per_selected_activity(self):
        existing = self.register()
        self.assertEqual(existing.status_code, 201)
        generated = self.create_invite()
        self.assertEqual(generated.status_code, 201)
        token = self.invite_token(generated)

        duplicate = self.invited_registration(token, activity_type="audience")
        self.assertEqual(duplicate.status_code, 409)
        self.assertIsNone(RegistrationInvite.query.one().used_at)

        event_id = self.create_event()
        other_activity = self.invited_registration(
            token,
            activity_type="event",
            activity_id=event_id,
        )
        self.assertEqual(other_activity.status_code, 201, other_activity.json)
        self.assertEqual(EventParticipant.query.count(), 1)

    def test_invite_validation_is_rate_limited(self):
        responses = [
            self.client.get("/api/cadastro/invalid-rate-limit-token")
            for _ in range(31)
        ]
        self.assertEqual(responses[-1].status_code, 429)
        self.assertIn("Retry-After", responses[-1].headers)

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
        self.assertIn(b'href="/evento"', response.data)
        self.assertIn(b'href="/admin"', response.data)
        self.assertIn("Cadastro de participante de audiência".encode("utf-8"), response.data)
        self.assertIn("Inscrição de participante de evento".encode("utf-8"), response.data)
        self.assertIn("Área administrativa".encode("utf-8"), response.data)

    def test_public_registration_page_keeps_existing_ui_and_replaces_local_storage_code(self):
        response = self.client.get("/cadastro")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"PLEITOS", response.data)
        self.assertIn(b"/static/js/public.js", response.data)
        self.assertIn(b"/static/css/public-layout.css", response.data)
        self.assertIn(b'class="home-link" href="/">In\xc3\xadcio</a>', response.data)
        self.assertIn(b'window.matchMedia("(max-width: 650px)")', response.data)
        self.assertNotIn(b'data-view="web"', response.data)
        self.assertNotIn(b'data-view="mobile"', response.data)
        self.assertNotIn(b"const KEY='pleitos_access_v1'", response.data)
        self.assertNotIn(b"localStorage", response.data)
        self.assertNotIn(b"let people=", response.data)
        self.assertIn(b"personDocument", response.data)

    def test_event_registration_page_has_separate_entry_point(self):
        response = self.client.get("/evento")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/static/js/event.js", response.data)
        self.assertIn(b"/static/css/event.css", response.data)
        self.assertIn(b'id="eventRegistrationForm"', response.data)

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
