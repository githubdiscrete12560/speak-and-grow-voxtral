"""Firestore adapter contract tests without production credentials or network."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from backend import backend


class Snapshot:
    def __init__(self, data):
        self.data = data
        self.exists = data is not None
    def to_dict(self):
        return copy.deepcopy(self.data)


class Reference:
    def __init__(self, database, path):
        self.database, self.path = database, path
    def get(self, transaction=None):
        return Snapshot(copy.deepcopy(self.database.data.get(self.path)))


class Collection:
    def __init__(self, database, name):
        self.database, self.name = database, name
    def document(self, key):
        return Reference(self.database, (self.name, key))


class Transaction:
    def __init__(self, database):
        self.database, self.writes = database, []
    def create(self, ref, data):
        if ref.path in self.database.data:
            raise RuntimeError('Already exists')
        self.writes.append((ref.path, copy.deepcopy(data)))
    def set(self, ref, data):
        self.writes.append((ref.path, copy.deepcopy(data)))


class Database:
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
    def collection(self, name):
        return Collection(self, name)
    def transaction(self):
        return Transaction(self)


def transactional(fn):
    def run(transaction):
        with transaction.database.lock:
            result = fn(transaction)
            for key, value in transaction.writes:
                transaction.database.data[key] = value
            return result
    return run


def test_firestore_atomic_creation_lookup_conflict_and_updates():
    database = Database()
    repo = backend.FirestoreOrderRepository(database, 'orders')
    record = {'order_id': 'order-one', 'access_digest': 'key', 'request_digest': 'payload', 'payment_status': 'pending_payment'}
    with patch('google.cloud.firestore.transactional', transactional):
        with ThreadPoolExecutor(max_workers=5) as pool:
            values = list(pool.map(lambda _: repo.create(record), range(5)))
        assert values == [record] * 5
        assert len(database.data) == 2
        assert repo.find_by_token('key') == record
        assert repo.find_by_token('missing') is None
        with pytest.raises(HTTPException) as error:
            repo.create({**record, 'request_digest': 'changed'})
        assert error.value.status_code == 409
        assert repo.find_by_token('key') == record
        result = repo.update('order-one', lambda old: {**old, 'payment_status': 'expired'})
        assert result['payment_status'] == 'expired'
        assert repo.find_by_token('key')['payment_status'] == 'expired'
        with pytest.raises(HTTPException) as error:
            repo.update('missing', lambda old: old)
        assert error.value.status_code == 404
        def rejected(old):
            old['payment_status'] = 'paid'
            raise HTTPException(404, 'Invalid token')
        with pytest.raises(HTTPException):
            repo.update('order-one', rejected)
        assert repo.find_by_token('key')['payment_status'] == 'expired'


def test_firestore_client_configuration_is_lazy_and_uses_managed_database():
    backend.get_book_repository.cache_clear()
    with patch.dict(backend.os.environ, {'BOOK_ORDER_STORAGE': 'firestore', 'BOOK_ORDERS_PROJECT_ID': 'test-project',
                                        'BOOK_ORDERS_DATABASE': 'test-db', 'BOOK_ORDERS_COLLECTION': 'test_orders'}), \
         patch('google.cloud.firestore.Client') as factory:
        repo = backend.get_book_repository()
        assert isinstance(repo, backend.FirestoreOrderRepository)
        factory.assert_called_once_with(project='test-project', database='test-db')
        assert backend.get_book_repository() is repo
    backend.get_book_repository.cache_clear()
