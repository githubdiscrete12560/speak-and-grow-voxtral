# Book checkout: Stripe test mode and direct PromptPay

## Architecture

The existing Firestore order and private UUIDv4 bearer credential remain in use.
`POST /api/book-orders` freezes server catalog titles, integer THB prices and quantities;
unknown browser price/status fields are rejected. Five books cost 300 THB each.
New orders default to `payment_method=stripe`, `payment_status=pending_payment`.
Explicit `promptpay` remains compatible. No consultation, OAuth, Calendar, Netlify,
profile or donation behavior is changed.

`POST /api/book-orders/{order_id}/stripe-checkout` requires the private order bearer
credential. It rejects missing, expired, paid, failed, cancelled and processing orders.
It converts frozen integer prices to satang (300 THB = 30000), creates a hosted
Checkout Session in payment mode with the official Stripe Python SDK, and returns
only `session_id` and `checkout_url`. Browser-supplied totals/statuses are ignored.
Stripe secret credentials never enter frontend JavaScript.

Before calling Stripe, a Firestore transaction reserves a stable payment reference,
return URLs, methods and expiry. The reference is also the Stripe idempotency key.
Concurrent requests and retries use identical parameters and reuse the same session.
No external API call runs inside a Firestore transaction. If session persistence
fails, retry the same order; do not create another order. A webhook arriving before
session persistence gets 503 so Stripe can retry after the session is saved.

Stripe requires Checkout expiry at least 30 minutes after session creation. The
first reservation sets the order/session deadline to 30 minutes plus 30 seconds
from reservation; this deliberately refreshes the original order's 30-minute window
once. Retries never extend it. If the API cannot create the session before that
minimum window closes, it fails safely; the order eventually expires. Cancel returns
keep the existing session available until expiry, rather than creating another one.

The frontend offers Stripe as the preferred method and redirects only to HTTPS
checkout.stripe.com. Success/cancel query parameters are display hints. On return,
the existing sessionStorage bearer credential recovers the order and authenticated
polling reads backend status. A success redirect never writes payment status.
The buyer sees payment confirmation only after the API reports `paid`. Lost guest
credentials (closed/cleared browser session) still require support; email/account
recovery is outside this implementation. No tokens or buyer details enter URLs or
localStorage. The cart continues to store only IDs and quantities.

## Webhook security and state

`POST /api/stripe/webhook` is publicly reachable without browser session auth, but
always verifies the raw request body and Stripe-Signature using the signing secret
and Stripe SDK timestamp tolerance. Register a **snapshot** event destination for:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`
- `checkout.session.async_payment_failed`
- `checkout.session.expired`

Before any transition it checks test mode on event and session, payment mode,
stored session ID, client reference, metadata order ID and internal payment reference,
amount_total against frozen Firestore total * 100, and currency against THB.
Invalid signatures/mismatches return 400. Storage failures return sanitized 503 for
retry. Unsupported event types are acknowledged without changing an order.

Only a verified matching completed/succeeded event with payment_status=paid marks
the order paid. Completed but unpaid sessions enter processing; async failure enters
failed. Processing orders cannot create a second session and remain pollable even
past their checkout deadline. Paid is absorbing: duplicate deliveries, different
event IDs for the same success, and out-of-order failure/expiry cannot undo it.
Updates are Firestore transactions. Delayed verified success reconciles a locally
expired/failed order so actual receipt is not lost. No fulfillment side effects are
implemented; any future fulfillment worker must also use an idempotent order key.

Safe internal provider fields: `stripe_payment_reference`,
`stripe_checkout_session_id`, `stripe_checkout_url`, `stripe_checkout_expires_at`,
`stripe_success_url`, `stripe_cancel_url`, `stripe_payment_methods`,
`stripe_payment_intent_id`, `stripe_payment_status`, `stripe_paid_event_id`;
existing `payment_reference` and `paid_at` are set on verified payment. Private
provider fields are excluded from public order responses. Card details, raw webhook
payloads and secrets are never stored or logged. No browser status-update API exists.

## Configuration and Stripe Dashboard

Install `backend/requirements.txt` (official SDK, StripeClient v1 services).
Use a Stripe test account/sandbox and test-mode Dashboard. This implementation
intentionally rejects live keys and live-mode webhook objects. It is not enabled
for real charges. No real keys, deployment, commits or Dashboard mutations are included.

Server configuration (placeholders are in `.env.example`):

- Secret-backed: `STRIPE_SECRET_KEY=sk_test_...`, `STRIPE_WEBHOOK_SECRET=whsec_...`.
- Normal variables: `STRIPE_CURRENCY=thb`, `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL`.
- Example success: `https://example.netlify.app/?payment=success&session_id={CHECKOUT_SESSION_ID}`.
- Example cancel: `https://example.netlify.app/?payment=cancelled`.
- Optional `STRIPE_PAYMENT_METHOD_TYPES`: empty by default, allowing Stripe Dashboard
  dynamic payment methods. An explicit comma-separated list, such as card,promptpay,
  is available when required. The configuration is frozen per session attempt.

