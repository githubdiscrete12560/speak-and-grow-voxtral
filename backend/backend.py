import asyncio
import base64
import binascii
import hashlib
import re
import json
import logging
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import lru_cache
from typing import AsyncIterator, Callable, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from mistralai.client import Mistral
from mistralai.client.models import (
    AudioFormat,
    RealtimeTranscriptionError,
    RealtimeTranscriptionSessionCreated,
    TranscriptionStreamDone,
    TranscriptionStreamTextDelta,
)
from mistralai.extra.realtime import UnknownRealtimeEvent
from pydantic import BaseModel, ConfigDict, EmailStr, Field, UUID4, field_validator
from starlette.middleware.sessions import SessionMiddleware

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL = "voxtral-mini-transcribe-realtime-2602"
BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Speak & Grow Voxtral Backend")
logger = logging.getLogger("speak_and_grow.calendar")


def configured_frontend_origins() -> list[str]:
    """Return normalized, explicitly configured browser origins."""
    return [
        origin.strip().rstrip("/")
        for origin in os.getenv("FRONTEND_ORIGINS", "").split(",")
        if origin.strip()
    ]


FRONTEND_ORIGINS = configured_frontend_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
]
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(48)
PRODUCTION = os.getenv("ENVIRONMENT", "development").strip().lower() == "production"
SESSION_COOKIE_NAME = "speak_grow_session"
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=PRODUCTION,
    session_cookie=SESSION_COOKIE_NAME,
)


class InMemoryRateLimiter:
    """Single-instance safety net; use Cloud Armor or a shared store in production."""

    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        requests = self._requests[key]
        while requests and requests[0] < cutoff:
            requests.popleft()
        if len(requests) >= limit:
            return False
        requests.append(now)
        return True


rate_limiter = InMemoryRateLimiter()


def enforce_rate_limit(request: Request, scope: str, limit: int, window_seconds: int) -> None:
    client = request.client.host if request.client else "unknown"
    if not rate_limiter.check(f"{scope}:{client}", limit, window_seconds):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")


class ConsultationRequest(BaseModel):
    guardian_name: str = Field(min_length=1, max_length=120)
    guardian_email: EmailStr
    child_display_name: str | None = Field(default=None, max_length=80)
    child_age_range: str | None = Field(default=None, max_length=40)
    consultation_reason: str = Field(min_length=10, max_length=1000)
    appointment_start: datetime
    duration_minutes: Literal[50] = 50
    timezone: str = Field(default="Asia/Bangkok", max_length=64)
    # Legacy guardian_name/email keys identify the client; retain API compatibility.
    consent_confirmed: bool = False
    under_20: bool = False
    legal_guardian_name: str | None = Field(default=None, max_length=120)
    legal_guardian_email: EmailStr | None = None
    guardian_relationship: str | None = Field(default=None, max_length=120)
    guardian_consent_confirmed: bool = False

    @field_validator("guardian_name")
    @classmethod
    def guardian_name_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Guardian name is required.")
        return value

    @field_validator("consultation_reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not 10 <= len(value) <= 1000:
            raise ValueError("Consultation reason must be between 10 and 1000 characters.")
        return value


class ConsultationResponse(BaseModel):
    event_id: str
    html_link: str
    meet_url: str
    start_time: datetime
    end_time: datetime
    timezone: str
    status: str
    message: str


@app.exception_handler(RequestValidationError)
async def consultation_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    if request.url.path == "/api/consultations":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "กรุณาตรวจสอบข้อมูลนัดหมาย: ชื่อ อีเมล วันเวลาพร้อมเขตเวลา และระยะเวลา 50 นาที"},
        )
    if request.url.path.startswith("/api/book-orders"):
        return JSONResponse(status_code=422, content={"detail": "กรุณาตรวจสอบชื่อ อีเมล และรายการหนังสือ จำนวนต้องเป็นจำนวนเต็ม 1–99 เล่มต่อรายการ"})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# Server-owned book catalog; browser prices are never used.
BOOK_CATALOG = {
    "guidance": {
        "title": "หนังสือจิตวิทยาและการแนะแนวสำหรับครู",
        "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
        "price": 300
    },
    "health": {
        "title": "หนังสือจิตวิทยาสุขภาพ",
        "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
        "price": 300
    },
    "counseling": {
        "title": "หนังสือทฤษฎีการให้คำปรึกษา",
        "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
        "price": 300
    },
    "learning": {
        "title": "หนังสือทฤษฎีทางจิตวิทยาที่ใช้จัดการเรียนการสอน",
        "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
        "price": 300
    },
    "skills": {
        "title": "หนังสือกระบวนการขั้นตอนเทคนิคและทักษะการให้คำปรึกษา",
        "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
        "price": 300
    }
}


class BookOrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    book_id: str = Field(min_length=1, max_length=40)
    quantity: int = Field(strict=True, ge=1, le=99)


class BookOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    buyer_name: str = Field(min_length=1, max_length=120)
    buyer_email: EmailStr
    buyer_phone: str | None = Field(default=None, max_length=30)
    items: list[BookOrderItem] = Field(min_length=1, max_length=5)
    payment_method: Literal["promptpay"] = "promptpay"
    # A random client-held credential: both retry key and order-access bearer token.
    idempotency_key: UUID4

    @field_validator("buyer_phone")
    @classmethod
    def valid_phone(cls, value: str | None) -> str | None:
        if not value:
            return None
        compact = re.sub(r"[ ()-]", "", value)
        if not re.fullmatch(r"(?:0[0-9]{8,9}|\+[1-9][0-9]{7,14})", compact):
            raise ValueError("กรุณาระบุเบอร์โทรศัพท์ให้ถูกต้อง")
        return compact


class BookOrderLine(BaseModel):
    book_id: str
    title: str
    quantity: int
    unit_price: int
    line_total: int


PaymentStatus = Literal["pending_payment", "awaiting_verification", "paid", "cancelled", "expired"]
ACTIVE_BOOK_STATUSES = {"pending_payment", "awaiting_verification"}


class BookOrderResponse(BaseModel):
    order_id: str
    items: list[BookOrderLine]
    total_quantity: int
    total_amount: int
    currency: Literal["THB"] = "THB"
    payment_method: Literal["promptpay"] = "promptpay"
    payment_status: PaymentStatus
    created_at: datetime
    expires_at: datetime
    paid_at: datetime | None = None
    payment_reference: str | None = None


class BookPaymentResponse(BaseModel):
    order: BookOrderResponse
    qr_image: str
    merchant_name: str


class OrderRepository(Protocol):
    """Implementations must make retry lookup + creation and updates atomic."""

    def create(self, record: dict) -> dict: ...
    def find_by_token(self, access_digest: str) -> dict | None: ...
    def update(self, order_id: str, transform: Callable[[dict], dict]) -> dict: ...


class FirestoreOrderRepository:
    def __init__(self, client: object, collection: str):
        self.client = client
        self.orders = client.collection(collection)
        self.requests = client.collection(collection + "_requests")

    def create(self, record: dict) -> dict:
        from google.cloud import firestore

        @firestore.transactional
        def create_once(transaction):
            key_ref = self.requests.document(record["access_digest"])
            key = key_ref.get(transaction=transaction)
            if key.exists:
                existing = self.orders.document(key.to_dict()["order_id"]).get(transaction=transaction).to_dict()
                if not existing or existing["request_digest"] != record["request_digest"]:
                    raise HTTPException(409, "ข้อมูลไม่ตรงกับคำขอเดิม กรุณาตรวจสอบคำสั่งซื้อเดิมก่อน")
                return existing
            transaction.create(self.orders.document(record["order_id"]), record)
            transaction.create(key_ref, {"order_id": record["order_id"]})
            return record

        return create_once(self.client.transaction())

    def find_by_token(self, access_digest: str) -> dict | None:
        key = self.requests.document(access_digest).get()
        if not key.exists:
            return None
        return self.orders.document(key.to_dict()["order_id"]).get().to_dict()

    def update(self, order_id: str, transform: Callable[[dict], dict]) -> dict:
        from google.cloud import firestore

        @firestore.transactional
        def update_once(transaction):
            ref = self.orders.document(order_id)
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                raise HTTPException(404, "ไม่พบคำสั่งซื้อ")
            original = snapshot.to_dict()
            updated = transform(dict(original))
            if updated != original:
                transaction.set(ref, updated)
            return updated

        return update_once(self.client.transaction())


@lru_cache(maxsize=1)
def get_book_repository() -> OrderRepository:
    # No local disk/in-memory fallback: Cloud Run instances are ephemeral.
    project = os.getenv("BOOK_ORDERS_PROJECT_ID", "").strip()
    collection = os.getenv("BOOK_ORDERS_COLLECTION", "book_orders").strip()
    if os.getenv("BOOK_ORDER_STORAGE", "").strip() != "firestore" or not project:
        raise HTTPException(503, "ระบบจัดเก็บคำสั่งซื้อยังไม่พร้อม กรุณาลองใหม่ภายหลัง")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", collection):
        raise HTTPException(503, "ระบบจัดเก็บคำสั่งซื้อยังไม่พร้อม")
    from google.cloud import firestore
    client = firestore.Client(project=project, database=os.getenv("BOOK_ORDERS_DATABASE", "(default)"))
    return FirestoreOrderRepository(client, collection)


