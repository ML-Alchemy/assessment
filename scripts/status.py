"""Report whether each Relay service is up. Run through `make status`."""
import sys

import httpx

SERVICES = {
    "webhook-api": "http://webhook-api:8000/health",
    "admin-app": "http://admin-app:8001/health",
    "mock-crm": "http://mock-crm:9001/health",
    "provider-a": "http://provider-a:9100/health",
    "provider-b": "http://provider-b:9100/health",
}

healthy = True
for name, url in SERVICES.items():
    try:
        ok = httpx.get(url, timeout=5).status_code == 200
    except httpx.HTTPError:
        ok = False
    healthy &= ok
    print(f"{name:12} {'healthy' if ok else 'NOT RESPONDING'}")
sys.exit(0 if healthy else 1)
