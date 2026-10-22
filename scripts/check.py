"""Relay verification report.

Compares what the meeting-tool simulator sent with what reached the mock CRM
and the mock LLM providers, and reports:

  - calls that haven't reached the CRM (some may still be waiting or retrying)
  - duplicate CRM tasks
  - transcripts that reached an LLM provider the customer doesn't allow
  - calls that took longer than customers expect to reach the CRM

Traffic sent with the simulator's flood mode is reported separately: a platform
may reject some of it to protect other customers.

It waits for processing to settle before reporting, so calls still in flight
aren't counted as missing: it waits until no new CRM tasks have appeared for
45 seconds, up to 5 minutes. Exits with status 1 if any problem is found.
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

MANIFEST_PATH = Path(os.environ.get("MANIFEST_PATH", "/state/sent_calls.jsonl"))
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", "/fixtures"))
CRM_URL = os.environ.get("MOCK_CRM_URL", "http://mock-crm:9001")
PROVIDER_URLS = {
    "provider-a": os.environ.get("PROVIDER_A_URL", "http://provider-a:9100"),
    "provider-b": os.environ.get("PROVIDER_B_URL", "http://provider-b:9100"),
}
SETTLE_SECONDS = float(os.environ.get("CHECK_SETTLE_SECONDS", "45"))
MAX_DELAY_SECONDS = float(os.environ.get("CHECK_MAX_DELAY_SECONDS", "900"))
MAX_WAIT_SECONDS = float(os.environ.get("CHECK_MAX_WAIT_SECONDS", "300"))
SHINGLE = 6
MIN_MATCHES = 3


# ---------- fixtures ----------


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def shingles(text: str) -> set[tuple]:
    w = words(text)
    return {tuple(w[i : i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)}


def load_fixtures():
    allowed = {}
    current = None
    for line in (FIXTURES_DIR / "customers.yaml").read_text().splitlines():
        m = re.match(r"\s*-\s*id:\s*(\S+)", line)
        if m:
            current = m.group(1)
        m = re.match(r"\s*allowed_llm_providers:\s*\[(.*)\]", line)
        if m and current:
            allowed[current] = {p.strip() for p in m.group(1).split(",") if p.strip()}

    transcripts = {}
    for path in sorted((FIXTURES_DIR / "transcripts").glob("*.txt")):
        text = path.read_text()
        customer = re.search(r"^customer:\s*(\S+)", text, re.MULTILINE).group(1)
        spoken = " ".join(m.group(1) for m in re.finditer(r"^\[[\d:]+\]\s+[^:]+:\s+(.*)$", text, re.MULTILINE))
        transcripts[path.stem] = {"customer": customer, "shingles": shingles(spoken)}
    return allowed, transcripts


def identify(content: str, transcripts: dict) -> list[str]:
    """Which fixture transcripts appear in a request, even if names or headers were stripped."""
    got = shingles(content)
    return [tid for tid, t in transcripts.items() if len(t["shingles"] & got) >= MIN_MATCHES]


# ---------- gathering results ----------


def load_manifest():
    if not MANIFEST_PATH.exists():
        sys.exit("No calls have been sent yet. Run `make burst` first.")
    sent = {}
    for line in MANIFEST_PATH.read_text().splitlines():
        e = json.loads(line)
        sent.setdefault(
            e["call_id"],
            {"customer": e["customer_id"], "accepted": False, "flood": e["reason"] == "flood", "first_sent": e["at"]},
        )
        if e["outcome"].startswith("2"):
            sent[e["call_id"]]["accepted"] = True
    return sent


def crm_report():
    return httpx.get(f"{CRM_URL}/admin/report", timeout=10).json()


def wait_until_settled() -> dict:
    """Wait until the CRM's task count stops changing."""
    start = time.time()
    last_total, last_change = None, time.time()
    while True:
        report = crm_report()
        if report["tasks_total"] != last_total:
            last_total, last_change = report["tasks_total"], time.time()
        if time.time() - last_change >= SETTLE_SECONDS:
            return report
        if time.time() - start >= MAX_WAIT_SECONDS:
            print(f"Stopped waiting after {MAX_WAIT_SECONDS:.0f} seconds; calls may still be in progress.\n")
            return report
        time.sleep(2)


