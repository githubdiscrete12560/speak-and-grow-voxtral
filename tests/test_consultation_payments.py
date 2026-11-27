from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from httplib2 import Response

from backend import backend
from test_book_firestore import Database, transactional
from test_consultations import future_slot, valid_payload
from test_stripe_payments import deliver, settings


@pytest.fixture
def flow(monkeypatch, settings):
    repository = backend.FirestoreConsultationRepository(Database())
    monkeypatch.setattr('google.cloud.firestore.transactional', transactional)
    monkeypatch.setattr(backend, "get_consultation_repository", lambda: repository)
    monkeypatch.setattr(backend, "enforce_rate_limit", lambda *args: None)
    monkeypatch.setattr(backend, "check_calendar_conflict", lambda *args: False)
    events = MagicMock()
    events.get.return_value.execute.side_effect = HttpError(Response({"status": "404"}), b"{}")
    events.insert.return_value.execute.return_value = {"hangoutLink": "https://meet.google.com/example"}
    calendar = MagicMock()
    calendar.events.return_value = events
    monkeypatch.setattr(backend, "get_google_calendar_service", lambda: calendar)
    stripe = MagicMock()
    sessions = stripe.v1.checkout.sessions
    sessions.create.return_value = {"id": "cs_test_consultation", "livemode": False,
                                    "url": "https://checkout.stripe.com/c/pay/synthetic"}
    monkeypatch.setattr(backend, "get_stripe_client", lambda _: stripe)
    with TestClient(backend.app) as client:
        yield client, repository, events, sessions


def auth():
    return {"Authorization": "Bearer " + str(uuid4())}


def details(minor=False, **changes):
    data = valid_payload(appointment_start=future_slot().isoformat(), under_20=minor,
                         consent_confirmed=not minor)
    if minor:
        data.update(legal_guardian_name="Legal Guardian", legal_guardian_email="guardian@example.com",
                    guardian_relationship="Mother", guardian_consent_confirmed=True)
    return {**data, **changes}


def start(flow, data=None, headers=None, checkout=True):
    client, repository, _, _ = flow
    headers = headers or auth()
    response = client.post("/api/consultations", json=data or details(), headers=headers)
    assert response.status_code == 201, response.text
    identifier = response.json()["consultation_id"]
    path = "/api/consultations/" + identifier
    if checkout:
        response = client.post(path + "/stripe-checkout", headers=headers, json={"amount": 1})
        assert response.status_code == 200, response.text
    return path, headers, repository.read(identifier)


def event_for(record, kind="completed", payment="paid"):
    identifier = record["consultation_id"]
    return {"id": "evt_consult", "type": "checkout.session." + kind, "livemode": False,
            "data": {"object": {
                "id": record["stripe_checkout_session_id"], "livemode": False, "mode": "payment",
                "client_reference_id": identifier, "amount_total": record["consultation_fee"] * 100,
                "currency": "thb", "payment_status": payment,
                "metadata": {"payment_type": "consultation", "consultation_id": identifier,
                             "payment_reference": record["stripe_payment_reference"]},
            }}}


@pytest.mark.parametrize("minor,amount,methods", [
    (False, 49900, "card"), (True, 29900, "promptpay"), (True, 29900, "card,promptpay")])
