import json
import base64
import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import backend


class MemoryOrders:
    """Test-only implementation. Production never falls back to memory."""
    def __init__(self):
        self.records = {}
        self.keys = {}
        self.lock = threading.RLock()

    def create(self, record):
        with self.lock:
            if record["access_digest"] in self.keys:
                existing = self.records[self.keys[record["access_digest"]]]
                if existing["request_digest"] != record["request_digest"]:
                    raise HTTPException(409, "Different retry payload")
                return copy.deepcopy(existing)
            self.records[record["order_id"]] = copy.deepcopy(record)
            self.keys[record["access_digest"]] = record["order_id"]
            return copy.deepcopy(record)

    def find_by_token(self, access_digest):
        with self.lock:
            order_id = self.keys.get(access_digest)
            return copy.deepcopy(self.records.get(order_id))

    def update(self, order_id, transform):
        with self.lock:
            if order_id not in self.records:
                raise HTTPException(404, "Not found")
            updated = transform(copy.deepcopy(self.records[order_id]))
            self.records[order_id] = copy.deepcopy(updated)
            return updated


@pytest.fixture
def repository():
    return MemoryOrders()


@pytest.fixture
def client(repository):
    with patch.object(backend, "enforce_rate_limit"), \
         patch.object(backend, "get_book_repository", return_value=repository), \
         TestClient(backend.app) as value:
        yield value


def payload(**changes):
    result = {"buyer_name": "ผู้สั่งซื้อ", "buyer_email": "reader@example.com",
              "buyer_phone": None, "idempotency_key": str(uuid.uuid4()),
              "items": [{"book_id": "guidance", "quantity": 2},
                        {"book_id": "health", "quantity": 1}]}
    result.update(changes)
    return result


def headers(order):
    return {"Authorization": "Bearer " + order["idempotency_key"]}


@pytest.mark.parametrize("items,amount", [
    ([{"book_id": "health", "quantity": 1}], 300),
    ([{"book_id": "health", "quantity": 2}], 600),
    ([{"book_id": "health", "quantity": 2}, {"book_id": "guidance", "quantity": 1}], 900),
])
def test_order_prices_status_and_no_buyer_data(client, repository, items, amount):
    order = payload(items=items)
    result = client.post("/api/book-orders", json=order)
    assert result.status_code == 200
    data = result.json()
    assert data["total_amount"] == amount
    assert data["total_quantity"] == amount // 300
    assert data["currency"] == "THB"
    assert data["payment_status"] == "pending_payment"
    assert data["paid_at"] is None and data["payment_reference"] is None
    assert re.fullmatch(r"BSJ-[0-9]{8}-[A-F0-9]{24}", data["order_id"])
    assert datetime.fromisoformat(data["expires_at"]) - datetime.fromisoformat(data["created_at"]) == timedelta(minutes=30)
    assert all(item["unit_price"] == 300 for item in data["items"])
    assert "buyer_email" not in data and "buyer_name" not in data
    assert "access_digest" not in data and "idempotency_key" not in data
    assert result.headers["cache-control"] == "no-store"
    assert repository.records[data["order_id"]]["buyer_email"] == order["buyer_email"]
    status = client.get('/api/book-orders/' + data['order_id'], headers=headers(order))
    assert status.json() == data
    assert client.get('/api/book-orders/session/current', headers=headers(order)).json() == data


@pytest.mark.parametrize("quantity", [0, -1, 100, 1.5, True, "2", None])
def test_invalid_quantities(client, quantity):
    response = client.post("/api/book-orders", json=payload(items=[{"book_id": "health", "quantity": quantity}]))
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("changes", [
    {"buyer_name": "   "}, {"buyer_name": "x" * 121},
    { "buyer_email": "invalid"}, {"buyer_phone": "0" * 31},
    {"buyer_phone": "hello"}, {"buyer_phone": "123"}, {"payment_status": "paid"},
    {"payment_method": "card"}, {"idempotency_key": "predictable"},
    {"items": []}, {"items": [{"book_id": "missing", "quantity": 1}]},
    {"items": [{"book_id": "health", "quantity": 1}] * 2},
    {"items": [{"book_id": "health", "quantity": 1}] * 6},
    {"items": [{"book_id": "health", "quantity": 1, "price": 1}]},
    {"total": 1}, {"status": "paid"},
])
def test_invalid_order_fields(client, changes):
    response = client.post("/api/book-orders", json=payload(**changes))
    assert response.status_code == 422