def provider_violations(allowed: dict, transcripts: dict) -> dict:
    violations = defaultdict(int)
    for provider, url in PROVIDER_URLS.items():
        entries = httpx.get(f"{url}/admin/received", timeout=10).json()["entries"]
        for e in entries:
            for tid in identify(e["content"], transcripts):
                customer = transcripts[tid]["customer"]
                if provider not in allowed.get(customer, set()):
                    violations[(customer, tid, provider)] += 1
    return violations


# ---------- report ----------


def main() -> None:
    allowed, transcripts = load_fixtures()
    sent = load_manifest()
    print(f"Waiting for processing to settle (up to {MAX_WAIT_SECONDS:.0f} seconds)...\n")
    report = wait_until_settled()

    reached = set(report["tasks_per_call"])
    normal = {c: s for c, s in sent.items() if not s["flood"]}
    flood = {c: s for c, s in sent.items() if s["flood"]}
    not_accepted = sorted(c for c, s in normal.items() if not s["accepted"])
    missing = sorted(c for c, s in normal.items() if s["accepted"] and c not in reached)
    delays = {}
    for c, s in normal.items():
        if c in report.get("first_task_at", {}):
            delays[c] = report["first_task_at"][c] - s["first_sent"]
    slow = sorted((c for c, d in delays.items() if d > MAX_DELAY_SECONDS), key=lambda c: -delays[c])
    duplicates = report["duplicates"]
    violations = provider_violations(allowed, transcripts)

    print("Relay verification report")
    print("=========================")
    print(f"Calls sent: {len(normal)} (plus {len(flood)} flood calls)")
    reached_count = sum(1 for c, s in normal.items() if s["accepted"] and c in reached)
    print(f"Calls that reached the CRM: {reached_count} of {len(normal)}")
    print(f"CRM tasks created: {report['tasks_total']} (one per next step; a call can have several)")
    if delays:
        ordered = sorted(delays.values())
        print(f"Time to reach the CRM: median {ordered[len(ordered) // 2]:.0f}s, slowest {ordered[-1]:.0f}s")
    print()

    problems = 0

    print(f"Calls rejected by Relay's webhook API: {len(not_accepted)}")
    for c in not_accepted[:10]:
        print(f"  - {c}")
    problems += len(not_accepted)

    print(f"Calls that haven't reached the CRM: {len(missing)}")
    if missing:
        print("  (Some may still be waiting or retrying. Run the check again later to confirm whether any were lost.)")
    for c in missing[:10]:
        print(f"  - {c} ({sent[c]['customer']})")
    problems += len(missing)

    print(f"Duplicate CRM tasks: {len(duplicates)}")
    for d in duplicates[:10]:
        print(f"  - {d['source_call_id']}: \"{d['subject'][:60]}\" created {len(d['task_ids'])} times")
    problems += len(duplicates)

    print(f"Calls slower than {MAX_DELAY_SECONDS:.0f}s to reach the CRM: {len(slow)}")
    for c in slow[:10]:
        print(f"  - {c} ({normal[c]['customer']}): {delays[c]:.0f}s")
    problems += len(slow)

    print(f"LLM provider violations: {sum(violations.values())}")
    for (customer, tid, provider), n in sorted(violations.items()):
        print(f"  - {customer}'s transcript {tid} reached {provider}, which {customer} doesn't allow ({n} time{'s' if n != 1 else ''})")
    problems += len(violations)

    if flood:
        accepted = sum(1 for s in flood.values() if s["accepted"])
        print(f"\nFlood traffic: {len(flood)} calls, {accepted} accepted, {len(flood) - accepted} rejected, "
              f"{sum(1 for c in flood if c in reached)} reached the CRM")

    print()
    if problems:
        print("RESULT: FAIL")
        sys.exit(1)
    print("RESULT: PASS")


if __name__ == "__main__":
    main()
