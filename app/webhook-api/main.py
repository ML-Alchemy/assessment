"""Relay webhook API.

Receives call transcripts from customers' meeting tools, checks the
signature, and stores each call for the worker to process.
"""

import hashlib
import hmac
import json
import time
import logging
import os

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

DATABASE_URL = os.environ["DATABASE_URL"]
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
MAX_PENDING_PER_CUSTOMER = int(os.environ.get("MAX_PENDING_PER_CUSTOMER", "100"))

LOG_FORMAT = os.environ.get("LOG_FORMAT", "text").lower()


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with customer and call IDs as separate fields when known."""

    def format(self, record):
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": record.name,
            "message": record.getMessage(),
        }
        for field in ("customer_id", "call_id"):
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry)


_handler = logging.StreamHandler()
_handler.setFormatter(
    JsonFormatter() if LOG_FORMAT == "json" else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
)
logging.basicConfig(level=LOG_LEVEL, handlers=[_handler])
log = logging.getLogger("webhook-api")

app = FastAPI(title="Relay webhook API")


def webhook_secret(customer_id: str) -> str | None:
    return os.environ.get(f"WEBHOOK_SECRET_{customer_id.upper()}")


def signature_valid(secret: str, header: str, body: bytes) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        timestamp = int(parts.get("t", "0"))
    except (TypeError, ValueError):
        return False
    # Reject replayed signed requests outside a five-minute window.
    if abs(int(time.time()) - timestamp) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    supplied = parts.get("v1", "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


@app.post("/webhooks/{customer_id}")
async def receive_call(customer_id: str, request: Request):
    secret = webhook_secret(customer_id)
    if not secret:
        raise HTTPException(status_code=404, detail="Unknown customer")

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Webhook payload too large")
    body = await request.body()
    if len(body) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Webhook payload too large")
    if not signature_valid(secret, request.headers.get("X-Relay-Signature", ""), body):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = json.loads(body)
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in ("call_id", "deal_id", "transcript")):
            raise ValueError("missing or invalid required fields")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from exc
    # Never log raw transcripts: they contain confidential customer data and may contain prompt injection.

    with psycopg.connect(DATABASE_URL) as conn:
        # Serialize admission for this tenant so simultaneous bursts cannot race past the cap.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (customer_id,))
        existing = conn.execute("SELECT customer_id FROM calls WHERE call_id = %s", (data["call_id"],)).fetchone()
        if existing:
            if existing[0] != customer_id:
                raise HTTPException(status_code=409, detail="Call ID already belongs to another customer")
            inserted = 0
        else:
            pending = conn.execute(
                "SELECT count(*) FROM calls WHERE customer_id = %s AND status IN ('received', 'in_progress')",
                (customer_id,),
            ).fetchone()[0]
            if pending >= MAX_PENDING_PER_CUSTOMER:
                raise HTTPException(status_code=429, detail="Customer queue is full; retry later", headers={"Retry-After": "30"})
            inserted = conn.execute(
                """INSERT INTO calls (call_id, customer_id, deal_id, transcript_id, transcript)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (call_id) DO NOTHING""",
                (data["call_id"], customer_id, data["deal_id"], data.get("transcript_id"), data["transcript"]),
            ).rowcount
            if not inserted:
                existing = conn.execute("SELECT customer_id FROM calls WHERE call_id = %s", (data["call_id"],)).fetchone()
                if not existing or existing[0] != customer_id:
                    raise HTTPException(status_code=409, detail="Call ID already belongs to another customer")

    if inserted:
        log.info("Received call %s for %s", data["call_id"], customer_id, extra={"customer_id": customer_id, "call_id": data["call_id"]})
    else:
        log.info("Ignored duplicate delivery of call %s for %s", data["call_id"], customer_id, extra={"customer_id": customer_id, "call_id": data["call_id"]})
    return JSONResponse({"received": True}, status_code=202)


@app.get("/health")
def health():
    return {"status": "ok"}
