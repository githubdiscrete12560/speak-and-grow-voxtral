"""Offline Stripe contract/security tests; no provider network requests."""
import copy
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import patch

import pytest
from backend import backend
from test_book_orders import client, repository, payload, headers, MERCHANT_TEST_ENV


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for name, value in {"STRIPE_SECRET_KEY": "sk_test_synthetic_only",
                        "STRIPE_WEBHOOK_SECRET": "whsec_synthetic_only",
                        "STRIPE_CURRENCY": "thb", "STRIPE_PAYMENT_METHOD_TYPES": "",
                        "STRIPE_SUCCESS_URL": "https://example.com/?payment=success&session_id={CHECKOUT_SESSION_ID}",
                        "STRIPE_CANCEL_URL": "https://example.com/?payment=cancelled"}.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def sdk():
    with patch.object(backend, "get_stripe_client") as factory:
        factory.return_value.v1.checkout.sessions.create.return_value = {
            "id": "cs_test_synthetic", "url": "https://checkout.stripe.com/c/pay/synthetic", "livemode": False}
        yield factory.return_value.v1.checkout.sessions.create


def checkout(client, order=None):
    order = order or payload(payment_method="stripe")
    created = client.post("/api/book-orders", json=order).json()
    path = "/api/book-orders/" + created["order_id"] + "/stripe-checkout"
    return order, created, path


def event_for(record, kind="checkout.session.completed", status="paid"):
    return {"id": "evt_synthetic", "type": kind, "livemode": False, "data": {"object": {
        "id": record["stripe_checkout_session_id"], "livemode": False, "mode": "payment",
        "client_reference_id": record["order_id"], "metadata": {
            "order_id": record["order_id"], "payment_reference": record["stripe_payment_reference"]},
        "currency": "thb", "amount_total": record["total_amount"] * 100,
        "payment_status": status, "payment_intent": "pi_synthetic"}}}


def deliver(client, event, secret="whsec_synthetic_only", timestamp=None):
    raw = json.dumps(event).encode()
    timestamp = timestamp or int(time.time())
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256).hexdigest()
    return client.post("/api/stripe/webhook", content=raw,
                       headers={"Stripe-Signature": f"t={timestamp},v1={digest}"})


@pytest.mark.parametrize("items,amount", [
    ([{"book_id": "health", "quantity": 1}], 30000),
    ([{"book_id": "health", "quantity": 2}, {"book_id": "guidance", "quantity": 1}], 90000)])
def test_frozen_integer_prices_and_retries(client, repository, sdk, items, amount):
    order, created, path = checkout(client, payload(payment_method="stripe", items=items))
    result = client.post(path, headers=headers(order), json={"total_amount": 1, "payment_status": "paid"})
    assert result.status_code == 200
    assert set(result.json()) == {"session_id", "checkout_url"}
    params = sdk.call_args.args[0]
    assert all(line["price_data"]["unit_amount"] == 30000 for line in params["line_items"])
    assert sum(line["quantity"] * line["price_data"]["unit_amount"] for line in params["line_items"]) == amount
    assert "payment_method_types" not in params
    assert params["success_url"].endswith("{CHECKOUT_SESSION_ID}")
    assert client.post(path, headers=headers(order)).json() == result.json()
    assert sdk.call_count == 1
    assert repository.records[created["order_id"]]["payment_status"] == "checkout_created"
    assert client.get('/api/book-orders/' + created['order_id'] + '?payment=success', headers=headers(order)).json()['paid_at'] is None


def test_access_and_terminal_orders(client, repository, sdk):
    order, created, path = checkout(client)
    assert client.post(path).status_code == 401
    assert client.post(path, headers=headers(payload())).status_code == 404
    assert client.post(path.replace(created["order_id"], "BSJ-20261025-" + "A"*24), headers=headers(order)).status_code == 404
    for status in ["paid", "cancelled", "expired", "failed", "processing"]:
        repository.records[created["order_id"]]["payment_status"] = status
        assert client.post(path, headers=headers(order)).status_code == 409
    repository.records[created["order_id"]]["payment_status"] = "pending_payment"
    with patch.object(backend, "book_now", return_value=datetime.fromisoformat(created["expires_at"])):
        assert client.post(path, headers=headers(order)).status_code == 409
    sdk.assert_not_called()


