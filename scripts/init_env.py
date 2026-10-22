"""Generate local credentials for the starter package and write them to .env.

Every value is random and generated on this machine. None of them are real.
Run through `make init`. If .env already exists, it is left unchanged.
"""
import base64
import os
import secrets
from pathlib import Path

env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    print(".env already exists; leaving it unchanged.")
    raise SystemExit(0)

db_password = secrets.token_urlsafe(16)
values = {
    "LOG_LEVEL": "DEBUG",
    "LOG_FORMAT": "text",
    "POSTGRES_PASSWORD": db_password,
    "DATABASE_URL": f"postgresql://postgres:{db_password}@postgres:5432/relay",
    "MOCK_CRM_URL": "http://mock-crm:9001",
    "MOCK_CRM_CLIENT_ID": "relay-local",
    "MOCK_CRM_CLIENT_SECRET": secrets.token_urlsafe(24),
    "CRM_TOKEN_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
    "PROVIDER_A_URL": "http://provider-a:9100",
    "PROVIDER_A_API_KEY": "pa-" + secrets.token_urlsafe(24),
    "PROVIDER_B_URL": "http://provider-b:9100",
    "PROVIDER_B_API_KEY": "pb-" + secrets.token_urlsafe(24),
    "WEBHOOK_SECRET_CUST_FERNWAY": "whsec_" + secrets.token_urlsafe(24),
    "WEBHOOK_SECRET_CUST_HALDEN": "whsec_" + secrets.token_urlsafe(24),
    "WEBHOOK_SECRET_CUST_QUARRY": "whsec_" + secrets.token_urlsafe(24),
}
env_path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
state = env_path.parent / "state"
state.mkdir(exist_ok=True)
state.chmod(0o777)  # writable by the tool containers
print(f"Wrote {len(values)} local settings to .env")