def book_storage_call(action: Callable[[OrderRepository], dict]) -> dict:
    try:
        return action(get_book_repository())
    except HTTPException:
        raise
    except Exception:
        # Do not log request contents, credentials or SDK exception payloads.
        raise HTTPException(503, "ระบบจัดเก็บคำสั่งซื้อไม่พร้อมใช้งาน กรุณาลองใหม่ด้วยคำขอเดิม") from None


def book_now() -> datetime:
    return datetime.now(timezone.utc)


def digest_book_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authorize_book_record(record: dict, token: str) -> dict:
    if not secrets.compare_digest(record["access_digest"], digest_book_token(token)):
        raise HTTPException(404, "ไม่พบคำสั่งซื้อ")
    if record["payment_status"] in ACTIVE_BOOK_STATUSES and book_now() >= datetime.fromisoformat(record["expires_at"]):
        record = {**record, "payment_status": "expired"}
    return record


def book_access_token(request: Request) -> str:
    value = request.headers.get("Authorization", "")
    if not value.startswith("Bearer ") or len(value) != 43:
        raise HTTPException(401, "กรุณาเปิดคำสั่งซื้อจากหน้าชำระเงินเดิม")
    try:
        token = str(uuid.UUID(value[7:], version=None))
    except ValueError:
        raise HTTPException(401, "กรุณาเปิดคำสั่งซื้อจากหน้าชำระเงินเดิม") from None
    return token


def read_book_order(order_id: str, token: str) -> dict:
    if not re.fullmatch(r"BSJ-[0-9]{8}-[A-F0-9]{24}", order_id):
        raise HTTPException(404, "ไม่พบคำสั่งซื้อ")
    return book_storage_call(lambda repo: repo.update(order_id, lambda record: authorize_book_record(record, token)))


def public_book_order(record: dict) -> BookOrderResponse:
    # Explicit allowlist excludes buyer PII, token digests and merchant QR data.
    return BookOrderResponse(**{name: record[name] for name in BookOrderResponse.model_fields})


def build_promptpay_payload(identifier: str, amount_baht: int) -> str:
    """Thai QR credit transfer: tag 29, THB 764, exact amount, CRC16-CCITT.

    Phone and 13-digit tax/national proxies only. No invoice/reference claim is
    encoded: a direct credit-transfer QR is not an order-aware gateway session.
    """
    if type(amount_baht) is not int or amount_baht <= 0:
        raise ValueError("Invalid server order amount")
    if re.fullmatch(r"0[689][0-9]{8}", identifier):
        proxy_tag, proxy = "01", "0066" + identifier[1:]
    elif re.fullmatch(r"[0-9]{13}", identifier):
        proxy_tag, proxy = "02", identifier
    else:
        raise ValueError("Invalid PromptPay configuration")

    def tlv(tag, value):
        return tag + f"{len(value):02d}" + value

    merchant = tlv("00", "A000000677010111") + tlv(proxy_tag, proxy)
    content = (tlv("00", "01") + tlv("01", "12") + tlv("29", merchant)
               + tlv("58", "TH") + tlv("53", "764") + tlv("54", f"{amount_baht}.00") + "6304")
    return content + f"{binascii.crc_hqx(content.encode('ascii'), 0xFFFF):04X}"


def promptpay_qr_image(payload: str) -> str:
    import qrcode
    from qrcode.image.svg import SvgPathImage
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4, box_size=10)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    return "data:image/svg+xml;base64," + base64.b64encode(image.to_string()).decode("ascii")


class PaymentProvider(Protocol):
    def create_payment(self, order: dict) -> dict: ...
    def get_payment_status(self, reference: str) -> PaymentStatus: ...
    def verify_callback(self, payload: bytes, headers: dict) -> dict: ...
    def verify_payment(self, order_id: str, provider_reference: str) -> dict: ...


