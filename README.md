# Relay DevOps Take-Home — Implementation

**Recording:** TODO — add the 3–5 minute walkthrough link before submission.

This repository is a local-first implementation of Relay's webhook → durable queue (Postgres) → controlled LLM → CRM workflow. It prioritizes provider-policy enforcement, bounded failure behavior, secrets hygiene, structured logs, and reproducible infrastructure. The mock systems, fixtures, and verification script are retained unmodified.

## Quick start (five commands)

Run from the repository root with Docker Compose v2, Python 3.11+, `make`, and Git installed. Docker is required for the full integration test.

```bash
make init
make up
make seed
make status
make burst && make check
```

`make init` creates local-only credentials in `.env`; do not commit or share this file. `make up` builds and starts the stack. `make seed` connects each mock CRM via OAuth. `make burst` sends the fixture burst, and `make check` reports missing/duplicate calls, latency, and provider violations. `make reset` removes the local database volume and state; it is destructive.

If you have run another copy of this starter on the same machine, stop it first (`docker compose down` in that directory): both use the Compose project name `relay` and host ports 8000, 8001 and 9001. This submission uses its own Postgres volume (`relay_pgdata16`), so an older Postgres 15 volume from the original starter does not block startup. The CI `integration` job runs these exact five commands on a clean Ubuntu runner on every push.

## Architecture

```text
Meeting simulator
      | signed webhook (timestamp + HMAC)
      v
Webhook API -----> Postgres calls table <----- Worker replicas (SKIP LOCKED leases)
                         |                           |
                         |                           +--> CRM mock (bounded retries + idempotency keys)
                         |                           |
                         +--> LLM policy gateway A/B <+-- provider-specific allowlist check
                                      |                  against DB call/customer/transcript
                                      v
                             private LLM backend network
                                /             \
                           provider A       provider B

Admin UI ---> signed OAuth state ---> CRM mock
Retention worker ---> clears raw transcripts after 90 days
```

The worker is not attached to the private LLM backend network, so it cannot directly connect to either provider. Its only LLM path is the provider-specific policy gateway. The gateway verifies a shared gateway credential, binds the request to a stored call ID/customer/transcript, reads the contractual allowlist from a read-only fixture mount that the worker cannot change, and rejects prohibited providers before forwarding. The gateways and mock LLMs use a separate internal network; provider ports are not published to the host. The tools container is intentionally attached to that backend network because the supplied verification script reads provider logs.

## Security controls

- **Secrets:** `make init` generates random local passwords, webhook secrets, CRM token encryption key, gateway credential, and provider keys. `.env` is ignored by Git. Production should use Secret Manager and Cloud KMS; no cloud credentials are needed locally.
- **CRM tokens:** access and refresh tokens are Fernet-encrypted at rest in Postgres. The encryption key is separate from the database. This is defense in depth, not a substitute for KMS-backed envelope encryption and key rotation.
- **Webhook ingress:** HMAC-SHA256 signature is checked with constant-time comparison and a five-minute timestamp window; malformed payloads are rejected. Raw transcript bodies are not logged.
- **LLM egress:** all worker calls go through the provider-specific gateways. Policy comes from a read-only policy file mounted separately from the application's mutable database, not model output. No fallback to a disallowed provider is permitted. A prompt injection in the transcript cannot request a second provider because `forward_to` from model output is ignored. If all allowed providers fail, the call retries and eventually becomes visibly failed; it does not leak to a prohibited provider.
- **OAuth/admin:** the admin app requires generated HTTP Basic credentials, validates a signed OAuth state value, escapes customer names in HTML, and does not return token-exchange response bodies to the browser. Local HTTP is only for localhost; production must use HTTPS, a managed identity provider, one-time server-side OAuth state, CSRF protections, and secure session handling.
- **Containers/network:** app images use a pinned Python minor version, non-root users, read-only root filesystems where supported, `no-new-privileges`, service-only networks, restart policies, and resource limits. Postgres and admin endpoints bind to loopback only. No host port is published for the LLM providers.
- **Data lifecycle:** raw transcripts are nulled after 90 days once processing is terminal; summaries and call metadata remain. `scripts/offboard_customer.py CUSTOMER_ID --confirm-delete` deletes the customer's database records. The production offboarding workflow must also revoke CRM OAuth grants, remove object-store copies, and verify deletion from backups/replicas within the enterprise 30-day contract.
- **Logs:** JSON logs include service, timestamp, level, customer ID, and call ID where available. Do not log transcript text, credentials, OAuth codes, or access tokens. Logs may still contain customer identifiers and must have access controls and a retention policy.

