"""Meeting-tool simulator for the Relay take-home.

Sends call transcripts to Relay's webhook API the way a meeting tool would:
one signed webhook per call, signed with that customer's webhook secret.

Like real meeting tools, it re-sends a webhook when Relay doesn't answer
quickly with a success, and it can also deliberately re-send some calls
with the same call ID. Relay should process each call ID only once.

Every delivery is recorded in a manifest file, which the check script uses
to find calls that never reached the CRM.

This tool is part of the test harness, not part of the assignment.

Usage:
  python sim.py send TR-D002-01
  python sim.py burst --rounds 3 --resend-rate 0.2
  python sim.py flood --customer cust_fernway --count 500
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "http://webhook-api:8000")
TRANSCRIPTS_DIR = Path(os.environ.get("TRANSCRIPTS_DIR", "/fixtures/transcripts"))
MANIFEST_PATH = Path(os.environ.get("MANIFEST_PATH", "/state/sent_calls.jsonl"))
DELIVERY_TIMEOUT = float(os.environ.get("DELIVERY_TIMEOUT_SECONDS", "10"))
MAX_ATTEMPTS = int(os.environ.get("MAX_DELIVERY_ATTEMPTS", "3"))


def load_transcripts() -> dict:
    transcripts = {}
    for path in sorted(TRANSCRIPTS_DIR.glob("*.txt")):
        text = path.read_text()
        header = dict(re.findall(r"^(\w+):\s*(.+)$", text.split("---", 1)[0], re.MULTILINE))
        transcripts[header["transcript_id"]] = {
            "transcript_id": header["transcript_id"],
            "deal_id": header["deal_id"],
            "customer_id": header["customer"],
            "text": text,
        }
    return transcripts


def secret_for(customer_id: str) -> str:
    name = "WEBHOOK_SECRET_" + customer_id.upper()
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing {name}. Run `make init` to generate local secrets.")
    return value


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def record(entry: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


async def deliver(client: httpx.AsyncClient, call: dict, reason: str) -> dict:
    """Deliver one webhook, retrying on failure or timeout like a real meeting tool."""
    body = json.dumps(
        {
            "call_id": call["call_id"],
            "customer_id": call["customer_id"],
            "deal_id": call["deal_id"],
            "transcript_id": call["transcript_id"],
            "occurred_at": call["occurred_at"],
            "transcript": call["text"],
        }
    ).encode()
    url = f"{WEBHOOK_BASE_URL}/webhooks/{call['customer_id']}"
    secret = secret_for(call["customer_id"])
    outcome = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ts = int(time.time())
        headers = {"Content-Type": "application/json", "X-Relay-Signature": sign(secret, ts, body)}
        try:
            r = await client.post(url, content=body, headers=headers, timeout=DELIVERY_TIMEOUT)
            outcome = str(r.status_code)
        except httpx.TimeoutException:
            outcome = "timeout"
        except httpx.HTTPError as e:
            outcome = f"error:{type(e).__name__}"
        record(
            {
                "call_id": call["call_id"],
                "customer_id": call["customer_id"],
                "deal_id": call["deal_id"],
                "transcript_id": call["transcript_id"],
                "reason": reason if attempt == 1 else "retry",
                "attempt": attempt,
                "outcome": outcome,
                "at": time.time(),
            }
        )
        if outcome.startswith("2"):
            break
        await asyncio.sleep(2 ** (attempt - 1))
    return {"call_id": call["call_id"], "outcome": outcome}


def make_call(t: dict, run_id: str, n: int) -> dict:
    return {
        **t,
        "call_id": f"{t['transcript_id']}-{run_id}-{n:03d}",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


async def cmd_send(transcript_id: str) -> None:
    transcripts = load_transcripts()
    if transcript_id not in transcripts:
        raise SystemExit(f"Unknown transcript {transcript_id}. Options: {', '.join(transcripts)}")
    call = make_call(transcripts[transcript_id], secrets.token_hex(3), 1)
    async with httpx.AsyncClient() as client:
        result = await deliver(client, call, "new")
    print(f"{result['call_id']}: {result['outcome']}")


async def cmd_burst(rounds: int, resend_rate: float, concurrency: int) -> None:
    transcripts = list(load_transcripts().values())
    run_id = secrets.token_hex(3)
    calls = [make_call(t, run_id, r * len(transcripts) + i + 1) for r in range(rounds) for i, t in enumerate(transcripts)]
    resends = [c for c in calls if random.random() < resend_rate]
    sem = asyncio.Semaphore(concurrency)

    async def go(call, reason, delay=0.0):
        await asyncio.sleep(delay)
        async with sem:
            return await deliver(client, call, reason)

    async with httpx.AsyncClient() as client:
        tasks = [go(c, "new") for c in calls]
        tasks += [go(c, "resend", delay=random.uniform(1, 5)) for c in resends]
        results = await asyncio.gather(*tasks)

    ok = sum(1 for r in results if r["outcome"].startswith("2"))
    print(f"run {run_id}: {len(calls)} calls, {len(resends)} deliberate resends, {ok}/{len(results)} deliveries accepted")
    failed = [r for r in results if not r["outcome"].startswith("2")]
    for r in failed[:10]:
        print(f"  not accepted: {r['call_id']} ({r['outcome']})")


async def cmd_flood(customer: str, count: int, concurrency: int) -> None:
    transcripts = [t for t in load_transcripts().values() if t["customer_id"] == customer]
    if not transcripts:
        raise SystemExit(f"No transcripts for customer {customer}")
    run_id = secrets.token_hex(3)
    calls = [make_call(transcripts[i % len(transcripts)], f"flood-{run_id}", i + 1) for i in range(count)]
    sem = asyncio.Semaphore(concurrency)

    async def go(call):
        async with sem:
            return await deliver(client, call, "flood")

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(go(c) for c in calls))
    outcomes = {}
    for r in results:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    print(f"flood {run_id}: {count} calls from {customer}; outcomes: {outcomes}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Meeting-tool simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("send", help="Send one transcript as a new call")
    s.add_argument("transcript_id")
    b = sub.add_parser("burst", help="Send every transcript, several times over, at once")
    b.add_argument("--rounds", type=int, default=3)
    b.add_argument("--resend-rate", type=float, default=0.2, help="Share of calls deliberately sent twice")
    b.add_argument("--concurrency", type=int, default=20)
    f = sub.add_parser("flood", help="Send a large spike of calls from one customer")
    f.add_argument("--customer", default="cust_fernway")
    f.add_argument("--count", type=int, default=500)
    f.add_argument("--concurrency", type=int, default=50)
    args = parser.parse_args()
    if args.command == "send":
        asyncio.run(cmd_send(args.transcript_id))
    elif args.command == "flood":
        asyncio.run(cmd_flood(args.customer, args.count, args.concurrency))
    else:
        asyncio.run(cmd_burst(args.rounds, args.resend_rate, args.concurrency))


if __name__ == "__main__":
    main()