class DirectPromptPayProvider:
    def create_payment(self, order: dict) -> dict:
        identifier = os.getenv("PROMPTPAY_ID", "").strip()
        name = os.getenv("PROMPTPAY_MERCHANT_NAME", "").strip()
        if not name or name.startswith("YOUR_") or len(name) > 120:
            raise HTTPException(503, "ระบบพร้อมเพย์ยังไม่ได้ตั้งค่า กรุณาติดต่อผู้ขาย")
        try:
            payload = build_promptpay_payload(identifier, order["total_amount"])
        except ValueError:
            raise HTTPException(503, "ระบบพร้อมเพย์ยังไม่ได้ตั้งค่า กรุณาติดต่อผู้ขาย") from None
        return {"qr_payload": payload, "merchant_name": name}

    def get_payment_status(self, reference: str) -> PaymentStatus:
        raise HTTPException(503, "ยังไม่มีบริการตรวจสอบการรับเงินจากธนาคาร")

    def verify_callback(self, payload: bytes, headers: dict) -> dict:
        raise HTTPException(503, "ยังไม่มีบริการยืนยันการชำระเงิน")

    def verify_payment(self, order_id: str, provider_reference: str) -> dict:
        raise HTTPException(503, "ต้องตรวจสอบการรับเงินจริงก่อนยืนยันการชำระเงิน")


def get_book_payment_provider() -> PaymentProvider:
    return DirectPromptPayProvider()


@app.post("/api/book-orders", response_model=BookOrderResponse)
def create_book_order(order: BookOrderRequest, request: Request, response: Response) -> BookOrderResponse:
    response.headers["Cache-Control"] = "no-store"
    enforce_rate_limit(request, "book-orders", 20, 60)
    seen = set()
    items = []
    for item in order.items:
        if item.book_id not in BOOK_CATALOG or item.book_id in seen:
            raise HTTPException(422, "กรุณาเลือกหนังสือจากรายการ และไม่ส่งรายการหนังสือซ้ำ")
        seen.add(item.book_id)
        book = BOOK_CATALOG[item.book_id]
        items.append(BookOrderLine(book_id=item.book_id, title=book["title"], quantity=item.quantity,
                                   unit_price=book["price"], line_total=book["price"] * item.quantity).model_dump())
    canonical = order.model_dump(mode="json", exclude={"idempotency_key"})
    canonical["items"] = sorted(canonical["items"], key=lambda item: item["book_id"])
    now = book_now()
    record = {
        "order_id": f"BSJ-{now.astimezone(ZoneInfo('Asia/Bangkok')):%Y%m%d}-{secrets.token_hex(12).upper()}",
        "buyer_name": order.buyer_name, "buyer_email": str(order.buyer_email), "buyer_phone": order.buyer_phone,
        "items": items, "total_quantity": sum(item["quantity"] for item in items),
        "total_amount": sum(item["line_total"] for item in items), "currency": "THB",
        "payment_method": "promptpay", "payment_status": "pending_payment",
        "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "paid_at": None, "payment_reference": None,
        "access_digest": digest_book_token(str(order.idempotency_key)),
        "request_digest": hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }
    created = book_storage_call(lambda repo: repo.create(record))
    return public_book_order(read_book_order(created["order_id"], str(order.idempotency_key)))


@app.get("/api/book-orders/session/current", response_model=BookOrderResponse)
def recover_book_order(request: Request, response: Response) -> BookOrderResponse:
    response.headers["Cache-Control"] = "no-store"
    enforce_rate_limit(request, "book-order-status", 120, 60)
    token = book_access_token(request)
    record = book_storage_call(lambda repo: repo.find_by_token(digest_book_token(token)))
    if record is None:
        raise HTTPException(404, "ยังไม่พบคำสั่งซื้อจากคำขอนี้ สามารถลองส่งคำขอเดิมได้")
    return public_book_order(read_book_order(record["order_id"], token))


@app.get("/api/book-orders/{order_id}", response_model=BookOrderResponse)
def get_book_order(order_id: str, request: Request, response: Response) -> BookOrderResponse:
    response.headers["Cache-Control"] = "no-store"
    enforce_rate_limit(request, "book-order-status", 120, 60)
    return public_book_order(read_book_order(order_id, book_access_token(request)))


