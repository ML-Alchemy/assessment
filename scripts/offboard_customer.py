"""Permanently delete a customer's Relay data. Requires an explicit confirmation."""
import os
import sys
import psycopg

if len(sys.argv) != 3 or sys.argv[2] != "--confirm-delete":
    raise SystemExit("Usage: python offboard_customer.py CUSTOMER_ID --confirm-delete")
customer_id = sys.argv[1]
if not customer_id.startswith("cust_"):
    raise SystemExit("Refusing unexpected customer ID format")
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.transaction():
        exists = conn.execute("SELECT 1 FROM customers WHERE id=%s FOR UPDATE", (customer_id,)).fetchone()
        if not exists:
            raise SystemExit("Customer not found")
        conn.execute("DELETE FROM summaries WHERE call_ref IN (SELECT id FROM calls WHERE customer_id=%s)", (customer_id,))
        conn.execute("DELETE FROM calls WHERE customer_id=%s", (customer_id,))
        conn.execute("DELETE FROM customers WHERE id=%s", (customer_id,))
print(f"Deleted Relay database records for {customer_id}. Also revoke external OAuth grants and expire backups per the retention schedule.")
