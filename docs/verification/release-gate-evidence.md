# P0 Release Gate — Evidence Index

Status date: 2026-10-27. Branch under review: `integration/p0` (single
integration branch; no remote is configured in this environment, so no hosted
PR exists).

Each Release Gate item (from
`.scratch/p0-platform/issues/16-release-gate.md`) is mapped to its evidence
source. Items marked **BLOCKED-ENVIRONMENT** require a Docker-capable host
and/or live model credentials; this sandbox cannot run Docker, PostgreSQL, or
outbound model calls, and no evidence is claimed for them.

| Release Gate item | Evidence source | Status |
| --- | --- | --- |
| `docker compose up` one-shot cold start, health checks green, repeatable | `docs/verification/2026-10-26-compose-baseline.md` (postgres+migrate+api at migrations 0001–0005, commit `3a559e7`); seed service and `--profile observability` added since, compose YAML validated statically | PARTIAL — re-run at final commit **BLOCKED-ENVIRONMENT** |
| Migrations + seed repeatable everywhere, `data_snapshot_id` stable | Offline suite asserts generator determinism (`tests/unit`, snapshot id `e38d3afb…b5b8`); loader re-run is a documented no-op (`business_loader.py`); repeat-run evidence at final commit **BLOCKED-ENVIRONMENT** | PARTIAL |
| API documentation (endpoints, error codes, SSE semantics) | `docs/api.md` — written from source at `integration/p0` | DONE |
| Operations runbook | `docs/runbook.md` | DONE |
| Test run reports | Offline tier `tests/unit`: **181 passed** at `integration/p0` `6344216` (2026-10-27). Integration tier `tests/integration`: last recorded full-suite run 376+ passed at ticket-07 merge (2026-10-26, commit `88c9add`); runs after that recorded per-ticket (413 passed at ticket-08 handoff). Full-suite rerun at final commit **BLOCKED-ENVIRONMENT** | PARTIAL |
| Migration version record | `docs/verification/2026-10-26-compose-baseline.md` (ledger 1–5 recorded); ledger now extends to `SCHEMA_VERSION = 12`; final ledger capture **BLOCKED-ENVIRONMENT** | PARTIAL |
| API request/response records | Reproducible via the integration suite (HTTP seam) and the compose baseline HTTP table; not separately archived | PARTIAL |
| Worker crash-recovery records | Fault-injection tests cover the eight crash windows (`tests/integration`, ticket 09 work merged at `851fcfd`); execution records at final commit **BLOCKED-ENVIRONMENT** | PARTIAL |
| Tool permission-denial records | Denied `tool.invocation` audit rows asserted in integration tests (unbound tool, invalid arguments, region denial) | PARTIAL — capture at final commit **BLOCKED-ENVIRONMENT** |
| ≥3 live DeepSeek end-to-end runs, fully recorded | Ticket 13 deferred live runs to the Release Gate; no provider call has been made (zero spend) | **BLOCKED-ENVIRONMENT** (needs API key + network) |
| Trace export | OTel SDK wired (default off), Tempo endpoint provided under `--profile observability`; actual export run **BLOCKED-ENVIRONMENT** | PARTIAL |
| Compose health-check results | Compose `--wait` gates on `postgres`/`api`/`web` healthchecks; recorded for the 2026-10-26 baseline only; final-commit capture **BLOCKED-ENVIRONMENT** | PARTIAL |
| Invariant re-checks: no cross-region leakage, 100% denial of unauthorized tool calls, 100% rejection of stale-lease writes, published-config immutability, single concurrent claimant | Enforced and asserted by integration tests (BA-08, HS-03/04/06–09, RT-04/07, HS-12) and by database triggers (`0004`, `0007`); summary pass/fail at final commit **BLOCKED-ENVIRONMENT** | PARTIAL |
| 10 core business-analysis cases pass deterministic metric checks | `tests/integration/test_evaluations.py` (BA-01–10 dataset seeded by migration `0011`, fixed UUIDs repaired at `6344216`); offline tier green; integration execution **BLOCKED-ENVIRONMENT** | PARTIAL |

## What must be executed on a Docker-capable host

```sh
uv run pytest tests/integration -q            # full integration tier
AGENTFORGE_API_PORT=18107 docker compose -p agentforge-gate up --build -d --wait --wait-timeout 180
docker compose -p agentforge-gate ps          # health status capture
curl -s http://127.0.0.1:18107/health/ready   # schema version 12
# live evidence (requires AGENTFORGE_DEEPSEEK_API_KEY and explicit enablement):
#   AGENTFORGE_LIVE_EVALUATIONS_ENABLED=true, then POST /api/v1/evaluations/runs
#   with model_mode=live, live_call_budget=... — at least 3 runs, retain traces
```

Record each result as a new dated file in `docs/verification/` and update this
index. "Design complete" or "code written" is not acceptance; only recorded
runtime evidence counts.