def test_all_books_and_quantity_limit(client):
    response = client.post("/api/book-orders", json=payload(
        items=[{"book_id": book_id, "quantity": 99} for book_id in backend.BOOK_CATALOG]))
    assert response.status_code == 200
    assert response.json()["total_amount"] == 5 * 99 * 300
    assert response.json()["total_quantity"] == 495


def test_missing_email_and_malformed_json(client):
    order = payload()
    del order["buyer_email"]
    assert client.post("/api/book-orders", json=order).status_code == 422
    assert client.post("/api/book-orders", content="{", headers={"Content-Type": "application/json"}).status_code == 422


def test_rate_limit_uses_isolated_scope(client):
    with patch.object(backend, "enforce_rate_limit", side_effect=HTTPException(429, "Too many requests")) as limiter:
        assert client.post("/api/book-orders", json=payload()).status_code == 429
        assert limiter.call_args.args[1:] == ("book-orders", 20, 60)


def test_catalog_matches_frontend_and_utf8():
    root = Path(__file__).parents[1]
    source = (root / "frontend/books.js").read_text(encoding="utf-8")
    catalog = json.loads(re.search(r"const BOOKS = (\[.*?\]);", source, re.S).group(1))
    assert len(catalog) == 5
    assert len({book["id"] for book in catalog}) == 5
    for book in catalog:
        assert {key: book[key] for key in ("title", "author", "price")} == backend.BOOK_CATALOG[book["id"]]
        assert book["author"] == "รศ.ดร. จันทร์เพ็ญ ภูโสภา"
        assert book["price"] == 300
    for filename in ["frontend/index.html", "frontend/books.js", "frontend/books.css", "backend/backend.py"]:
        data = (root / filename).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        decoded = data.decode("utf-8")
        for bad in ["â", "Ã", "ðŸ", "�"]:
            assert bad not in decoded
    html = (root / "frontend/index.html").read_text(encoding="utf-8")
    assert '<meta charset="UTF-8">' in html


def test_retry_is_atomic_and_payload_cannot_change(client, repository):
    order = payload()
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: client.post("/api/book-orders", json=order), range(5)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["order_id"] for response in responses}) == 1
    assert len(repository.records) == 1
    assert client.post("/api/book-orders", json={**order, "buyer_name": "Changed"}).status_code == 409
    assert len(repository.records) == 1


def test_missing_storage_fails_closed_without_fallback():
    backend.get_book_repository.cache_clear()
    with patch.dict(backend.os.environ, {"BOOK_ORDER_STORAGE": "", "BOOK_ORDERS_PROJECT_ID": ""}), \
         patch.object(backend, "enforce_rate_limit"), TestClient(backend.app) as client:
        response = client.post("/api/book-orders", json=payload())
    assert response.status_code == 503
    backend.get_book_repository.cache_clear()


def test_storage_failure_is_sanitized(client):
    with patch.object(backend, "get_book_repository", side_effect=RuntimeError("private credentials")):
        response = client.post("/api/book-orders", json=payload())
    assert response.status_code == 503
    assert "private credentials" not in response.text


def test_status_requires_order_token_and_is_read_only(client):
    order = payload()
    created = client.post("/api/book-orders", json=order).json()
    path = '/api/book-orders/' + created['order_id']
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers(payload())).status_code == 404
    assert client.post(path, json={"payment_status": "paid"}, headers=headers(order)).status_code == 405
    assert client.patch(path, json={"payment_status": "paid"}, headers=headers(order)).status_code == 405
    assert client.get(path, headers=headers(order)).json()["payment_status"] == "pending_payment"


# Deliberately synthetic values, not a real merchant/payment account.
MERCHANT_TEST_ENV = {"PROMPTPAY_ID": "08" + "0" * 8, "PROMPTPAY_MERCHANT_NAME": "TEST ONLY"}


def test_qr_stays_unpaid_is_exact_and_idempotent(client, repository):
    order = payload()
    created = client.post("/api/book-orders", json=order).json()
    path = '/api/book-orders/' + created['order_id'] + '/payment'
    assert client.post(path).status_code == 401
    with patch.dict(backend.os.environ, MERCHANT_TEST_ENV):
        result = client.post(path, headers=headers(order), json={"total_amount": 1, "payment_status": "paid"})
    assert result.status_code == 200
    data = result.json()
    assert data["order"]["payment_status"] == "awaiting_verification"
    assert data["order"]["paid_at"] is None
    assert data["order"]["total_amount"] == 900
    assert b'<svg' in base64.b64decode(data["qr_image"].split(',')[1])
    record = repository.records[created["order_id"]]
    assert '5406900.00' in record['payment_instruction']['qr_payload']
    # Existing instructions are frozen even if deployment config later changes.
    with patch.dict(backend.os.environ, {"PROMPTPAY_ID": "", "PROMPTPAY_MERCHANT_NAME": ""}):
        again = client.post(path, headers=headers(order)).json()
    assert again == data
    status = client.get('/api/book-orders/' + created['order_id'], headers=headers(order)).json()
    assert 'payment_instruction' not in status and 'qr_payload' not in status
    assert 'merchant_name' not in status


