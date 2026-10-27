# P0 Release Gate — Evidence Index

Status date: 2026-10-27. Branch under review: `integration/p0` (single
integration branch; no remote is configured in this environment, so no hosted
PR exists).

Each Release Gate item (from
`.scratch/p0-platform/issues/16-release-gate.md`) is mapped to its evidence
source. Items marked **BLOCKED-ENVIRONMENT** require a deployment Compose
environment, live model credentials, or a trace-export service. The isolated
suite ran with disposable Docker-backed PostgreSQL; no Compose deployment,
live model call, or external trace export is claimed.

| Release Gate item | Evidence source | Status |
| --- | --- | --- |
| `docker compose up` one-shot cold start, health checks green, repeatable | `docs/verification/2026-10-26-compose-baseline.md` (postgres+migrate+api at migrations 0001–0005, commit `3a559e7`); current schema 13 deployment cold-start not exercised | PARTIAL — Compose deployment not run in this task |
| Migrations + seed repeatable everywhere, `data_snapshot_id` stable | Offline determinism assertions plus the full isolated suite applying migrations through 0013 and exercising the seed path; snapshot id `e38d3afb…b5b8` | PARTIAL — isolated PostgreSQL passes; deployed volume not tested |
| API documentation (endpoints, error codes, SSE semantics, Registry responses) | `docs/api.md` — updated from source and verified response contracts | DONE |
| Operations runbook | `docs/runbook.md` — backup, migration failure, Bootstrap, disable, and recovery steps | DONE |
| Test run reports | Full repository suite: `uv run pytest -q` — **545 passed in 993.73s** (2026-10-27), including unit and disposable-PostgreSQL integration tests | PASS — isolated suite; no deployment Compose run |
| Migration version record | `docs/verification/2026-10-26-compose-baseline.md` records ledger 1–5; isolated tests verify schema version 13; no deployment ledger capture | PARTIAL — deployed ledger not read |
| API request/response records | Reproducible via the integration suite (HTTP seam) and the compose baseline HTTP table; not separately archived | PARTIAL |
| Worker crash-recovery records | Fault-injection tests cover the eight crash windows and pass in the full isolated suite; no separate runtime trace archive | PARTIAL — test assertions pass; deployment records absent |
| Tool permission-denial records | Denied `tool.invocation` audit rows are asserted in passing integration tests (unbound tool, invalid arguments, region denial); no separate runtime archive | PARTIAL — test assertions pass; deployment records absent |
| ≥3 live DeepSeek end-to-end runs, fully recorded | Ticket 13 deferred live runs to the Release Gate; no provider call has been made (zero spend) | **BLOCKED-ENVIRONMENT** (needs API key + network) |
| Trace export | OTel SDK wired (default off), Tempo endpoint provided under `--profile observability`; actual export run **BLOCKED-ENVIRONMENT** | PARTIAL |
| Compose health-check results | Compose `--wait` gates on `postgres`/`api`/`web` healthchecks; recorded for the 2026-10-26 baseline only; final-commit capture **BLOCKED-ENVIRONMENT** | PARTIAL |
| Invariant re-checks: no cross-region leakage, 100% denial of unauthorized tool calls, 100% rejection of stale-lease writes, published-config immutability, single concurrent claimant | Integration assertions and database triggers (`0004`, `0007`) pass in the full isolated suite; deployment capture not run | PARTIAL — isolated suite passes |
| 10 core business-analysis cases pass deterministic metric checks | `tests/integration/test_evaluations.py` — BA-01–10 all pass through the public API and worker in Fake mode | DONE — isolated Fake-mode run |

## Remaining deployment evidence

The isolated test run did not execute application Compose, touch `agentforge-gate`, or call a live provider. The optional Compose check below uses a distinct project and host ports, creating its own PostgreSQL volume and seeded database; it has not been run.

```sh
uv run pytest tests/integration -q            # full integration tier
AGENTFORGE_API_PORT=18107 AGENTFORGE_WEB_PORT=18117 docker compose -p agentforge-p05a-review up --build -d --wait --wait-timeout 180
docker compose -p agentforge-p05a-review ps
curl -fsS http://127.0.0.1:18107/health/ready   # schema version 13
# live evidence (requires AGENTFORGE_DEEPSEEK_API_KEY and explicit enablement):
#   AGENTFORGE_LIVE_EVALUATIONS_ENABLED=true, then POST /api/v1/evaluations/runs
#   with model_mode=live, live_call_budget=... — at least 3 runs, retain traces
```

Record each result as a new dated file in `docs/verification/` and update this
index. "Design complete" or "code written" is not acceptance; only recorded
runtime evidence counts.
