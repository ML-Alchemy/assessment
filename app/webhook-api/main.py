"""Relay webhook API.

Receives call transcripts from customers' meeting tools, checks the
signature, and stores each call for the worker to process.
"""

import hashlib
import hmac
import json
import logging
import os

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

DATABASE_URL = os.environ["DATABASE_URL"]
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

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
    expected = hmac.new(secret.encode(), f"{parts.get('t', '')}.".encode() + body, hashlib.sha256).hexdigest()
    return parts.get("v1") == expected


@app.post("/webhooks/{customer_id}")
async def receive_call(customer_id: str, request: Request):
    secret = webhook_secret(customer_id)
    if not secret:
        raise HTTPException(status_code=404, detail="Unknown customer")

    body = await request.body()
    if not signature_valid(secret, request.headers.get("X-Relay-Signature", ""), body):
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(body)
    log.debug("Webhook payload for %s: %s", customer_id, data, extra={"customer_id": customer_id, "call_id": data.get("call_id")})

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        inserted = conn.execute(
            """INSERT INTO calls (call_id, customer_id, deal_id, transcript_id, transcript)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (call_id) DO NOTHING""",
            (data["call_id"], customer_id, data["deal_id"], data.get("transcript_id"), data["transcript"]),
        ).rowcount

    if inserted:
        log.info("Received call %s for %s", data["call_id"], customer_id, extra={"customer_id": customer_id, "call_id": data["call_id"]})
    else:
        log.info("Ignored duplicate delivery of call %s for %s", data["call_id"], customer_id, extra={"customer_id": customer_id, "call_id": data["call_id"]})
    return JSONResponse({"received": True}, status_code=202)


@app.get("/health")
def health():
    return {"status": "ok"}