def test_missing_promptpay_configuration_never_marks_paid(client):
    order = payload()
    created = client.post("/api/book-orders", json=order).json()
    with patch.dict(backend.os.environ, {"PROMPTPAY_ID": "YOUR_PROMPTPAY_ID", "PROMPTPAY_MERCHANT_NAME": "YOUR_MERCHANT_NAME"}):
        response = client.post('/api/book-orders/' + created['order_id'] + '/payment', headers=headers(order))
    assert response.status_code == 503
    assert client.get('/api/book-orders/' + created['order_id'], headers=headers(order)).json()['payment_status'] == 'pending_payment'


def test_expiration_preserves_history_and_blocks_qr(client, repository):
    order = payload()
    created = client.post("/api/book-orders", json=order).json()
    with patch.dict(backend.os.environ, MERCHANT_TEST_ENV):
        client.post('/api/book-orders/' + created['order_id'] + '/payment', headers=headers(order))
    expiry = datetime.fromisoformat(created['expires_at'])
    with patch.object(backend, 'book_now', return_value=expiry):
        assert client.get('/api/book-orders/' + created['order_id'], headers=headers(order)).json()['payment_status'] == 'expired'
        assert client.post('/api/book-orders/' + created['order_id'] + '/payment', headers=headers(order)).status_code == 409
        retry = client.post('/api/book-orders', json=order).json()
        assert retry['order_id'] == created['order_id'] and retry['payment_status'] == 'expired'
    assert len(repository.records) == 1


def test_expiry_race_during_qr_creation(client, repository):
    order = payload()
    created = client.post("/api/book-orders", json=order).json()
    expiry = datetime.fromisoformat(created['expires_at'])
    with patch.object(backend, 'book_now', side_effect=[expiry-timedelta(seconds=1), expiry]), \
         patch.dict(backend.os.environ, MERCHANT_TEST_ENV):
        result = client.post('/api/book-orders/' + created['order_id'] + '/payment', headers=headers(order))
    assert result.status_code == 409
    assert repository.records[created['order_id']]['payment_status'] == 'expired'


@pytest.mark.parametrize('state', ['paid', 'cancelled'])
def test_terminal_states_do_not_expire_or_issue_qr(client, repository, state):
    order = payload()
    created = client.post("/api/book-orders", json=order).json()
    repository.records[created['order_id']]['payment_status'] = state  # trusted test fixture only
    with patch.object(backend, 'book_now', return_value=datetime.fromisoformat(created['expires_at'])+timedelta(days=1)):
        assert client.get('/api/book-orders/' + created['order_id'], headers=headers(order)).json()['payment_status'] == state
        assert client.post('/api/book-orders/' + created['order_id'] + '/payment', headers=headers(order)).status_code == 409


def test_direct_provider_cannot_verify_payment():
    provider = backend.DirectPromptPayProvider()
    for action in [lambda: provider.get_payment_status('reference'),
                   lambda: provider.verify_callback(b'{"paid":true}', {}),
                   lambda: provider.verify_payment('order', 'reference')]:
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 503


def test_promptpay_tags_amount_and_independent_crc():
    payload = backend.build_promptpay_payload(MERCHANT_TEST_ENV['PROMPTPAY_ID'], 600)
    fields = {}
    cursor = 0
    while cursor < len(payload):
        tag, length = payload[cursor:cursor+2], int(payload[cursor+2:cursor+4])
        fields[tag] = payload[cursor+4:cursor+4+length]
        cursor += 4 + length
    assert fields['00'] == '01' and fields['01'] == '12'
    assert fields['58'] == 'TH' and fields['53'] == '764' and fields['54'] == '600.00'
    assert fields['29'] == '0016A00000067701011101130066' + MERCHANT_TEST_ENV['PROMPTPAY_ID'][1:]
    crc = 0xffff
    for value in payload[:-4].encode('ascii'):
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xffff if crc & 0x8000 else (crc << 1) & 0xffff
    assert fields['63'] == f'{crc:04X}'
    assert '0213' + '0'*13 in backend.build_promptpay_payload('0'*13, 300)
    with pytest.raises(ValueError):
        backend.build_promptpay_payload('YOUR_PROMPTPAY_ID', 300)
