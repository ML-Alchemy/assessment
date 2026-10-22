from pathlib import Path

WORKER = Path("app/worker/main.py").read_text()
GATEWAY = Path("app/llm-policy/main.py").read_text()


def test_worker_does_not_add_disallowed_fallbacks():
    assert "candidates = [p for p in allowed if p in PROVIDERS]" in WORKER
    assert "allowed + [p for p in PROVIDERS if p not in allowed]" not in WORKER


def test_model_output_cannot_request_second_provider():
    assert "result.get(\"forward_to\"" not in WORKER
    assert "Never let untrusted model output choose a provider" in WORKER


def test_gateway_binds_customer_and_transcript_to_database_call():
    assert "WHERE call_id=%s" in GATEWAY
    assert "transcript != db_transcript" in GATEWAY
    assert "if PROVIDER not in allowed" in GATEWAY
    assert "POLICY_FILE" in GATEWAY
    assert "POLICY = {" in GATEWAY and "allowed_llm_providers" in GATEWAY


def test_admission_control_and_fair_claiming_are_present():
    webhook = Path("app/webhook-api/main.py").read_text()
    assert "MAX_PENDING_PER_CUSTOMER" in webhook
    assert "pg_advisory_xact_lock" in webhook
    assert 'status_code=429' in webhook
    assert "active.customer_id = calls.customer_id" in WORKER