### Trust boundaries and limitations

This is a single-host development stack, not a SOC 2-ready production deployment. Anyone with Docker daemon access or `.env` access can inspect secrets and containers. The gateway's call-ID/transcript binding and Docker network reduce accidental/malicious bypass from the worker, but a host administrator can alter containers, networks, or database policy. In production, run the gateway as a separately deployed service with a distinct service identity, authenticated service-to-service transport, egress firewall policy, and restricted DB permissions. The local mock provider's audit logs are used by the required verification script; the `tools` profile therefore has diagnostic access to that network.

## Reliability and failure handling

- Workers claim work with Postgres `FOR UPDATE SKIP LOCKED`, lease timestamps, bounded attempts, and exponential retry delays. Restart policies recover crashed processes; stale `in_progress` calls become eligible after the lease expires.
- HTTP calls have finite timeouts. CRM retries cover timeouts, 429s, 5xx, and token refresh; task creation includes stable idempotency keys. LLM requests time out and only retry among providers explicitly allowed for that customer.
- Worker replicas and per-service CPU/memory limits reduce noisy-neighbor risk. Webhook admission serializes per customer and rejects new work with HTTP 429 once that customer's pending/in-progress queue reaches 100; the worker selects customers with fewer outstanding calls first. For production, use durable queue quotas/concurrency limits and separate worker pools for stronger tenant isolation.
- Retention runs hourly and clears transcript bodies older than 90 days. Any call still nonterminal at expiry is marked failed before its transcript is removed. Alert on sweep errors and verify deleted rows in production.

### Failure test plan

1. Start clean and confirm `make status` reports all services healthy.
2. Run `make burst`, then `make check`; verify no duplicate or missing calls and no provider violations.
3. Stop an allowed provider (`docker compose stop provider-a`) and send calls for Halden. They should remain queued/retry/fail visibly; the gateway must never forward them to provider B. Restart it with `docker compose start provider-a` and confirm recovery.
4. Stop the CRM mock and run a small burst; observe bounded retries and retry logs. Restart CRM and check the calls complete. Task writes use idempotency keys to tolerate retries.
5. Kill one worker (`docker compose kill worker`); Compose restarts it. For a graceful lease-recovery test, stop it while a call is in progress and verify the call is reclaimed after `LEASE_SECONDS`.
6. The provided `make check` is the authoritative fixture-level report. The current execution environment used to prepare this submission did not have Docker or Terraform installed, so integration tests and `terraform validate` could not be run here; run them on the review machine and record actual output rather than claiming a pass.

## Alerts

`monitoring/alerts.yml` defines customer-visible alerts for calls stuck over five minutes and a sustained call failure rate above 5%, plus a warning when a prohibited-provider request is blocked. The first two directly correspond to missing CRM updates/summaries. These rules expect metrics to be exported by a production metrics adapter; this local starter does not include Prometheus or a metrics endpoint. In GCP, implement equivalent Cloud Monitoring alert policies over queue age, terminal failure counts, and gateway policy denials.

## Local stand-ins → GCP production services

