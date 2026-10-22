"""Mock CRM for the Relay take-home.

A stand-in for Salesforce or HubSpot. It holds each customer's deals,
issues expiring OAuth tokens, accepts deal updates and task creation,
and can be told to misbehave (errors, latency, and timeouts that happen
after a write is saved).

Endpoints under /admin are test-only. Relay itself never calls them;
the scripts use them to inject failures and to report what happened.

This service is part of the test harness, not part of the assignment.
"""

import asyncio
import json
import os
import random
import secrets
import time
from collections import defaultdict, deque
from datetime import date
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

DEALS_PATH = os.environ.get("DEALS_PATH", "/fixtures/deals.json")
CLIENT_ID = os.environ.get("MOCK_CRM_CLIENT_ID", "relay-local")
CLIENT_SECRET = os.environ.get("MOCK_CRM_CLIENT_SECRET", "")
ACCESS_TOKEN_TTL = int(os.environ.get("ACCESS_TOKEN_TTL_SECONDS", "600"))
RATE_LIMIT_RPS = int(os.environ.get("RATE_LIMIT_PER_SECOND", "5"))
IDEMPOTENCY_WINDOW = int(os.environ.get("IDEMPOTENCY_WINDOW_SECONDS", "86400"))
CODE_TTL = 300

DEFAULT_CHAOS = {
    "enabled": False,
    "error_rate": 0.20,
    "timeout_after_save_rate": 0.10,
    "latency_ms": [1000, 3000],
    "hang_seconds": 30,
    # Reproducibility: random failures follow a fixed seed, so every run sees the same pattern.
    "seed": 42,
    # Guaranteed minimums: every Nth request fails, and every Nth write times out after saving (0 = off).
    "error_every": 0,
    "timeout_every": 0,
}

app = FastAPI(title="Mock CRM", docs_url="/admin/docs", redoc_url=None)

state: dict = {}
lock = asyncio.Lock()


def reset_state() -> None:
    with open(DEALS_PATH) as f:
        data = json.load(f)
    deals = {d["deal_id"]: dict(d) for d in data["deals"]}
    state.clear()
    state.update(
        {
            "deals": deals,
            "contacts": data.get("contacts", []),
            "codes": {},  # code -> {account, redirect_uri, expires}
            "access_tokens": {},  # token -> {account, expires, refresh, revoked}
            "refresh_tokens": {},  # token -> {account, revoked}
            "tasks": [],
            "idempotency": {},  # (account, key) -> {task, at}
            "deal_updates": [],
            "rate": defaultdict(deque),  # account -> request timestamps
            "chaos": dict(DEFAULT_CHAOS),
            "chaos_rng": random.Random(DEFAULT_CHAOS["seed"]),
            "chaos_counts": {"requests": 0, "writes": 0},
            "requests": defaultdict(int),
        }
    )


reset_state()


# ---------- helpers ----------


def error(status: int, code: str, message: str, headers: dict | None = None):
    raise HTTPException(status_code=status, detail={"error": code, "message": message}, headers=headers)


