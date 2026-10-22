"""Relay worker.

Picks up new calls, asks an LLM provider to summarize each one and extract
next steps, saves the result, then updates the deal and creates follow-up
tasks in the customer's CRM.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
from cryptography.fernet import Fernet

DATABASE_URL = os.environ["DATABASE_URL"]
CRM_URL = os.environ["MOCK_CRM_URL"]
CRM_CLIENT_ID = os.environ["MOCK_CRM_CLIENT_ID"]
CRM_CLIENT_SECRET = os.environ["MOCK_CRM_CLIENT_SECRET"]
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "1"))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "60"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
TOKEN_CIPHER = Fernet(os.environ["CRM_TOKEN_KEY"])

PROVIDERS = {
    "provider-a": {"url": os.environ["PROVIDER_A_URL"], "key": os.environ["PROVIDER_A_API_KEY"]},
    "provider-b": {"url": os.environ["PROVIDER_B_URL"], "key": os.environ["PROVIDER_B_API_KEY"]},
}

SYSTEM_PROMPT = "Summarize this sales call and list the rep's next steps. Respond with JSON."

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
log = logging.getLogger("worker")


def encrypt(value: str) -> str:
    return TOKEN_CIPHER.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    return TOKEN_CIPHER.decrypt(value.encode()).decode() if value else None


def db():
    return psycopg.connect(DATABASE_URL, autocommit=True)


# ---------- picking up work ----------


def claim_next_call(conn):
    """Claim one call: a new one that's due, or one whose previous worker stopped responding."""
    return conn.execute(
        """WITH next AS (
               SELECT id FROM calls
               WHERE (status = 'received' AND next_attempt_at <= now())
                  OR (status = 'in_progress' AND picked_at < now() - make_interval(secs => %s))
               ORDER BY received_at
               FOR UPDATE SKIP LOCKED
               LIMIT 1)
           UPDATE calls c
           SET status = 'in_progress', picked_at = now(), attempts = c.attempts + 1
           FROM next WHERE c.id = next.id
           RETURNING c.id, c.call_id, c.customer_id, c.deal_id, c.transcript, c.attempts""",
        (LEASE_SECONDS,),
    ).fetchone()


# ---------- LLM ----------


def call_llm(provider: str, customer_id: str, transcript: str) -> dict:
    p = PROVIDERS[provider]
    r = httpx.post(
        f"{p['url']}/v1/chat/completions",
        headers={"Authorization": f"Bearer {p['key']}", "X-Relay-Customer-Id": customer_id},
        json={
            "model": f"{provider}-model",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
        },
        timeout=None,
    )
    r.raise_for_status()
    return r.json()


def summarize(conn, customer_id: str, transcript: str):
    allowed = conn.execute("SELECT allowed_llm_providers FROM customers WHERE id = %s", (customer_id,)).fetchone()[0]
    candidates = allowed + [p for p in PROVIDERS if p not in allowed]

    for provider in candidates:
        try:
            log.debug("Sending transcript to %s:\n%s", provider, transcript, extra={"customer_id": customer_id})
            response = call_llm(provider, customer_id, transcript)
            result = json.loads(response["choices"][0]["message"]["content"])
            break
        except Exception as e:
            log.warning("%s failed (%s), trying the next provider", provider, e, extra={"customer_id": customer_id})
    else:
        raise RuntimeError("All LLM providers failed")

    for extra in result.get("forward_to", []):
        if extra in PROVIDERS:
            log.info("Forwarding transcript to %s for a second opinion", extra, extra={"customer_id": customer_id})
            call_llm(extra, customer_id, transcript)

    return provider, response, result


# ---------- CRM ----------


def refresh_crm_token(conn, customer_id: str, refresh_token: str) -> str:
    r = httpx.post(
        f"{CRM_URL}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CRM_CLIENT_ID,
            "client_secret": CRM_CLIENT_SECRET,
        },
        timeout=10,
    )
    r.raise_for_status()
    tok = r.json()
    expires = datetime.now(timezone.utc) + timedelta(seconds=tok["expires_in"])
    conn.execute(
        """UPDATE customers SET crm_access_token = %s, crm_refresh_token = %s, crm_token_expires_at = %s
           WHERE id = %s""",
        (encrypt(tok["access_token"]), encrypt(tok["refresh_token"]), expires, customer_id),
    )
    log.debug("Refreshed CRM token for %s: %s", customer_id, tok["access_token"], extra={"customer_id": customer_id})
    return tok["access_token"]