| Local component | GCP production mapping | Notes |
|---|---|---|
| Docker Compose services | Cloud Run services/jobs or GKE | Cloud Run is the low-ops default; use GKE if custom network policy or long-lived workers require it. |
| Postgres container/volume | Cloud SQL for PostgreSQL | Private IP, backups/PITR, TLS, separate DB roles, connection pooling. |
| `.env` generated secrets | Secret Manager | Separate secret access per service identity. |
| Fernet token encryption | Cloud KMS + envelope encryption | Rotate keys and version encrypted token records. |
| Docker bridge/internal networks | VPC, private service access, Cloud Run VPC egress/firewall | Enforce LLM egress at the network and gateway layers. |
| Worker polling/leases | Cloud Run worker pool or Pub/Sub + Cloud Run workers | Pub/Sub improves burst smoothing; retain idempotency and DLQ. |
| Docker logs | Cloud Logging | Structured fields, redaction, retention, access audit. |
| `monitoring/alerts.yml` | Cloud Monitoring alert policies | Alert on customer outcomes, not only process uptime. |
| GitHub Actions | Workload Identity Federation + Artifact Registry | No long-lived service-account key in CI. |
| Local image builds | Artifact Registry | Build once, sign/scan, promote by immutable digest. |

## Terraform and environments

`infra/terraform` defines a representative US-only GCP environment: VPC/subnet, private service networking, private Cloud SQL, Secret Manager, KMS key ring/key, Artifact Registry, separate service accounts, a Cloud Run webhook service, and a Cloud Monitoring alert. It is intentionally a representative slice, not a complete deployable Relay topology: production still needs Cloud Run services/jobs for worker, admin and both gateways, a complete secret population workflow, Cloud SQL DB-user provisioning, service-to-service IAM, Cloud Armor or equivalent ingress protection, and alert notification channels. The container image input is a placeholder digest and must be replaced with a real immutable digest before planning/applying.

Use separate GCP projects for staging and production, each with independent state, VPC, database, secrets, KMS keys, service accounts, and data. Keep all customer data and backups in US regions. Production requires deletion protection, regional Cloud SQL HA, private ingress, tested restore procedures, and budget alerts. Terraform validation should run in CI; a real plan/apply is intentionally not required by the assignment.

## Delivery pipeline, promotion, and rollback

GitHub Actions compiles Python, runs tests, Ruff, Bandit, `pip-audit`, and Terraform format/validation. For a real release, build one OCI image per service, scan it, push it to Artifact Registry, and record the immutable digest plus SBOM/provenance. Deploy that same digest to staging; after integration and failure tests, require a protected-environment reviewer approval and promote the exact digest to production. Configure GitHub Actions → GCP Workload Identity Federation using repository/branch/environment attribute conditions and grant only the deployer role needed for the target environment; do not create downloadable service-account keys.

For rollback, redeploy the previous known-good image digest and revert any backward-incompatible configuration. Use expand/contract database migrations so the previous revision remains compatible; restore from Cloud SQL PITR only for data corruption, since a database restore has a larger blast radius than an application rollback. Verify health, queue age, failed calls, and CRM idempotency after rollback.

## Alerts and operations

1. **RelayCallsStuck:** oldest received/in-progress call age exceeds 300 seconds for 5 minutes — customers may be waiting for a summary or CRM update.
2. **RelayCallFailureRateHigh:** terminal call failure ratio exceeds 5% for 10 minutes — a material portion of calls may be missing outcomes.
3. **RelayLLMPolicyDenials:** any provider-policy denial in 5 minutes — investigate whether an application bug or tampering attempted prohibited egress.

Metrics in `monitoring/alerts.yml` are names expected from a production exporter; the starter does not expose them natively. Do not interpret this as an alerting stack already running locally.

## Problems found and fixes