@app.post("/api/book-orders/{order_id}/payment", response_model=BookPaymentResponse)
def create_book_payment(order_id: str, request: Request, response: Response) -> BookPaymentResponse:
    response.headers["Cache-Control"] = "no-store"
    enforce_rate_limit(request, "book-payment", 20, 60)
    token = book_access_token(request)
    record = read_book_order(order_id, token)
    if record["payment_status"] not in ACTIVE_BOOK_STATUSES:
        raise HTTPException(409, "คำสั่งซื้อนี้หมดเวลาการชำระเงินแล้ว หรือไม่สามารถรับชำระได้")
    try:
        instruction = record.get("payment_instruction") or get_book_payment_provider().create_payment(record)
        # Ensure QR rendering works before persisting the instruction state.
        promptpay_qr_image(instruction["qr_payload"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "ไม่สามารถสร้าง QR ได้ กรุณาลองใหม่ภายหลัง") from None

    def issue(current):
        current = authorize_book_record(current, token)
        if current["payment_status"] not in ACTIVE_BOOK_STATUSES:
            return current
        current.setdefault("payment_instruction", instruction)
        current["payment_status"] = "awaiting_verification"
        return current

    updated = book_storage_call(lambda repo: repo.update(order_id, issue))
    if updated["payment_status"] not in ACTIVE_BOOK_STATUSES:
        raise HTTPException(409, "คำสั่งซื้อนี้หมดเวลาการชำระเงินแล้ว หรือไม่สามารถรับชำระได้")
    saved = updated["payment_instruction"]
    return BookPaymentResponse(order=public_book_order(updated), merchant_name=saved["merchant_name"],
                               qr_image=promptpay_qr_image(saved["qr_payload"]))


def google_settings() -> dict[str, str]:
    return {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.getenv(
            "GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback"
        ).strip(),
        "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", "primary").strip(),
        "token_file": os.getenv("GOOGLE_TOKEN_FILE", "google_token.json").strip(),
        "token_storage": os.getenv("GOOGLE_TOKEN_STORAGE", "file").strip().lower(),
        "token_secret_name": os.getenv("GOOGLE_TOKEN_SECRET_NAME", "speak-and-grow-google-token").strip(),
        "gcp_project_id": os.getenv("GCP_PROJECT_ID", "").strip(),
    }


def google_is_configured() -> bool:
    settings = google_settings()
    return bool(settings["client_id"] and settings["client_secret"] and settings["redirect_uri"])


def google_client_config() -> dict:
    settings = google_settings()
    return {
        "web": {
            "client_id": settings["client_id"],
            "client_secret": settings["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings["redirect_uri"]],
        }
    }


def create_google_oauth_flow(
    state: str | None = None,
    code_verifier: str | None = None,
    autogenerate_code_verifier: bool = False,
) -> object:
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        google_client_config(),
        scopes=GOOGLE_SCOPES,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=autogenerate_code_verifier,
    )
    flow.redirect_uri = google_settings()["redirect_uri"]
    return flow


def google_token_path() -> Path:
    configured_path = Path(google_settings()["token_file"])
    return configured_path if configured_path.is_absolute() else BASE_DIR / configured_path


def save_google_token_to_file(token_json: str) -> None:
    token_path = google_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token_json, encoding="utf-8")


def load_google_token_from_file() -> str | None:
    token_path = google_token_path()
    if not token_path.exists():
        return None
    try:
        return token_path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Stored Google credential file could not be loaded")
        return None


def google_secret_resource_name() -> str:
    settings = google_settings()
    if not settings["gcp_project_id"]:
        raise RuntimeError("GCP_PROJECT_ID is required for Secret Manager token storage.")
    return f"projects/{settings['gcp_project_id']}/secrets/{settings['token_secret_name']}"


def load_google_token_from_secret_manager() -> str | None:
    from google.api_core.exceptions import NotFound
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    try:
        response = client.access_secret_version(request={"name": f"{google_secret_resource_name()}/versions/latest"})
    except NotFound:
        return None
    except Exception:
        logger.exception("Google credentials could not be loaded from Secret Manager")
        return None
    return response.payload.data.decode("utf-8")


def save_google_token_to_secret_manager(token_json: str) -> None:
    from google.api_core.exceptions import NotFound
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    parent = google_secret_resource_name()
    try:
        client.get_secret(request={"name": parent})
    except NotFound:
        settings = google_settings()
        client.create_secret(request={"parent": f"projects/{settings['gcp_project_id']}", "secret_id": settings["token_secret_name"], "secret": {"replication": {"automatic": {}}}})
    client.add_secret_version(request={"parent": parent, "payload": {"data": token_json.encode("utf-8")}})


def save_google_credentials(credentials: object) -> None:
    token_json = credentials.to_json()
    if google_settings()["token_storage"] == "secret_manager":
        save_google_token_to_secret_manager(token_json)
    else:
        save_google_token_to_file(token_json)


def load_google_credentials() -> object | None:
    try:
        token_json = load_google_token_from_secret_manager() if google_settings()["token_storage"] == "secret_manager" else load_google_token_from_file()
        if not token_json:
            return None
        from google.oauth2.credentials import Credentials
        return Credentials.from_authorized_user_info(json.loads(token_json), GOOGLE_SCOPES)
    except Exception:
        logger.exception("Stored Google credentials could not be loaded")
        return None