@pytest.mark.parametrize("name,value", [("STRIPE_SECRET_KEY", ""), ("STRIPE_SECRET_KEY", "sk_live_forbidden"),
    ("STRIPE_WEBHOOK_SECRET", ""), ("STRIPE_CURRENCY", "usd"), ("STRIPE_SUCCESS_URL", "javascript:bad")])
def test_config_fails_safely(client, sdk, monkeypatch, name, value):
    order, _, path = checkout(client)
    monkeypatch.setenv(name, value)
    response = client.post(path, headers=headers(order))
    assert response.status_code == 503
    if value:
        assert value not in response.text
    sdk.assert_not_called()


def test_signature_verification(client):
    assert client.post('/api/stripe/webhook', content=b'{}').status_code == 400
    assert deliver(client, {"type": "unrelated"}, secret="wrong").status_code == 400
    assert deliver(client, {"type": "unrelated"}, timestamp=int(time.time())-600).status_code == 400
    assert deliver(client, {"type": "unrelated"}).status_code == 200


def test_verified_paid_is_atomic_idempotent(client, repository, sdk):
    order, created, path = checkout(client)
    client.post(path, headers=headers(order))
    record = repository.records[created["order_id"]]
    event = event_for(record)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(result.status_code == 200 for result in pool.map(lambda _: deliver(client, event), range(4)))
    paid = copy.deepcopy(repository.records[created["order_id"]])
    assert paid["payment_status"] == "paid" and paid["paid_at"] and paid["stripe_payment_intent_id"] == "pi_synthetic"
    event["id"] = "evt_another"
    assert deliver(client, event).status_code == 200
    assert deliver(client, event_for(paid, "checkout.session.async_payment_failed", "unpaid")).status_code == 200
    assert repository.records[created["order_id"]] == paid
    assert client.post(path, headers=headers(order)).status_code == 409


@pytest.mark.parametrize("field,value", [("amount_total", 1), ("currency", "usd"), ("id", "cs_wrong"),
    ("livemode", True), ("mode", "subscription"), ("client_reference_id", "wrong")])
def test_mismatched_webhooks_never_pay(client, repository, sdk, field, value):
    order, created, path = checkout(client)
    client.post(path, headers=headers(order))
    event = event_for(repository.records[created["order_id"]])
    event["data"]["object"][field] = value
    assert deliver(client, event).status_code == 400
    assert repository.records[created["order_id"]]["paid_at"] is None


def test_async_and_late_delivery(client, repository, sdk):
    order, created, path = checkout(client)
    client.post(path, headers=headers(order))
    record = repository.records[created["order_id"]]
    assert deliver(client, event_for(record, status="unpaid")).status_code == 200
    assert repository.records[created["order_id"]]["payment_status"] == "processing"
    assert client.post(path, headers=headers(order)).status_code == 409
    assert deliver(client, event_for(record, "checkout.session.async_payment_failed", "unpaid")).status_code == 200
    assert repository.records[created["order_id"]]["payment_status"] == "failed"
    assert deliver(client, event_for(record, "checkout.session.async_payment_succeeded")).status_code == 200
    assert repository.records[created["order_id"]]["payment_status"] == "paid"


def test_retry_after_network_or_persistence_failure(client, repository, sdk):
    order, created, path = checkout(client)
    sdk.side_effect = RuntimeError('secret provider details')
    assert client.post(path, headers=headers(order)).status_code == 503
    first = sdk.call_args
    sdk.side_effect = None
    assert client.post(path, headers=headers(order)).status_code == 200
    assert sdk.call_args == first


def test_providers_cannot_overlap(client, repository, sdk):
    order, created, path = checkout(client)
    client.post(path, headers=headers(order))
    with patch.dict(backend.os.environ, MERCHANT_TEST_ENV):
        assert client.post(path.replace('/stripe-checkout', '/payment'), headers=headers(order)).status_code == 409
    order2, _, path2 = checkout(client)
    with patch.dict(backend.os.environ, MERCHANT_TEST_ENV):
        assert client.post(path2.replace('/stripe-checkout', '/payment'), headers=headers(order2)).status_code == 200
    assert client.post(path2, headers=headers(order2)).status_code == 409


