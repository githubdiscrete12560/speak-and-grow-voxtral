import asyncio
import base64
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from pydantic import ValidationError

from backend import backend


def valid_payload(**overrides):
    payload = {
        "guardian_name": "Parent Example",
        "guardian_email": "parent@example.com",
        "child_display_name": "Child",
        "child_age_range": "6-8 years",
        "consultation_reason": "General speech-language consultation request.",
        "appointment_start": datetime.now(timezone.utc) + timedelta(days=2),
        "duration_minutes": 45,
        "timezone": "Asia/Bangkok",
        "consent_confirmed": True,
    }
    payload.update(overrides)
    return payload


class ExecuteCall:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeEvents:
    def __init__(self):
        self.insert_kwargs = None

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return ExecuteCall({
            "id": "event_123",
            "htmlLink": "https://calendar.google.com/event",
            "hangoutLink": "https://meet.google.com/example",
        })


class FakeService:
    def __init__(self):
        self.event_resource = FakeEvents()

    def events(self):
        return self.event_resource


class FakeOAuthFlow:
    def __init__(self, state="stored-state", code_verifier="stored-verifier"):
        self.state = state
        self.code_verifier = code_verifier
        self.credentials = object()
        self.fetch_token_kwargs = None

    def authorization_url(self, **kwargs):
        return "https://accounts.google.com/o/oauth2/auth", self.state

    def fetch_token(self, **kwargs):
        self.fetch_token_kwargs = kwargs


GOOGLE_ENV = {
    "GOOGLE_CLIENT_ID": "test-client-id",
    "GOOGLE_CLIENT_SECRET": "test-client-secret",
    "GOOGLE_REDIRECT_URI": "http://localhost:8000/auth/google/callback",
}


def signed_session(values):
    encoded = base64.b64encode(json.dumps(values).encode("utf-8"))
    return TimestampSigner(str(backend.SESSION_SECRET)).sign(encoded).decode("utf-8")


def decoded_session(cookie):
    encoded = TimestampSigner(str(backend.SESSION_SECRET)).unsign(cookie.encode("utf-8"))
    return json.loads(base64.b64decode(encoded))


class ConsultationTests(unittest.TestCase):
    def test_health_still_works(self):
        result = asyncio.run(backend.health())
        self.assertEqual(result["status"], "ok")

    def test_google_status_not_configured_without_environment(self):
        with patch.dict(os.environ, {
            "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "", "GOOGLE_REDIRECT_URI": ""
        }):
            result = asyncio.run(backend.google_auth_status())
        self.assertEqual(result, {"configured": False, "authorized": False})

    def test_missing_consent_is_rejected(self):
        booking = backend.ConsultationRequest(**valid_payload(consent_confirmed=False))
        with self.assertRaises(HTTPException) as caught:
            backend.create_consultation(booking)
        self.assertEqual(caught.exception.status_code, 400)

    def test_past_date_is_rejected(self):
        booking = backend.ConsultationRequest(**valid_payload(
            appointment_start=datetime.now(timezone.utc) - timedelta(minutes=1)
        ))
        with self.assertRaises(HTTPException) as caught:
            backend.create_consultation(booking)
        self.assertEqual(caught.exception.status_code, 400)

    def test_invalid_email_is_rejected(self):
        with self.assertRaises(ValidationError):
            backend.ConsultationRequest(**valid_payload(guardian_email="not-an-email"))

    def test_unsupported_duration_is_rejected(self):
        with self.assertRaises(ValidationError):
            backend.ConsultationRequest(**valid_payload(duration_minutes=15))

    def test_conflict_returns_409(self):
        booking = backend.ConsultationRequest(**valid_payload())
        with patch.object(backend, "get_google_calendar_service", return_value=FakeService()), \
             patch.object(backend, "check_calendar_conflict", return_value=True):
            with self.assertRaises(HTTPException) as caught:
                backend.create_consultation(booking)
        self.assertEqual(caught.exception.status_code, 409)

    def test_insert_options_and_unique_conference_request_ids(self):
        booking = backend.ConsultationRequest(**valid_payload())
        event_one = backend.build_calendar_event(
            booking, booking.appointment_start, booking.appointment_start + timedelta(minutes=45)
        )
        event_two = backend.build_calendar_event(
            booking, booking.appointment_start, booking.appointment_start + timedelta(minutes=45)
        )
        self.assertNotEqual(
            event_one["conferenceData"]["createRequest"]["requestId"],
            event_two["conferenceData"]["createRequest"]["requestId"],
        )
        service = FakeService()
        with patch.object(backend, "get_google_calendar_service", return_value=service), \
             patch.object(backend, "check_calendar_conflict", return_value=False):
            backend.create_consultation(booking)
        self.assertEqual(service.event_resource.insert_kwargs["conferenceDataVersion"], 1)
        self.assertEqual(service.event_resource.insert_kwargs["sendUpdates"], "all")

    def test_meet_url_extraction_variants(self):
        self.assertEqual(
            backend.extract_meet_url({"hangoutLink": "https://meet.google.com/direct"}),
            "https://meet.google.com/direct",
        )
        self.assertEqual(
            backend.extract_meet_url({"conferenceData": {"entryPoints": [
                {"entryPointType": "phone", "uri": "tel:123"},
                {"entryPointType": "video", "uri": "https://meet.google.com/video"},
            ]}}),
            "https://meet.google.com/video",
        )

    def test_join_window(self):
        start = datetime.now(timezone.utc) + timedelta(minutes=9)
        end = start + timedelta(minutes=45)
        self.assertTrue(backend.calculate_join_allowed(start, end))
        self.assertFalse(backend.calculate_join_allowed(start + timedelta(hours=1), end + timedelta(hours=1)))

    def test_voxtral_route_is_registered(self):
        paths = {getattr(route, "path", "") for route in backend.app.routes}
        self.assertIn("/ws/pronunciation", paths)


class GoogleOAuthPkceTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend.app, follow_redirects=False)

    def tearDown(self):
        self.client.close()

    def test_login_stores_state_and_code_verifier_in_signed_session(self):
        login_flow = FakeOAuthFlow()
        with patch.dict(os.environ, GOOGLE_ENV), \
             patch.object(backend, "create_google_oauth_flow", return_value=login_flow) as factory:
            response = self.client.get("/auth/google/login")
        self.assertEqual(response.status_code, 307)
        factory.assert_called_once_with(autogenerate_code_verifier=True)
        session = decoded_session(self.client.cookies.get(backend.SESSION_COOKIE_NAME))
        self.assertEqual(session["google_oauth_state"], "stored-state")
        self.assertEqual(session["google_code_verifier"], "stored-verifier")

    def test_callback_restores_stored_code_verifier_and_saves_credentials(self):
        login_flow = FakeOAuthFlow()
        callback_flow = FakeOAuthFlow()
        with patch.dict(os.environ, GOOGLE_ENV), \
             patch.object(backend, "create_google_oauth_flow", return_value=login_flow):
            self.client.get("/auth/google/login")
        with patch.dict(os.environ, GOOGLE_ENV), \
             patch.object(backend, "create_google_oauth_flow", return_value=callback_flow) as factory, \
             patch.object(backend, "save_google_credentials") as save_credentials:
            response = self.client.get("/auth/google/callback?state=stored-state&code=test-code")
        self.assertEqual(response.status_code, 307)
        factory.assert_called_once_with(state="stored-state", code_verifier="stored-verifier")
        self.assertEqual(callback_flow.fetch_token_kwargs, {"code": "test-code"})
        save_credentials.assert_called_once_with(callback_flow.credentials)

    def test_callback_rejects_missing_code_verifier(self):
        self.client.cookies.set(backend.SESSION_COOKIE_NAME, signed_session({"google_oauth_state": "stored-state"}))
        response = self.client.get("/auth/google/callback?state=stored-state&code=test-code")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Google authorization code verifier is missing.")

    def test_callback_rejects_mismatched_state(self):
        self.client.cookies.set(backend.SESSION_COOKIE_NAME, signed_session({
            "google_oauth_state": "stored-state",
            "google_code_verifier": "stored-verifier",
        }))
        response = self.client.get("/auth/google/callback?state=different-state&code=test-code")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Google authorization state does not match.")

    def test_oauth_flow_requests_event_and_freebusy_scopes(self):
        expected_scopes = [
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.events.freebusy",
        ]
        fake_flow = MagicMock()
        with patch.dict(os.environ, GOOGLE_ENV), \
             patch("google_auth_oauthlib.flow.Flow.from_client_config", return_value=fake_flow) as factory:
            result = backend.create_google_oauth_flow(autogenerate_code_verifier=True)
        self.assertIs(result, fake_flow)
        self.assertEqual(factory.call_args.kwargs["scopes"], expected_scopes)
        self.assertEqual(backend.GOOGLE_SCOPES, expected_scopes)

    def test_credential_loading_uses_both_scopes(self):
        credentials = object()
        token_json = '{"token":"test","client_id":"id","client_secret":"secret","refresh_token":"refresh","token_uri":"https://oauth2.googleapis.com/token"}'
        with patch.object(backend, "load_google_token_from_file", return_value=token_json), \
             patch("google.oauth2.credentials.Credentials.from_authorized_user_info", return_value=credentials) as loader:
            result = backend.load_google_credentials()
        self.assertIs(result, credentials)
        loader.assert_called_once_with(json.loads(token_json), backend.GOOGLE_SCOPES)


if __name__ == "__main__":
    unittest.main()