Stripe documents PromptPay support for Thailand business accounts, THB, and Checkout
payment mode (not setup/subscription mode). Enable PromptPay in the test Dashboard
if available for the account; this repository cannot inspect account eligibility.
No frontend rewrite is required to change Stripe-supported methods.

Official references:
[Checkout Sessions](https://docs.stripe.com/api/checkout/sessions/create?lang=python),
[PromptPay and Dashboard configuration](https://docs.stripe.com/payments/promptpay),
[Webhook signatures and CLI](https://docs.stripe.com/webhooks).

## Firestore, Cloud Run and Secret Manager

Retain `BOOK_ORDER_STORAGE=firestore`, `BOOK_ORDERS_PROJECT_ID`,
`BOOK_ORDERS_DATABASE=(default)`, `BOOK_ORDERS_COLLECTION=book_orders` and its
companion request-key collection. Use Application Default Credentials and narrowly
scoped Firestore IAM. There is no memory/disk production fallback. Restrict direct
browser database access. Do not configure TTL deletion on order expiry; retain history.
Preserve existing Google/OAuth secret bindings and environment configuration.

For production, store STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET in Google Secret
Manager and bind them as **secret-backed environment variables** on Cloud Run.
Grant the runtime service account Secret Manager Secret Accessor on those secrets
only. Do not put secrets in source, frontend config, plain deployment variables,
command history or logs. Use separate test/staging and production secrets. Return
URLs must use HTTPS in production (localhost HTTP allowed only in development).
No Cloud Run/Secret Manager changes are performed here.

## Local testing

Automated tests mock Stripe SDK session calls, use synthetic webhook signatures and
test-only repositories/transactional Firestore fakes. They make no Stripe network calls.

```powershell
.\.venv\Scripts\python.exe -m pytest
node tests/test_books_frontend.cjs
node tests/test_consultation_frontend.cjs
git diff --check
```

For an optional manual test, install the official Stripe CLI, run `stripe login`
in test mode, and forward snapshot events:

```powershell
stripe listen --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired --forward-to localhost:8000/api/stripe/webhook
```

Set the CLI listener's signing secret in the local backend environment (it differs
from the Dashboard endpoint secret). Restart the backend with test key, test Firestore
and local return URLs, create an order through the UI, and complete its actual test
Checkout Session. Generic `stripe trigger` fixtures lack the stored session/order
binding and must not mark a real app order paid. Test cancel, delayed webhook, invalid
signature, duplicate delivery, expired order and backend outage. Confirm that return
alone remains unpaid. Never use real payment details for test-mode exercises.

## Staging deployment checklist (manual; nothing deployed here)

- Provision dedicated test Firestore and narrowly scoped service identity.
- Bind test Stripe key and matching webhook signing secret from Secret Manager.
- Set HTTPS frontend return URLs, CORS origins, currency and backend URL.
- Register the public HTTPS `/api/stripe/webhook` snapshot destination and four events.
- Enable desired test payment methods in Dashboard, check Thailand eligibility.
- Complete test Checkout, cancel/resume, async, replay and restart/multi-instance tests.
- Inspect Firestore safe references, frozen amounts and backend-only paid transition.
- Monitor webhook delivery failures without logging secrets/buyer payloads.

## Production launch checklist (future review required)

- Explicitly review/remove the test-only key/event guards before any live launch;
  keep a strict environment-specific test/live match. This work does not authorize it.
- Complete Stripe business activation and live payment-method eligibility checks.
- Provision separate live secrets and HTTPS webhook destination with correct signing
  secret/API snapshot version; bind secrets through Secret Manager.
- Validate managed Firestore transactions, backups, IAM, retention and audit policy.
- Define guest recovery, shipping, refunds, late receipts, reconciliation and
  idempotent fulfillment before taking real orders.
- Add operational webhook alerts, reconciliation and shared rate/abuse controls;
  the existing in-process limiter is not distributed across Cloud Run instances.
- Run final staging acceptance and consultation/OAuth/Calendar regressions.

## Direct PromptPay retained

`GET /api/book-payment-methods` exposes only availability flags. The direct button is
hidden unless PROMPTPAY_ID validates and PROMPTPAY_MERCHANT_NAME is configured.
`POST /api/book-orders/{order_id}/payment` retains existing bearer-protected frozen
QR instructions and awaiting_verification status. Once either provider is reserved,
the other cannot be initiated for that order, preventing duplicate payment routes.

Direct QR is generated locally with python-qrcode, Thai QR tag 29, THB 764 and CRC16.
Displaying/scanning it does not prove receipt; equal amounts can yield equal QR codes
and the order ID is not a bank-enforced transfer reference. Saved QR images cannot
be revoked by the app. DirectPromptPayProvider still refuses verification (503);
there is no direct-bank receipt integration/admin paid override. Resolve any real
direct transfers manually with the merchant; never infer paid from a browser click.