def test_concurrent_checkout_uses_identical_provider_parameters(client, repository, sdk):
    order, created, path = checkout(client)
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: client.post(path, headers=headers(order)), range(5)))
    assert all(response.status_code == 200 for response in responses)
    assert all(call == sdk.call_args_list[0] for call in sdk.call_args_list)
    assert len({response.json()["session_id"] for response in responses}) == 1


def test_failed_attach_and_early_webhook_recover(client, repository, sdk):
    order, created, path = checkout(client)
    original_update = repository.update
    def fail_attach(order_id, transform):
        if transform.__name__ == "attach":
            raise RuntimeError("synthetic persistence outage")
        return original_update(order_id, transform)
    with patch.object(repository, 'update', side_effect=fail_attach):
        assert client.post(path, headers=headers(order)).status_code == 503
    reserved = repository.records[created["order_id"]]
    event = event_for({**reserved, "stripe_checkout_session_id": "cs_test_synthetic"})
    assert deliver(client, event).status_code == 503
    assert reserved["paid_at"] is None
    params = sdk.call_args
    assert client.post(path, headers=headers(order)).status_code == 200
    assert sdk.call_args == params
    assert deliver(client, event).status_code == 200
    assert repository.records[created["order_id"]]["payment_status"] == "paid"


def test_metadata_binding_and_unpaid_success(client, repository, sdk):
    order, created, path = checkout(client)
    client.post(path, headers=headers(order))
    record = repository.records[created["order_id"]]
    event = event_for(record)
    event["data"]["object"]["metadata"]["payment_reference"] = "wrong"
    assert deliver(client, event).status_code == 400
    event = event_for(record, "checkout.session.async_payment_succeeded", "unpaid")
    assert deliver(client, event).status_code == 200
    assert repository.records[created["order_id"]]["paid_at"] is None
    repository.records[created["order_id"]]["payment_status"] = "expired"
    assert deliver(client, event_for(record)).status_code == 200
    assert repository.records[created["order_id"]]["payment_status"] == "paid"


def test_dashboard_method_configuration_is_frozen(client, repository, sdk, monkeypatch):
    monkeypatch.setenv("STRIPE_PAYMENT_METHOD_TYPES", "card,promptpay")
    order, _, path = checkout(client)
    sdk.side_effect = RuntimeError("offline")
    assert client.post(path, headers=headers(order)).status_code == 503
    params = sdk.call_args
    assert params.args[0]["payment_method_types"] == ["card", "promptpay"]
    monkeypatch.setenv("STRIPE_PAYMENT_METHOD_TYPES", "card")
    sdk.side_effect = None
    assert client.post(path, headers=headers(order)).status_code == 200
    assert sdk.call_args == params


def test_availability_only_reports_valid_direct_config(client, monkeypatch):
    monkeypatch.setenv('PROMPTPAY_ID', '')
    assert client.get('/api/book-payment-methods').json() == {"preferred": "stripe", "direct_promptpay": False}
    for name, value in MERCHANT_TEST_ENV.items():
        monkeypatch.setenv(name, value)
    response = client.get('/api/book-payment-methods')
    assert response.json()["direct_promptpay"] is True
    assert response.headers['cache-control'] == 'no-store'


def test_stripe_webhook_uses_firestore_transaction(client, sdk):
    from test_book_firestore import Database, transactional
    database = Database()
    repo = backend.FirestoreOrderRepository(database, 'orders')
    with patch.object(backend, 'get_book_repository', return_value=repo), \
         patch('google.cloud.firestore.transactional', transactional):
        order, created, path = checkout(client)
        assert client.post(path, headers=headers(order)).status_code == 200
        record = repo.find_by_token(backend.digest_book_token(order['idempotency_key']))
        event = event_for(record)
        assert deliver(client, event).status_code == 200
        paid = copy.deepcopy(database.data)
        assert deliver(client, event).status_code == 200
        assert database.data == paid
        assert repo.find_by_token(backend.digest_book_token(order['idempotency_key']))['payment_status'] == 'paid'