def authenticate(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        error(401, "invalid_token", "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    info = state["access_tokens"].get(token)
    if not info or info["revoked"]:
        error(401, "invalid_token", "Unknown or revoked token")
    if time.time() >= info["expires"]:
        error(401, "invalid_token", "Token expired")
    return info["account"]


def check_rate_limit(account: str) -> None:
    now = time.monotonic()
    window = state["rate"][account]
    while window and now - window[0] > 1.0:
        window.popleft()
    if len(window) >= RATE_LIMIT_RPS:
        error(429, "rate_limited", "Too many requests", headers={"Retry-After": "1"})
    window.append(now)


async def before_request(account: str) -> None:
    state["requests"][account] += 1
    check_rate_limit(account)
    chaos = state["chaos"]
    if not chaos["enabled"]:
        return
    rng, counts = state["chaos_rng"], state["chaos_counts"]
    counts["requests"] += 1
    lo, hi = chaos["latency_ms"]
    await asyncio.sleep(rng.uniform(lo, hi) / 1000)
    every = chaos["error_every"]
    if (every and counts["requests"] % every == 0) or rng.random() < chaos["error_rate"]:
        error(503, "unavailable", "Temporary failure, try again")


async def after_write(response_body: dict, status: int = 200) -> JSONResponse:
    """Optionally hang after the write is already saved, so the caller
    times out even though the change happened."""
    chaos = state["chaos"]
    if not chaos["enabled"]:
        return JSONResponse(response_body, status_code=status)
    counts = state["chaos_counts"]
    counts["writes"] += 1
    every = chaos["timeout_every"]
    if (every and counts["writes"] % every == 0) or state["chaos_rng"].random() < chaos["timeout_after_save_rate"]:
        await asyncio.sleep(chaos["hang_seconds"])
        return JSONResponse({"error": "gateway_timeout", "message": "Upstream timed out"}, status_code=504)
    return JSONResponse(response_body, status_code=status)


def get_deal_for(account: str, deal_id: str) -> dict:
    deal = state["deals"].get(deal_id)
    if deal is None:
        error(404, "not_found", f"Deal {deal_id} not found")
    if deal["customer_id"] != account:
        error(403, "forbidden", "This token cannot access that record")
    return deal


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


# ---------- OAuth ----------


@app.get("/oauth/authorize")
def authorize(request: Request, client_id: str, redirect_uri: str, account: str):
    """Simplified authorization: approves automatically.

    `account` selects which customer's CRM is being connected (a real CRM
    would know this from the user's login).
    """
    oauth_state = request.query_params.get("state")
    if client_id != CLIENT_ID:
        error(400, "invalid_client", "Unknown client_id")
    if not any(d["customer_id"] == account for d in state["deals"].values()):
        error(400, "invalid_account", "Unknown account")
    code = secrets.token_urlsafe(16)
    state["codes"][code] = {"account": account, "redirect_uri": redirect_uri, "expires": time.time() + CODE_TTL}
    params = {"code": code}
    if oauth_state:
        params["state"] = oauth_state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


@app.post("/oauth/token")
def token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    refresh_token: str | None = Form(None),
):
    if client_id != CLIENT_ID or not CLIENT_SECRET or client_secret != CLIENT_SECRET:
        error(401, "invalid_client", "Invalid client credentials")

    if grant_type == "authorization_code":
        entry = state["codes"].pop(code or "", None)
        if not entry or time.time() > entry["expires"]:
            error(400, "invalid_grant", "Invalid or expired code")
        if redirect_uri and redirect_uri != entry["redirect_uri"]:
            error(400, "invalid_grant", "redirect_uri mismatch")
        account = entry["account"]
        rt = new_token("rt")
        state["refresh_tokens"][rt] = {"account": account, "revoked": False}
    elif grant_type == "refresh_token":
        info = state["refresh_tokens"].get(refresh_token or "")
        if not info or info["revoked"]:
            error(400, "invalid_grant", "Invalid or revoked refresh token")
        account = info["account"]
        rt = refresh_token
    else:
        error(400, "unsupported_grant_type", "Unsupported grant_type")

    at = new_token("at")
    state["access_tokens"][at] = {
        "account": account,
        "expires": time.time() + ACCESS_TOKEN_TTL,
        "refresh": rt,
        "revoked": False,
    }
    return {
        "access_token": at,
        "refresh_token": rt,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "account": account,
    }


@app.post("/oauth/revoke")
def revoke(token: str = Form(...)):
    if token in state["access_tokens"]:
        state["access_tokens"][token]["revoked"] = True
    elif token in state["refresh_tokens"]:
        state["refresh_tokens"][token]["revoked"] = True
        for info in state["access_tokens"].values():
            if info["refresh"] == token:
                info["revoked"] = True
    return {"revoked": True}


# ---------- API used by Relay ----------


class DealUpdate(BaseModel):
    next_step: str | None = None
    close_date: date | None = None
    notes: str | None = None


class TaskIn(BaseModel):
    deal_id: str
    subject: str
    due_date: date | None = None
    source_call_id: str | None = None


@app.get("/api/deals/{deal_id}")
async def read_deal(deal_id: str, authorization: str | None = Header(None)):
    account = authenticate(authorization)
    await before_request(account)
    return get_deal_for(account, deal_id)


@app.patch("/api/deals/{deal_id}")
async def update_deal(deal_id: str, body: DealUpdate, authorization: str | None = Header(None)):
    account = authenticate(authorization)
    await before_request(account)
    async with lock:
        deal = get_deal_for(account, deal_id)
        changes = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in body.model_dump().items() if v is not None}
        deal.update(changes)
        state["deal_updates"].append({"deal_id": deal_id, "account": account, "changes": changes, "at": time.time()})
        result = dict(deal)
    return await after_write(result)


