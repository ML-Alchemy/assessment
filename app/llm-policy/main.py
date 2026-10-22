"""Provider-specific LLM policy gateway. Only this service can reach mock LLMs."""
import hmac
import json
import logging
import os
from pathlib import Path
import yaml

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

DATABASE_URL = os.environ["DATABASE_URL"]
GATEWAY_KEY = os.environ["LLM_GATEWAY_KEY"]
PROVIDER = os.environ["POLICY_PROVIDER"]
POLICY_FILE = Path(os.environ.get("POLICY_FILE", "/fixtures/customers.yaml"))
with POLICY_FILE.open() as stream:
    POLICY = {item["id"]: set(item["allowed_llm_providers"]) for item in yaml.safe_load(stream)["customers"]}
UPSTREAM_URL = os.environ["UPSTREAM_URL"].rstrip("/")
UPSTREAM_KEY = os.environ["UPSTREAM_API_KEY"]
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format='%(message)s')
log = logging.getLogger("llm-policy-gateway")
app = FastAPI(title=f"Relay policy gateway ({PROVIDER})")

@app.get("/health")
def health():
    return {"status": "ok", "provider": PROVIDER}

@app.post("/v1/chat/completions")
async def completions(request: Request):
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {GATEWAY_KEY}"):
        raise HTTPException(status_code=401, detail="Unauthorized gateway client")
    customer_id = request.headers.get("X-Relay-Customer-Id", "")
    call_id = request.headers.get("X-Relay-Call-ID", "")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    transcript = next((m.get("content") for m in payload.get("messages", []) if m.get("role") == "user"), None)
    # The DB row binds the request to a stored call; the read-only policy file defines provider permissions.
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT customer_id, transcript FROM calls WHERE call_id=%s",
            (call_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Unknown call")
    db_customer, db_transcript = row
    if customer_id != db_customer or transcript != db_transcript:
        raise HTTPException(status_code=403, detail="Call identity or transcript mismatch")
    # This policy is mounted read-only and is independent of the application's mutable DB role.
    allowed = POLICY.get(db_customer, set())
    if PROVIDER not in allowed:
        log.warning(json.dumps({"event":"llm_policy_denied", "customer_id":db_customer, "call_id":call_id, "provider":PROVIDER}))
        raise HTTPException(status_code=403, detail="Provider prohibited by customer policy")
    # Provider/model selection comes from this gateway's deployment, not caller-controlled routing.
    payload["model"] = f"{PROVIDER}-model"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=3.0)) as client:
            upstream = await client.post(
                f"{UPSTREAM_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {UPSTREAM_KEY}", "X-Relay-Customer-Id": db_customer},
                json=payload,
            )
        return JSONResponse(content=upstream.json(), status_code=upstream.status_code)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.error(json.dumps({"event":"llm_upstream_failure", "customer_id":db_customer, "call_id":call_id, "provider":PROVIDER, "error_type":type(exc).__name__}))
        raise HTTPException(status_code=503, detail="Allowed provider unavailable") from exc
