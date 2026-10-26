# Compose startup verification — 2026-10-26

Verified source commit: `3a559e7` on `integration/p0`.

The actual app was built and started using Compose project `agentforge-p0-verify-261026`, with API port `18107` and a newly created project-specific PostgreSQL volume. Existing Compose projects were inspected before startup and left unchanged.

```sh
AGENTFORGE_API_PORT=18107 docker compose -p agentforge-p0-verify-261026 up --build -d --wait --wait-timeout 180
```

The command exited successfully. PostgreSQL and API container health were `healthy`; the migration container exited with code `0`.

Migration output:

```text
applied: 0001, 0002, 0003, 0004, 0005
```

Re-running the migration command against the same volume returned `up to date` with exit code `0`.

HTTP interaction with the running API:

| Request | Status | Body |
| --- | --- | --- |
| `GET http://127.0.0.1:18107/health/live` | `200` | `{"status":"alive"}` |
| `GET http://127.0.0.1:18107/health/ready` | `200` | `{"status":"ready","expected_schema_version":5,"applied_schema_version":5}` |

Database inspection confirmed the ledger contained versions `1, 2, 3, 4, 5` with the expected filenames. The API container reported `uid=10001 gid=10001 groups=10001`. The `agentforge_api`, `agentforge_worker`, and `agentforge_registry_cli` roles each reported `rolsuper=false` and `rolcreaterole=false`.

The isolated project services were stopped after verification. Its containers and volume were retained.

This verifies the PostgreSQL, migration, and API startup path at the recorded commit. Task API migration0006, the Registry policy repair, Worker, seed service, Web, and the final P0 release gate require verification at their integrated commits.
