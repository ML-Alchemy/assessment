"""Relay admin app.

Where customers connect their CRM to Relay. Lists customers, starts the
OAuth flow with the CRM, and stores the tokens it receives.
"""

import html
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import psycopg
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

DATABASE_URL = os.environ["DATABASE_URL"]
CRM_URL = os.environ["MOCK_CRM_URL"]
CRM_PUBLIC_URL = os.environ.get("MOCK_CRM_PUBLIC_URL", CRM_URL)
CRM_CLIENT_ID = os.environ["MOCK_CRM_CLIENT_ID"]
CRM_CLIENT_SECRET = os.environ["MOCK_CRM_CLIENT_SECRET"]
ADMIN_PUBLIC_URL = os.environ.get("ADMIN_PUBLIC_URL", "http://localhost:8001")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
ADMIN_STATE_KEY = os.environ["ADMIN_STATE_KEY"]
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
TOKEN_CIPHER = Fernet(os.environ["CRM_TOKEN_KEY"])

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
log = logging.getLogger("admin-app")

app = FastAPI(title="Relay admin")
basic = HTTPBasic()


def require_admin(creds: HTTPBasicCredentials = Depends(basic)) -> str:
    if creds.username != ADMIN_USERNAME or creds.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return creds.username


def encrypt(value: str) -> str:
    return TOKEN_CIPHER.encrypt(value.encode()).decode()


def db():
    return psycopg.connect(DATABASE_URL, autocommit=True)


@app.get("/", response_class=HTMLResponse)
def index(_: str = Depends(require_admin)):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, allowed_llm_providers, crm_connected_at FROM customers ORDER BY name"
        ).fetchall()
    items = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{html.escape(', '.join(providers))}</td>"
        f"<td>{'Connected ' + connected.strftime('%Y-%m-%d %H:%M') if connected else 'Not connected'}</td>"
        f"<td><a href='/connect/{html.escape(cid)}'>Connect CRM</a></td></tr>"
        for cid, name, providers, connected in rows
    )
    return f"""<!doctype html><html><head><title>Relay admin</title></head><body>
<h1>Relay customers</h1>
<table border="1" cellpadding="6"><tr><th>Customer</th><th>Allowed LLM providers</th><th>CRM</th><th></th></tr>{items}</table>
</body></html>"""


@app.get("/connect/{customer_id}")
def connect(customer_id: str, _: str = Depends(require_admin)):
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Unknown customer")
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(ADMIN_STATE_KEY.encode(), f"{customer_id}:{nonce}".encode(), hashlib.sha256).hexdigest()
    state = f"{customer_id}:{nonce}:{signature}"
    params = {
        "client_id": CRM_CLIENT_ID,
        "redirect_uri": f"{ADMIN_PUBLIC_URL}/callback",
        "account": customer_id,
        "state": state,
    }
    return RedirectResponse(f"{CRM_PUBLIC_URL}/oauth/authorize?{urlencode(params)}", status_code=302)


@app.get("/callback")
def callback(code: str, state: str):
    parts = state.split(":")
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    customer_id, nonce, supplied_signature = parts
    expected_signature = hmac.new(ADMIN_STATE_KEY.encode(), f"{customer_id}:{nonce}".encode(), hashlib.sha256).hexdigest()
    if len(nonce) < 32 or not hmac.compare_digest(supplied_signature, expected_signature):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=400, detail="Invalid OAuth customer")
    r = httpx.post(
        f"{CRM_URL}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CRM_CLIENT_ID,
            "client_secret": CRM_CLIENT_SECRET,
            "redirect_uri": f"{ADMIN_PUBLIC_URL}/callback",
        },
        timeout=10,
    )
    if r.status_code != 200:
        log.warning("CRM OAuth token exchange failed for customer %s (status %d)", customer_id, r.status_code)
        raise HTTPException(status_code=400, detail="CRM connection failed")
    tok = r.json()
    expires = datetime.now(timezone.utc) + timedelta(seconds=tok["expires_in"])
    with db() as conn:
        conn.execute(
            """UPDATE customers
               SET crm_access_token = %s, crm_refresh_token = %s, crm_token_expires_at = %s, crm_connected_at = now()
               WHERE id = %s""",
            (encrypt(tok["access_token"]), encrypt(tok["refresh_token"]), expires, customer_id),
        )
    log.info("Connected CRM for %s", customer_id, extra={"customer_id": customer_id})
    return RedirectResponse("/", status_code=302)


@app.get("/health")
def health():
    return {"status": "ok"}