def get_google_calendar_service() -> object:
    credentials = load_google_credentials()
    if credentials is None:
        raise RuntimeError("Google Calendar is not authorized.")
    try:
        if credentials.expired and credentials.refresh_token:  # type: ignore[attr-defined]
            from google.auth.transport.requests import Request as GoogleRequest

            credentials.refresh(GoogleRequest())  # type: ignore[attr-defined]
            save_google_credentials(credentials)
        if not credentials.valid:  # type: ignore[attr-defined]
            raise RuntimeError("Google Calendar authorization has expired.")
        from googleapiclient.discovery import build

        return build("calendar", "v3", credentials=credentials, cache_discovery=False)
    except RuntimeError:
        raise
    except Exception as exc:
        logger.exception("Google Calendar service initialization failed")
        raise RuntimeError("Google Calendar is unavailable.") from exc


def normalize_appointment_start(value: datetime, timezone_name: str) -> tuple[datetime, ZoneInfo]:
    if timezone_name != "Asia/Bangkok":
        raise HTTPException(status_code=400, detail="กรุณาใช้เขตเวลา Asia/Bangkok")
    try:
        appointment_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=400, detail="The selected time zone is invalid.") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=400, detail="Appointment time must include a time zone.")
    start_time = value.astimezone(appointment_timezone)
    now = datetime.now(timezone.utc)
    if start_time.astimezone(timezone.utc) <= now:
        raise HTTPException(status_code=400, detail="The appointment must be in the future.")
    if start_time.astimezone(timezone.utc) > now + timedelta(days=180):
        raise HTTPException(status_code=400, detail="Appointments may be booked up to 180 days ahead.")
    allowed = {(19, 10)} if start_time.weekday() < 5 else {(10, 0), (19, 30)}
    if (start_time.hour, start_time.minute) not in allowed or start_time.second or start_time.microsecond:
        raise HTTPException(status_code=400, detail="เวลานัดหมายที่อนุญาต: จันทร์–ศุกร์ 19:10–20:00 เสาร์–อาทิตย์ 10:00–10:50 หรือ 19:30–20:20 (Asia/Bangkok)")
    return start_time, appointment_timezone


def sanitize_calendar_description() -> str:
    return "Online speech consultation booked through Speak & Grow."


def build_calendar_event(booking: ConsultationRequest, start_time: datetime, end_time: datetime) -> dict:
    return {
        "summary": "Speech consultation",
        "description": sanitize_calendar_description(),
        "start": {"dateTime": start_time.isoformat(), "timeZone": booking.timezone},
        "end": {"dateTime": end_time.isoformat(), "timeZone": booking.timezone},
        "attendees": [{
            "email": str(booking.legal_guardian_email if booking.under_20 else booking.guardian_email),
            "displayName": booking.legal_guardian_name if booking.under_20 else booking.guardian_name,
        }],
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 1440},
                {"method": "popup", "minutes": 30},
            ],
        },
    }


def extract_meet_url(event: dict) -> str:
    if event.get("hangoutLink"):
        return str(event["hangoutLink"])
    for entry_point in event.get("conferenceData", {}).get("entryPoints", []):
        if entry_point.get("entryPointType") == "video" and entry_point.get("uri"):
            return str(entry_point["uri"])
    return ""


def check_calendar_conflict(service: object, start_time: datetime, end_time: datetime) -> bool:
    settings = google_settings()
    result = service.freebusy().query(body={  # type: ignore[attr-defined]
        "timeMin": start_time.astimezone(timezone.utc).isoformat(),
        "timeMax": end_time.astimezone(timezone.utc).isoformat(),
        "items": [{"id": settings["calendar_id"]}],
    }).execute()
    return bool(result.get("calendars", {}).get(settings["calendar_id"], {}).get("busy", []))


def calculate_join_allowed(start_time: datetime, end_time: datetime, now: datetime | None = None) -> bool:
    current_time = now or datetime.now(timezone.utc)
    return start_time - timedelta(minutes=10) <= current_time <= end_time


@app.get("/")
async def home() -> dict[str, str]:
    return {"service": "Speak and Grow API", "status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "model": MODEL,
        "api_key_configured": str(bool(os.getenv("MISTRAL_API_KEY"))).lower(),
    }


@app.get("/auth/google/status")
async def google_auth_status() -> dict[str, bool]:
    credentials = load_google_credentials() if google_is_configured() else None
    return {
        "configured": google_is_configured(),
        "authorized": bool(credentials and getattr(credentials, "valid", False)),
    }


