import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import backend


@pytest.fixture
def cors_client():
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://example.netlify.app"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    with TestClient(app) as client:
        yield client


def test_cors_allowed_origin_and_credentials(cors_client):
    response = cors_client.get("/health", headers={"Origin": "https://example.netlify.app"})
    assert response.headers["access-control-allow-origin"] == "https://example.netlify.app"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_rejected_origin(cors_client):
    response = cors_client.get("/health", headers={"Origin": "https://unknown.example"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_preflight(cors_client):
    response = cors_client.options("/health", headers={
        "Origin": "https://example.netlify.app",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://example.netlify.app"


def test_websocket_allowed_origins_and_rejection():
    with patch.dict(os.environ, {"FRONTEND_ORIGINS": "https://site.netlify.app,http://localhost:5500", "MISTRAL_API_KEY": ""}):
        with TestClient(backend.app) as client:
            for origin in ("https://site.netlify.app", "http://localhost:5500"):
                with client.websocket_connect("/ws/pronunciation", headers={"origin": origin}) as websocket:
                    assert websocket.receive_json()["type"] == "error"
            with pytest.raises(WebSocketDisconnect) as closed:
                with client.websocket_connect("/ws/pronunciation", headers={"origin": "https://unknown.example"}):
                    pass
            assert closed.value.code == 1008


def test_oauth_callback_redirects_to_frontend():
    class Flow:
        credentials = MagicMock()
        def fetch_token(self, **kwargs):
            pass
    with TestClient(backend.app, follow_redirects=False) as client, \
         patch.dict(os.environ, {"FRONTEND_PUBLIC_URL": "https://site.netlify.app"}), \
         patch.object(backend, "create_google_oauth_flow", return_value=Flow()), \
         patch.object(backend, "save_google_credentials"):
        client.cookies.set(backend.SESSION_COOKIE_NAME, _signed_session({"google_oauth_state": "state", "google_code_verifier": "verifier"}))
        response = client.get("/auth/google/callback?state=state&code=code")
    assert response.headers["location"] == "https://site.netlify.app/?google_calendar=connected"


def _signed_session(values):
    import base64
    from itsdangerous import TimestampSigner
    payload = base64.b64encode(json.dumps(values).encode())
    return TimestampSigner(str(backend.SESSION_SECRET)).sign(payload).decode()


def test_session_cookie_configuration():
    middleware = next(item for item in backend.app.user_middleware if item.cls.__name__ == "SessionMiddleware")
    assert middleware.kwargs["session_cookie"] == "speak_grow_session"
    assert middleware.kwargs["same_site"] == "lax"
    assert middleware.kwargs["https_only"] is backend.PRODUCTION


def test_file_token_mode_round_trip():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GOOGLE_TOKEN_STORAGE": "file", "GOOGLE_TOKEN_FILE": str(Path(directory) / "token.json")}):
        backend.save_google_token_to_file('{"token":"value"}')
        assert backend.load_google_token_from_file() == '{"token":"value"}'


def _fake_secret_modules(client, not_found_type):
    secretmanager = types.ModuleType("google.cloud.secretmanager")
    secretmanager.SecretManagerServiceClient = lambda: client
    cloud = types.ModuleType("google.cloud")
    cloud.secretmanager = secretmanager
    return patch.dict(sys.modules, {"google.cloud": cloud, "google.cloud.secretmanager": secretmanager})


def test_secret_manager_missing_and_latest_load():
    from google.api_core.exceptions import NotFound
    client = MagicMock()
    client.access_secret_version.side_effect = NotFound("missing")
    with patch.dict(os.environ, {"GCP_PROJECT_ID": "project", "GOOGLE_TOKEN_SECRET_NAME": "token"}), _fake_secret_modules(client, NotFound):
        assert backend.load_google_token_from_secret_manager() is None
    client.access_secret_version.side_effect = None
    client.access_secret_version.return_value.payload.data = b'{"token":"stored"}'
    with patch.dict(os.environ, {"GCP_PROJECT_ID": "project", "GOOGLE_TOKEN_SECRET_NAME": "token"}), _fake_secret_modules(client, NotFound):
        assert backend.load_google_token_from_secret_manager() == '{"token":"stored"}'


def test_secret_manager_creates_secret_and_adds_version():
    from google.api_core.exceptions import NotFound
    client = MagicMock()
    client.get_secret.side_effect = NotFound("missing")
    with patch.dict(os.environ, {"GCP_PROJECT_ID": "project", "GOOGLE_TOKEN_SECRET_NAME": "token"}), _fake_secret_modules(client, NotFound):
        backend.save_google_token_to_secret_manager('{"token":"new"}')
    client.create_secret.assert_called_once()
    request = client.add_secret_version.call_args.kwargs["request"]
    assert request["payload"]["data"] == b'{"token":"new"}'


def test_refreshed_credentials_are_persisted():
    credentials = MagicMock(expired=True, refresh_token="refresh", valid=True)
    service = object()
    with patch.object(backend, "load_google_credentials", return_value=credentials), \
         patch.object(backend, "save_google_credentials") as save, \
         patch("googleapiclient.discovery.build", return_value=service):
        assert backend.get_google_calendar_service() is service
    credentials.refresh.assert_called_once()
    save.assert_called_once_with(credentials)


def test_backend_does_not_require_frontend_and_health():
    assert not (Path(backend.__file__).parent / "index.html").exists()
    with TestClient(backend.app) as client:
        assert client.get("/").json() == {"service": "Speak and Grow API", "status": "ok"}
        assert client.get("/health").status_code == 200


def test_frontend_public_config_and_no_secrets():
    root = Path(__file__).parents[1]
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
    config = (root / "frontend" / "config.js").read_text(encoding="utf-8")
    assert '<script src="./config.js"></script>' in html
    assert "BACKEND_WS_URL" in html and "${BACKEND_URL}/api/consultations" in html
    assert "http://localhost:8000" in config
    combined = html + config
    for forbidden in ("MISTRAL_API_KEY", "GOOGLE_CLIENT_SECRET", "SESSION_SECRET", "google_token.json"):
        assert forbidden not in combined
