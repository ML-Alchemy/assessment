"""Connect each customer's CRM to Relay through the admin app's OAuth flow,
the same way a customer admin would by clicking "Connect CRM".

URL_MAP rewrites the browser-facing addresses (localhost) to the addresses
reachable from inside the Docker network, when this runs in a container.
"""
import os
import sys

import httpx

ADMIN_URL = os.environ.get("ADMIN_URL", "http://admin-app:8001")
ADMIN_USER = os.environ["ADMIN_USERNAME"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
CUSTOMERS = ["cust_fernway", "cust_halden", "cust_quarry"]
URL_MAP = dict(pair.split("=", 1) for pair in os.environ.get("URL_MAP", "").split(",") if "=" in pair)


def internal(url: str) -> str:
    for public, inside in URL_MAP.items():
        if url.startswith(public):
            return inside + url[len(public):]
    return url


def connect(client: httpx.Client, customer: str) -> None:
    r = client.get(f"{ADMIN_URL}/connect/{customer}", auth=(ADMIN_USER, ADMIN_PASSWORD))
    r = client.get(internal(r.headers["location"]))  # CRM authorize page
    r = client.get(internal(r.headers["location"]))  # back to the admin app callback
    if r.status_code != 302:
        sys.exit(f"Connecting {customer} failed: {r.status_code} {r.text}")
    print(f"Connected CRM for {customer}")


with httpx.Client(follow_redirects=False, timeout=10) as client:
    for c in CUSTOMERS:
        connect(client, c)
