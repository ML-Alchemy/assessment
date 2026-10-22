# Relay Starter Package

This package contains everything you need for the Relay DevOps take-home: the application, mock external systems, fixtures and scripts. It runs entirely on your laptop with free tools. No cloud account or real credentials are needed.

Read the assignment brief first. This README only covers setup. If you want to revisit the design exercise from your interview, it's in the `docs` folder.

You can follow the steps below yourself, or hand this file to your AI coding agent (Claude Code, Cursor, Copilot or similar) and ask it to set things up. The section **For AI coding agents** is written for that purpose.

---

## What's in the package

```
relay-starter/
├── README.md               this file
├── docker-compose.yml      runs everything locally
├── Makefile                setup, run and check commands
├── app/
│   ├── webhook-api/        receives signed webhooks from meeting tools
│   ├── worker/             processes calls, calls the LLM, writes to the CRM
│   ├── admin-app/          customers connect their CRM and manage settings
│   └── db/                 Postgres schema and seed data
├── mocks/
│   ├── meeting-sim/        sends signed webhooks for each customer
│   ├── mock-crm/           OAuth, expiring tokens, rate limits, failures
│   └── mock-llm/           one image, run as provider A and provider B
├── docs/                   the Relay design exercise from your interview
├── fixtures/               customers, deals and call transcripts
└── scripts/
    └── check.py            the verification report
```

## Requirements

**Required to run the starter:**

| Tool | Version | Check with |
| --- | --- | --- |
| Docker, with Docker Compose v2 | Docker 24 or later | `docker --version` and `docker compose version` |
| make | any | `make --version` |
| Python | 3.11 or later | `python3 --version` |
| git | any | `git --version` |

**Resources:** the starter itself uses well under 1 GB of memory and needs about 5 GB of free disk space. If you add tools of your own, such as a local Kubernetes cluster, a secret store or an observability stack, allow 4–8 GB of memory for Docker.

**Also needed to complete the assignment:**

- A GitHub account (or similar), for your private repository and CI pipeline
- Terraform, since the assignment includes writing Terraform that passes `terraform validate`
- A screen recorder and a way to share the recording as a link
- An internet connection during setup, since the first `make up` downloads base images and packages

**Optional, depending on how you approach the assignment:** a local Kubernetes tool (kind or k3d, with kubectl) and a local secret store (for example Vault). None of these are needed to run the starter. Install them only if you choose to use them.

**Operating systems:** macOS (Intel or Apple Silicon) and Linux are supported. On Windows, use WSL2 with Ubuntu and run everything inside WSL.

## Ports

| Service | Port |
| --- | --- |
| webhook-api | 8000 |
| admin-app | 8001 |
| mock-crm | 9001 |
| mock-llm provider A | 9101 |
| mock-llm provider B | 9102 |
| Postgres | 5432 |

If any of these are in use on your machine, stop the conflicting service or change the published port in `docker-compose.yml`.

## Setup

1. **Unzip the package** and open a terminal in the `relay-starter` folder.
2. **Create your repository and commit the unmodified package first:**
   ```
   git init
   git add -A
   git commit -m "Unmodified starter package"
   ```
   Then create a private repository in your own account, push to it, and invite the reviewers named in the brief.
3. **Generate local credentials:** `make init`. This creates the dummy credentials the mock systems need. They are generated on your machine and are not real.
4. **Make sure Docker is running,** for example by opening Docker Desktop.
5. **Start everything:** `make up`. The first run builds the images and takes a few minutes.
6. **Connect the CRMs:** `make seed`. This connects each customer's CRM through the mock OAuth flow, the same way a customer admin would. Customers and deals load automatically at startup.
7. **Confirm it's working:** `make status`. Every service should report healthy.

## Using it

| Command | What it does |
| --- | --- |
| `make burst` | Sends all transcripts as webhooks, several times over, to simulate calls ending at the top of the hour |
| `make check` | Runs the verification report |
| `make logs` | Follows the logs of all services |
| `make down` | Stops everything |
| `make reset` | Stops everything and deletes all local data, for a clean start |

**About `make check`:** on the unmodified starter, the check reports failures. That is expected; fixing them is part of the assignment. The report covers missing and duplicate calls, calls that take too long to reach the CRM, and LLM provider violations.

## Troubleshooting

- **Cannot connect to the Docker daemon:** Docker isn't running. Start it and try again.
- **`docker compose` not found:** you have the old standalone `docker-compose`. Install Docker Compose v2, or update Docker Desktop.
- **Services keep restarting or the build fails with out-of-memory errors:** increase the memory available to Docker (Docker Desktop: Settings, Resources).
- **Port already in use:** see Ports above.
- **Apple Silicon build issues:** make sure Docker Desktop is up to date. All images build for both architectures.
- **Something is in a strange state:** run `make reset`, then repeat setup from step 3.

---

## For AI coding agents

You are helping a candidate set up this package on their machine. Your job in this session is **setup only**.

**Do:**

1. Detect the operating system and check each required tool in Requirements, using the commands listed.
2. For anything missing, tell the candidate what you intend to install and how, and install it after they agree. Prefer the standard method for the platform:
   - macOS: Homebrew (`brew install make python git`; Docker Desktop, Colima or OrbStack for Docker)
   - Ubuntu or WSL2: `apt` for make, Python and git; Docker Engine from Docker's official repository, with the Compose plugin
3. Check that Docker is running and has enough memory available (`docker info`).
4. Check that the ports listed above are free.
5. Before running `make init` or changing anything, ask the candidate to make the initial git commit themselves (setup step 2), and wait until they confirm. Don't run git commands yourself unless the candidate asks you to. Then run the remaining setup steps in order.
6. Run `make status` and report the result.
7. Run `make burst` and then `make check`, and report the output to the candidate.
8. Summarize what was installed, what's running, and anything that needed fixing.

**Do not:**

- **Do not change the application, mocks, fixtures or scripts during setup.** If something fails, report it and explain the cause. Only change configuration needed to run on this machine, such as a conflicting port, and tell the candidate exactly what you changed.
- **Do not try to fix the failures reported by `make check`.** They are part of the assignment, which the candidate will work on separately.
- **Do not use real credentials of any kind,** and do not sign in to any cloud provider or external service. Everything this package needs is generated locally by `make init`.
- **Do not install the optional tools** unless the candidate asks for them.