@app.get("/auth/google/login")
async def google_auth_login(request: Request) -> RedirectResponse:
    enforce_rate_limit(request, "google-login", 20, 60)
    if not google_is_configured():
        raise HTTPException(status_code=503, detail="Google Calendar is not configured.")
    settings = google_settings()
    if not settings["redirect_uri"].startswith("https://") and not settings["redirect_uri"].startswith(
        ("http://localhost", "http://127.0.0.1")
    ):
        raise HTTPException(status_code=400, detail="Google OAuth requires HTTPS outside local development.")
    flow = create_google_oauth_flow(autogenerate_code_verifier=True)
    authorization_url, oauth_state = flow.authorization_url(  # type: ignore[attr-defined]
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    code_verifier = flow.code_verifier  # type: ignore[attr-defined]
    if not code_verifier:
        raise HTTPException(status_code=502, detail="Google authorization could not be started.")
    request.session["google_oauth_state"] = oauth_state
    request.session["google_code_verifier"] = code_verifier
    return RedirectResponse(authorization_url)


@app.get("/auth/google/callback")
async def google_auth_callback(request: Request, state: str = "", code: str = "") -> RedirectResponse:
    expected_state = request.session.pop("google_oauth_state", None)
    code_verifier = request.session.pop("google_code_verifier", None)
    if not expected_state or not state:
        raise HTTPException(status_code=400, detail="Google authorization state is missing.")
    if not isinstance(expected_state, str) or not secrets.compare_digest(expected_state, state):
        raise HTTPException(status_code=400, detail="Google authorization state does not match.")
    if not code_verifier or not isinstance(code_verifier, str):
        raise HTTPException(status_code=400, detail="Google authorization code verifier is missing.")
    if not code:
        raise HTTPException(status_code=400, detail="Google did not return an authorization code.")
    flow = create_google_oauth_flow(
        state=expected_state,
        code_verifier=code_verifier,
    )
    try:
        flow.fetch_token(code=code)  # type: ignore[attr-defined]
        save_google_credentials(flow.credentials)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.error("Google OAuth token exchange failed")
        raise HTTPException(status_code=502, detail="Google Calendar authorization failed.") from exc
    frontend_url = os.getenv("FRONTEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    return RedirectResponse(f"{frontend_url}/?google_calendar=connected")


@app.post("/api/consultations", response_model=ConsultationResponse, status_code=201)
def create_consultation(booking: ConsultationRequest, request: Request = None) -> ConsultationResponse:
    if request is not None:
        enforce_rate_limit(request, "consultation", 10, 60)
    if booking.under_20:
        if not (booking.legal_guardian_name and booking.legal_guardian_name.strip()
                and booking.legal_guardian_email
                and booking.guardian_relationship and booking.guardian_relationship.strip()
                and booking.guardian_consent_confirmed):
            raise HTTPException(status_code=400, detail="กรุณากรอกชื่อ อีเมล ความสัมพันธ์ และยืนยันความยินยอมของผู้ปกครองตามกฎหมาย")
    elif not booking.consent_confirmed:
        raise HTTPException(status_code=400, detail="กรุณายืนยันว่ามีอายุ 20 ปีบริบูรณ์ขึ้นไปและยินยอมเข้ารับการปรึกษาด้วยตนเอง")
    start_time, _ = normalize_appointment_start(booking.appointment_start, booking.timezone)
    end_time = start_time + timedelta(minutes=booking.duration_minutes)
    try:
        service = get_google_calendar_service()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Google Calendar is not authorized.") from exc
    try:
        if check_calendar_conflict(service, start_time, end_time):
            raise HTTPException(
                status_code=409,
                detail="The selected time is no longer available. Please choose another time.",
            )
        created_event = service.events().insert(
            calendarId=google_settings()["calendar_id"],
            body=build_calendar_event(booking, start_time, end_time),
            conferenceDataVersion=1,
            sendUpdates="all",
        ).execute()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Google Calendar appointment creation failed")
        raise HTTPException(
            status_code=502,
            detail="The appointment could not be created. Please try again later.",
        ) from exc
    meet_url = extract_meet_url(created_event)
    response_status = "confirmed" if meet_url else "conference_pending"
    return ConsultationResponse(
        event_id=str(created_event.get("id", "")),
        html_link=str(created_event.get("htmlLink", "")),
        meet_url=meet_url,
        start_time=start_time,
        end_time=end_time,
        timezone=booking.timezone,
        status=response_status,
        message=(
            "Appointment created. Google Calendar has sent the invitation."
            if meet_url
            else "Appointment created. The video meeting link is still being prepared."
        ),
    )


@app.get("/api/consultations/{event_id}")
def consultation_status(event_id: str) -> dict:
    if not event_id or len(event_id) > 256 or not all(ch.isalnum() or ch in "_-" for ch in event_id):
        raise HTTPException(status_code=400, detail="Invalid appointment identifier.")
    try:
        service = get_google_calendar_service()
        event = service.events().get(  # type: ignore[attr-defined]
            calendarId=google_settings()["calendar_id"], eventId=event_id
        ).execute()
        start_time = datetime.fromisoformat(event["start"]["dateTime"].replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(event["end"]["dateTime"].replace("Z", "+00:00"))
        join_allowed = calculate_join_allowed(start_time, end_time)
        meet_url = extract_meet_url(event) if join_allowed else ""
        return {
            "event_id": event_id,
            "start_time": start_time,
            "end_time": end_time,
            "status": event.get("status", "confirmed"),
            "meet_url": meet_url,
            "join_allowed": join_allowed,
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Google Calendar is not authorized.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Google Calendar appointment status lookup failed")
        raise HTTPException(status_code=502, detail="Appointment status is temporarily unavailable.") from exc


@app.delete("/api/consultations/{event_id}", status_code=403)
async def cancel_consultation(event_id: str) -> None:
    # TODO: Enable only after an authenticated administrator mechanism is added.
    raise HTTPException(status_code=403, detail="Online cancellation is not enabled.")


@app.websocket("/ws/pronunciation")
async def pronunciation_socket(websocket: WebSocket) -> None:
    origin = (websocket.headers.get("origin") or "").rstrip("/")
    if origin not in configured_frontend_origins():
        await websocket.close(code=1008)
        return
    client = websocket.client.host if websocket.client else "unknown"
    if not rate_limiter.check(f"websocket:{client}", 20, 60):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        await websocket.send_json({
            "type": "error",
            "message": "MISTRAL_API_KEY is not configured on the server.",
        })
        await websocket.close(code=1011)
        return

    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=100)
    transcript_parts: list[str] = []
    stop_event = asyncio.Event()

    async def audio_stream() -> AsyncIterator[bytes]:
        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                break
            yield chunk

    async def receive_browser_audio() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message.get("bytes") is not None:
                    try:
                        audio_queue.put_nowait(message["bytes"])
                    except asyncio.QueueFull:
                        # Drop the oldest queued chunk to keep latency bounded.
                        _ = audio_queue.get_nowait()
                        audio_queue.put_nowait(message["bytes"])
                elif message.get("text"):
                    command = json.loads(message["text"])
                    if command.get("type") == "stop":
                        stop_event.set()
                        await audio_queue.put(None)
                        return
        except WebSocketDisconnect:
            stop_event.set()
            await audio_queue.put(None)

    async def run_voxtral() -> None:
        client = Mistral(api_key=api_key)
        audio_format = AudioFormat(encoding="pcm_s16le", sample_rate=16000)
        try:
            async for event in client.audio.realtime.transcribe_stream(
                audio_stream=audio_stream(),
                model=MODEL,
                audio_format=audio_format,
                target_streaming_delay_ms=480,
            ):
                if isinstance(event, RealtimeTranscriptionSessionCreated):
                    await websocket.send_json({"type": "session", "status": "created"})
                elif isinstance(event, TranscriptionStreamTextDelta):
                    transcript_parts.append(event.text)
                    await websocket.send_json({"type": "delta", "text": event.text})
                elif isinstance(event, TranscriptionStreamDone):
                    break
                elif isinstance(event, RealtimeTranscriptionError):
                    await websocket.send_json({
                        "type": "error",
                        "message": str(event),
                    })
                    return
                elif isinstance(event, UnknownRealtimeEvent):
                    continue

            await websocket.send_json({
                "type": "done",
                "transcript": "".join(transcript_parts).strip(),
            })
        except Exception as exc:
            try:
                await websocket.send_json({"type": "error", "message": str(exc)})
            except Exception:
                pass

    receiver = asyncio.create_task(receive_browser_audio())
    transcriber = asyncio.create_task(run_voxtral())

    try:
        done, pending = await asyncio.wait(
            {receiver, transcriber},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if receiver in done and not transcriber.done():
            try:
                await asyncio.wait_for(transcriber, timeout=12)
            except asyncio.TimeoutError:
                transcriber.cancel()

        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(receiver, transcriber, return_exceptions=True)
    finally:
        if websocket.client_state.name == "CONNECTED":
            await websocket.close()