@app.post("/api/tasks")
async def create_task(
    body: TaskIn,
    authorization: str | None = Header(None),
    idempotency_key: str | None = Header(None),
):
    account = authenticate(authorization)
    await before_request(account)
    async with lock:
        get_deal_for(account, body.deal_id)
        now = time.time()
        if idempotency_key:
            prior = state["idempotency"].get((account, idempotency_key))
            if prior and now - prior["at"] < IDEMPOTENCY_WINDOW:
                return JSONResponse(prior["task"], status_code=200, headers={"Idempotent-Replayed": "true"})
        task = {
            "task_id": f"T-{len(state['tasks']) + 1:05d}",
            "account": account,
            "deal_id": body.deal_id,
            "subject": body.subject,
            "due_date": body.due_date.isoformat() if body.due_date else None,
            "source_call_id": body.source_call_id,
            "created_at": now,
        }
        state["tasks"].append(task)
        if idempotency_key:
            state["idempotency"][(account, idempotency_key)] = {"task": task, "at": now}
    return await after_write(task, status=201)


@app.get("/api/tasks")
async def list_tasks(deal_id: str | None = None, authorization: str | None = Header(None)):
    account = authenticate(authorization)
    await before_request(account)
    return [t for t in state["tasks"] if t["account"] == account and (deal_id is None or t["deal_id"] == deal_id)]


# ---------- test-only endpoints ----------


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/admin/report")
def report():
    by_call = defaultdict(list)
    for t in state["tasks"]:
        by_call[t["source_call_id"]].append(t)
    duplicates = []
    for call_id, tasks in by_call.items():
        seen = defaultdict(list)
        for t in tasks:
            seen[(t["deal_id"], t["subject"])].append(t["task_id"])
        for (deal_id, subject), ids in seen.items():
            if len(ids) > 1:
                duplicates.append({"source_call_id": call_id, "deal_id": deal_id, "subject": subject, "task_ids": ids})
    return {
        "tasks_total": len(state["tasks"]),
        "tasks_per_call": {str(k): len(v) for k, v in by_call.items()},
        "first_task_at": {str(k): min(t["created_at"] for t in v) for k, v in by_call.items()},
        "duplicates": duplicates,
        "deal_updates": len(state["deal_updates"]),
        "requests_per_account": dict(state["requests"]),
    }


@app.get("/admin/chaos")
def get_chaos():
    return state["chaos"]


class ChaosIn(BaseModel):
    enabled: bool
    error_rate: float | None = None
    timeout_after_save_rate: float | None = None
    latency_ms: list[int] | None = None
    hang_seconds: float | None = None
    seed: int | None = None
    error_every: int | None = None
    timeout_every: int | None = None


@app.post("/admin/chaos")
def set_chaos(body: ChaosIn):
    chaos = dict(DEFAULT_CHAOS)
    chaos.update({k: v for k, v in body.model_dump().items() if v is not None})
    state["chaos"] = chaos
    state["chaos_rng"] = random.Random(chaos["seed"])
    state["chaos_counts"] = {"requests": 0, "writes": 0}
    return chaos


@app.get("/admin/issued-tokens")
def issued_tokens():
    return {
        "access_tokens": list(state["access_tokens"].keys()),
        "refresh_tokens": list(state["refresh_tokens"].keys()),
    }


@app.post("/admin/reset")
def admin_reset():
    reset_state()
    return {"reset": True}