def test_fee_checkout_and_no_calendar_before_payment(flow, monkeypatch, minor, amount, methods):
    client, repository, events, sessions = flow
    monkeypatch.setenv("STRIPE_PAYMENT_METHOD_TYPES", methods)
    data = details(minor, amount=1, price=2, total_amount=3, payment_status="paid")
    path, headers, record = start(flow, data)
    assert record["consultation_fee"] == amount // 100
    params = sessions.create.call_args.args[0]
    price = params["line_items"][0]["price_data"]
    assert (price["unit_amount"], price["currency"]) == (amount, "thb")
    assert params["payment_method_types"] == methods.split(",")
    assert set(params["metadata"]) == {"payment_type", "consultation_id", "payment_reference"}
    assert params["success_url"] == "https://sabaijai.netlify.app/?consultation_payment=return#consultation"
    assert params["cancel_url"] == "https://sabaijai.netlify.app/?consultation_payment=cancel#consultation"
    assert start(flow, data, headers)[0] == path
    sessions.create.assert_called_once()
    assert sum(coll == "consultation_orders" for coll, _ in repository.client.data) == 1
    assert client.get(path + "/status", headers=auth()).status_code == 404
    assert client.get(path + "/status").status_code == 401
    status = client.get(path + "/status?payment=success", headers=headers).json()
    assert status["payment_status"] == "pending_payment" and not status["meet_available"]
    assert "guardian" not in str(status)
    events.insert.assert_not_called()


@pytest.mark.parametrize("minor,change", [
    (False, {"consent_confirmed": False}),
    (True, {"legal_guardian_name": " "}),
    (True, {"legal_guardian_email": None}),
    (True, {"guardian_relationship": " "}),
    (True, {"guardian_consent_confirmed": False}),
])
def test_required_consent(flow, minor, change):
    client, repository, _, _ = flow
    assert client.post("/api/consultations", headers=auth(), json=details(minor, **change)).status_code == 400
    assert not repository.client.data


@pytest.mark.parametrize("field,value", [
    ("amount_total", 1), ("id", "cs_wrong"), ("currency", "usd"),
    ("client_reference_id", "wrong"), ("livemode", True),
    ("metadata.consultation_id", "cslt" + "0" * 32), ("metadata.payment_reference", "wrong"),
])
def test_signed_mismatches_rejected(flow, field, value):
    client, _, events, _ = flow
    path, headers, record = start(flow)
    event = event_for(record)
    session = event["data"]["object"]
    if field.startswith("metadata."):
        session["metadata"][field.split(".")[1]] = value
    else:
        session[field] = value
    assert deliver(client, event).status_code in {400, 404}
    assert client.get(path + "/status", headers=headers).json()["payment_status"] == "pending_payment"
    events.insert.assert_not_called()


@pytest.mark.parametrize("minor", [True, False])
def test_payment_and_duplicate_webhook_create_one_appointment(flow, monkeypatch, minor):
    client, _, events, _ = flow
    path, headers, record = start(flow, details(minor))
    event = event_for(record)
    assert deliver(client, event, secret="incorrect").status_code == 400
    assert deliver(client, event).status_code == 200
    assert deliver(client, event).status_code == 200
    events.insert.assert_called_once()
    insert = events.insert.call_args.kwargs
    assert insert["sendUpdates"] == "all" and insert["conferenceDataVersion"] == 1
    assert "sendNotifications" not in insert
    body = insert["body"]
    start_time = datetime.fromisoformat(record["appointment_start"])
    end_time = start_time + timedelta(minutes=50)
    assert body["start"] == {"dateTime": start_time.isoformat(), "timeZone": "Asia/Bangkok"}
    assert body["end"] == {"dateTime": end_time.isoformat(), "timeZone": "Asia/Bangkok"}
    assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert events.get.call_args.kwargs["eventId"] == body["id"]
    assert len(body["id"]) == 64
    expected = "guardian@example.com" if minor else "parent@example.com"
    assert [attendee["email"] for attendee in insert["body"]["attendees"]] == [expected]
    status = client.get(path + "/status", headers=headers).json()
    assert status["payment_status"] == "paid" and status["booking_status"] == "confirmed"
    assert not status["meet_url"]
    monkeypatch.setattr(backend, "calculate_join_allowed", lambda *_: True)
    assert client.get(path + "/status", headers=headers).json()["meet_url"].startswith("https://meet.google.com/")