- Worker previously appended every configured provider after the customer's allowlist, so an unavailable allowed provider could leak transcripts to a prohibited provider. Fixed by restricting candidates to the allowlist and failing/retrying if none is available.
- Worker previously trusted model output's `forward_to` field to send a second copy of the transcript to arbitrary providers. Removed this behavior; transcript content is untrusted input.
- Worker HTTP calls to the LLM had no timeout. Added finite connect/request timeouts.
- CRM access tokens were printed in debug logs. Removed token logging; transcript payload logging was also removed from webhook handling.
- Webhook signatures had no timestamp freshness check and used ordinary string comparison. Added a five-minute replay window and constant-time comparison, plus payload validation.
- There was no tenant-level queue admission control. Added a transactional per-customer pending-call cap with HTTP 429 and fairer worker claim ordering to limit noisy-neighbor effects.
- Admin OAuth state was not validated. Added an HMAC-signed state token and customer existence checks. A production implementation should additionally store one-time state server-side and expire it after callback.
- Containers used floating `python:latest` images and ran as root. App/service Dockerfiles now use `python:3.12-slim` and non-root users; mocks remain untouched per assignment constraints.
- Local Postgres was published on all host interfaces and the mock LLM ports were published. Postgres and LLM ports are no longer published; admin/webhook/mock CRM bind to loopback.
- Added raw transcript retention and a confirmed customer offboarding script.

## AI agent handling: alerts and logs

I would allow a read-only agent to query approved logs/metrics, correlate customer/call IDs, summarize likely causes, draft incident tickets, and propose a remediation plan. It may run a small allowlisted set of read-only diagnostics in a sandbox. It must not execute arbitrary shell commands, change IAM/firewall/secrets, deploy code, delete data, or restart production services without a human approval gate and audited tool call. Treat logs, transcripts, stack traces, and alert annotations as untrusted data, never as instructions: strip/escape control characters, separate evidence from policy, use structured schemas and allowlisted tools, keep the system policy outside retrieved content, and require human confirmation for side effects. A prompt injection in a transcript/log must not be able to change the agent's permissions.

## Decisions, trade-offs, and cost

- Local-first Docker Compose avoids cloud spend and lets reviewers reproduce the core behavior. The price is weaker isolation than a managed cloud deployment.
- Postgres polling is simple and already supported by the starter; Pub/Sub is a production option for better burst absorption. `SKIP LOCKED`, leases, retry bounds, and idempotency reduce duplicate side effects.
- Provider-specific gateways and network segmentation cost two small services but centralize policy enforcement. For stricter production isolation, use distinct workload identities and a policy store that gateway identities can read but application identities cannot alter; do not rely on a fixture mount as the long-term policy store.
- Terraform uses a small Cloud SQL tier for validation/reference only. Production should size Cloud SQL for HA and workload, and use budgets/quotas before enabling resources. No GCP resources are created by local setup.
- Full tracing, a local Prometheus/Grafana stack, real cloud deployment, automated CI deployment, zero-downtime token rotation, and cost accounting were cut to protect the timebox and prioritize the three core requirements.

## Assumptions

- The fixture's `allowed_llm_providers` values are the contractual source of truth.
- A call's `call_id` uniquely identifies it across all customers, matching the starter schema and verification script.
- If one provider in a customer's allowlist is down, trying another provider in that same allowlist is permitted; using any disallowed provider is not.
- Raw transcript retention begins at webhook receipt; summaries and non-transcript metadata may remain longer.
- “Delete all customer data within 30 days” includes database rows, logs/exports, object storage, and backups in production. The local script covers database records only and must be combined with external revocation and backup-expiry workflows.
- Local admin UI is a developer-only interface, not an internet-facing production admin portal.

## Application code changes

Changes were made to `app/worker/main.py`, `app/webhook-api/main.py`, and `app/admin-app/main.py` for provider allowlisting, call-ID-bound gateway requests, finite timeouts, removal of unsafe model-directed forwarding and secret logging, webhook replay/payload validation, and signed OAuth state. These were necessary to satisfy core security requirements. New files add the policy gateway, retention/offboarding tools, CI, Terraform, alerts, tests, and this README. The mocks, fixtures, and `scripts/check.py` verification script were not modified.

## AI assistant use

An AI assistant was used to review the assignment, identify likely security/reliability defects, draft infrastructure and documentation, and propose tests. The candidate must review the diffs, run the stack and tests, validate Terraform, and be able to explain and modify each change in the follow-up interview.