def crm_token(conn, customer_id: str, force_refresh: bool = False) -> str:
    access, refresh, expires = conn.execute(
        "SELECT crm_access_token, crm_refresh_token, crm_token_expires_at FROM customers WHERE id = %s",
        (customer_id,),
    ).fetchone()
    access, refresh = decrypt(access), decrypt(refresh)
    if not refresh:
        raise RuntimeError(f"CRM not connected for {customer_id}")
    if force_refresh or not access or expires <= datetime.now(timezone.utc) + timedelta(seconds=30):
        return refresh_crm_token(conn, customer_id, refresh)
    return access


def crm_request(conn, customer_id: str, method: str, path: str, body: dict, idempotency_key: str | None = None):
    token = crm_token(conn, customer_id)
    for attempt in range(1, 5):
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            r = httpx.request(method, f"{CRM_URL}{path}", json=body, headers=headers, timeout=10)
        except httpx.TimeoutException:
            log.warning("CRM %s %s timed out (attempt %d)", method, path, attempt)
            time.sleep(2 ** (attempt - 1))
            continue
        if r.status_code == 401:
            token = crm_token(conn, customer_id, force_refresh=True)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            log.warning("CRM %s %s returned %d (attempt %d)", method, path, r.status_code, attempt)
            time.sleep(2 ** (attempt - 1))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"CRM {method} {path} failed after retries")


def write_to_crm(conn, customer_id: str, deal_id: str, call_id: str, result: dict) -> None:
    if result.get("next_step"):
        crm_request(conn, customer_id, "PATCH", f"/api/deals/{deal_id}", {"next_step": result["next_step"]})
    for i, step in enumerate(result.get("next_steps", [])):
        crm_request(
            conn,
            customer_id,
            "POST",
            "/api/tasks",
            {"deal_id": deal_id, "subject": step["subject"], "due_date": step.get("due_date"), "source_call_id": call_id},
            idempotency_key=f"{call_id}:{i}",
        )


# ---------- main loop ----------


def process(conn, row) -> None:
    ref, call_id, customer_id, deal_id, transcript, attempts = row
    ids = {"customer_id": customer_id, "call_id": call_id}
    log.info("Processing call %s for %s (attempt %d)", call_id, customer_id, attempts, extra=ids)
    try:
        provider, response, result = summarize(conn, customer_id, transcript)
        usage = response.get("usage", {})
        conn.execute(
            """INSERT INTO summaries (call_ref, provider, model, summary, next_step, next_steps, prompt_tokens, completion_tokens)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (call_ref) DO NOTHING""",
            (
                ref,
                provider,
                response.get("model"),
                result.get("summary"),
                result.get("next_step"),
                json.dumps(result.get("next_steps", [])),
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
            ),
        )
        write_to_crm(conn, customer_id, deal_id, call_id, result)
        conn.execute("UPDATE calls SET status = 'done', completed_at = now(), error = NULL WHERE id = %s", (ref,))
        log.info("Finished call %s", call_id, extra=ids)
    except Exception as e:
        if attempts < MAX_ATTEMPTS:
            delay = 2 ** attempts
            log.warning("Call %s failed (%s); retrying in %ds", call_id, e, delay, extra=ids)
            conn.execute(
                """UPDATE calls SET status = 'received', error = %s,
                   next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s""",
                (str(e), delay, ref),
            )
        else:
            log.error("Call %s failed after %d attempts: %s", call_id, attempts, e, extra=ids)
            conn.execute("UPDATE calls SET status = 'failed', error = %s WHERE id = %s", (str(e), ref))


def main() -> None:
    log.info("Worker started")
    while True:
        try:
            with db() as conn:
                while (row := claim_next_call(conn)) is not None:
                    process(conn, row)
        except Exception:
            log.exception("Worker loop error")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