def test_temporary_calendar_failure_and_async_success(flow):
    client, _, events, _ = flow
    path, headers, record = start(flow)
    assert deliver(client, event_for(record, payment="unpaid")).status_code == 200
    assert client.get(path + "/status", headers=headers).json()["payment_status"] == "processing"
    events.insert.return_value.execute.side_effect = [RuntimeError("temporary"), {"hangoutLink": "https://meet.google.com/example"}]
    paid = event_for(record, "async_payment_succeeded")
    assert deliver(client, paid).status_code == 503
    status = client.get(path + "/status", headers=headers).json()
    assert (status["payment_status"], status["booking_status"]) == ("paid", "calendar_pending")
    assert deliver(client, paid).status_code == 200
    assert client.get(path + "/status", headers=headers).json()["booking_status"] == "confirmed"


@pytest.mark.parametrize("kind,expected", [("async_payment_failed", "failed"), ("expired", "expired")])
def test_terminal_payment_releases_slot(flow, kind, expected):
    client, _, events, _ = flow
    path, headers, record = start(flow)
    assert deliver(client, event_for(record, kind, "unpaid")).status_code == 200
    assert client.get(path + "/status", headers=headers).json()["payment_status"] == expected
    events.insert.assert_not_called()
    assert start(flow, checkout=False)[0] != path


@pytest.mark.parametrize("stripe_status,expected", [("expired", "expired"), ("complete", "pending_payment")])
def test_expiry_checks_stripe_before_releasing(flow, monkeypatch, stripe_status, expected):
    client, _, _, sessions = flow
    path, headers, record = start(flow)
    monkeypatch.setattr(backend, "book_now", lambda: datetime.fromtimestamp(record["expires_at"], timezone.utc) + timedelta(seconds=1))
    sessions.retrieve.return_value = {
        "id": record["stripe_checkout_session_id"], "livemode": False,
        "status": stripe_status, "payment_status": "unpaid",
    }
    assert client.get(path + "/status", headers=headers).json()["payment_status"] == expected
    response = client.post("/api/consultations", headers=auth(), json=details())
    assert response.status_code == (201 if stripe_status == "expired" else 409)


def test_two_concurrent_clients_cannot_start_same_slot(flow):
    client, _, _, _ = flow
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: client.post("/api/consultations", headers=auth(), json=details()).status_code,
                                 range(2)))
    assert sorted(outcomes) == [201, 409]


def test_calendar_conflict_prevents_pending_record(flow, monkeypatch):
    client, repository, events, sessions = flow
    monkeypatch.setattr(backend, "check_calendar_conflict", lambda *_: True)
    assert client.post("/api/consultations", headers=auth(), json=details()).status_code == 409
    assert not repository.client.data
    sessions.create.assert_not_called()
    events.insert.assert_not_called()

def test_verified_late_payment_never_books_reassigned_slot(flow):
    client, repository, events, _ = flow
    path, headers, record = start(flow)
    assert deliver(client, event_for(record, "expired", "unpaid")).status_code == 200
    _, _, new_owner = start(flow, checkout=False)
    assert deliver(client, event_for(record, "async_payment_succeeded")).status_code == 200
    status = client.get(path + "/status", headers=headers).json()
    assert (status["payment_status"], status["booking_status"]) == ("paid", "booking_failed")
    assert repository.owner(record["slot"])["consultation_id"] == new_owner["consultation_id"]
    events.insert.assert_not_called()


def test_retry_recovers_calendar_event_when_insert_response_is_lost(flow):
    client, _, events, _ = flow
    path, headers, record = start(flow)
    paid = event_for(record)
    events.insert.return_value.execute.side_effect = TimeoutError("lost response")
    assert deliver(client, paid).status_code == 503
    events.get.return_value.execute.side_effect = None
    events.get.return_value.execute.return_value = {"hangoutLink": "https://meet.google.com/recovered"}
    assert deliver(client, paid).status_code == 200
    assert events.get.call_args.kwargs["eventId"] == events.insert.call_args.kwargs["body"]["id"]
    events.insert.assert_called_once()
    assert client.get(path + "/status", headers=headers).json()["booking_status"] == "confirmed"
