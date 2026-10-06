# Book orders and PromptPay

This implements real order records backed by Firestore when configured. Without
managed storage, checkout returns 503. There is no production memory/disk fallback.
No deployment, Google/OAuth configuration, or live merchant identifiers are included.

## Configuration (server only)

Install `backend/requirements.txt`. Create a Firestore Native-mode database and
configure the following separately in your deployment; `.env.example` has placeholders:

- `BOOK_ORDER_STORAGE=firestore`
- `BOOK_ORDERS_PROJECT_ID`: project containing the managed database
- `BOOK_ORDERS_DATABASE`: database ID, default `(default)`
- `BOOK_ORDERS_COLLECTION`: default `book_orders`; companion collection
  `book_orders_requests` holds hashed idempotency-key lookups
- `PROMPTPAY_ID`: a registered Thai mobile proxy (10 digits, starting 06/08/09),
  or registered 13-digit tax/national proxy, digits only
- `PROMPTPAY_MERCHANT_NAME`: display name to compare with the recipient in the bank app

Use Application Default Credentials and a narrowly scoped backend service account
with Firestore read/write access (for example a database-scoped `roles/datastore.user`
assignment where supported). Never expose Firestore directly to browser clients;
restrict IAM and client Security Rules. Verify database region, backups, access audit,
PII retention, and shipping/fulfillment terms before accepting real orders.
Do not set Firestore TTL deletion on `expires_at`: expired records are historical orders.
No changes to Cloud Run settings are made by this implementation.

## API and storage

`POST /api/book-orders` accepts buyer information, `items` (book_id and integer
quantity 1–99), payment_method `promptpay`, and a cryptographically random UUIDv4
`idempotency_key`. Unknown fields, including browser prices/totals/statuses, are
rejected. The server snapshots all book titles, prices (300 THB), quantities and totals.

The UUID is also the client's private order-access credential. It is kept in
sessionStorage, not URLs or localStorage; the database stores only its SHA-256 hash.
Never log request bodies or Authorization headers at the proxy/application layer.
Both order creation and key reservation are one Firestore transaction. Reusing a key
returns the existing record; different data with the same key returns 409. Clients
must retry the SAME key after timeouts, never automatically generate a new key.
There is no deduplication across intentionally different keys/devices.

`GET /api/book-orders/{order_id}` and `GET /api/book-orders/session/current` require
`Authorization: Bearer <idempotency_key>`. They return safe order fields without buyer
PII or merchant configuration. Recovery by key handles a lost POST response. Closing
or clearing the browser session loses this guest credential; account/email recovery
is not implemented and must be designed before general launch.

Order IDs use BSJ, Bangkok date, and 96 random bits. Firestore stores immutable buyer
and item snapshots, total_amount (integer THB), payment_method, payment_status,
created_at, expires_at (30 minutes), paid_at, and payment_reference. The last two are
null until a trusted verification integration is implemented. Responses are no-store.

`POST /api/book-orders/{order_id}/payment` also requires the bearer credential. It
creates/fetches frozen payment instructions from server order data, then atomically
moves only an active unpaid order to `awaiting_verification`. It does not accept a
client amount or status. The returned QR image encodes the merchant proxy by design;
the status endpoint never returns that proxy. QR instructions are stored internally
so retries cannot redirect a previously created order after environment changes.

## What PromptPay QR does and does not do

The direct transfer QR uses Thai QR tag 29, PromptPay AID A000000677010111, THB currency
764, an exact server-calculated amount and CRC16-CCITT. QR rendering uses python-qrcode
locally; it sends nothing to a third-party QR service. Mobile and tax/national proxies
are supported, not e-wallet/bill-payment/cross-border variants.

The order ID is shown beside the QR. It is NOT a bank-enforced transfer reference.
Two equal-amount orders can yield the same QR. Showing/scanning/returning to a QR page
cannot prove receipt. Merchant display name is configuration, not a bank-verified
recipient name: buyers must check the actual recipient in their banking app.

Expiration is enforced at every server read/payment operation and again atomically
when issuing instructions. The UI hides expired QR images. Direct-transfer QR images
cannot be revoked at a bank: a saved image may still transfer money after expiry.
Late/duplicate transfers require reconciliation/refund by the merchant. Expiration
does not prove that a bank transfer failed. Do not tell buyers to pay again merely
because the app expired their order.

## Verification and launch boundary

New orders start `pending_payment`. Instructions move them to `awaiting_verification`.
Only a future trusted provider/admin system may record `paid`; there is deliberately
no public status-update, fake confirmation, or unsigned webhook route. The default
DirectPromptPayProvider refuses all verification calls (503).

`PaymentProvider` defines create_payment, get_payment_status, verify_callback and
verify_payment. A real adapter/admin workflow must verify actual receipt, order binding,
amount, currency, merchant account and settlement timestamp; atomically reserve unique
provider references; handle callback authentication, replay/idempotency, audit records,
late receipts, refunds and reconciliation. Authenticated administrator verification
is not implemented. Do not bypass this by editing status from a browser.

Production-oriented code is present for persistence, pricing, authenticated guest
reads, retries, expiry and QR instructions. Actual deployment/IAM, real recipient
configuration, bank-app QR acceptance tests, authenticated verification, recovery,
monitoring/distributed abuse controls and fulfillment are still launch requirements.
The existing in-process limiter is not a distributed Cloud Run rate limiter.

## Verification

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest
node tests/test_books_frontend.cjs
node tests/test_consultation_frontend.cjs
git diff --check
```

Automated tests use test-only repositories/synthetic merchant values, never a real
bank account. Firestore adapter tests use a transactional fake, not a live database.
Validate against a dedicated managed test database before production use.

References:
- https://promptpay.readthedocs.io/en/latest/_modules/promptpay/qrcode.html
- https://github.com/lincolnloop/python-qrcode
- https://firebase.google.com/docs/firestore/manage-data/transactions
