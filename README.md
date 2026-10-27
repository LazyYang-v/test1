# AgentForge P0 walking skeleton

## Local Compose startup

The local defaults are for development only. Copy `.env.example` to `.env` and replace its passwords before exposing the services beyond your machine. Start the PostgreSQL database, one-shot bootstrap/migration service, and non-root API with:

```sh
docker compose up --build
```

The API is available at `http://localhost:8000`; the React analysis workspace is available at `http://localhost:3000`. The web service proxies API requests to the API container under the same browser origin. `/health/live` reports process liveness; `/health/ready` returns success only when PostgreSQL is reachable and the applied migration version matches this API. The PostgreSQL healthcheck probes TCP so the temporary Unix-socket-only initialization server cannot satisfy it.

The frontend uses bearer-authenticated `fetch` for the durable event stream because native browser `EventSource` cannot set the API's `Authorization` header. It reconnects with `Last-Event-ID` and deduplicates events by task sequence. See [apps/web/README.md](apps/web/README.md) for the UI and authentication details.

## Task event streams

Subscribe to `GET /api/v1/tasks/{id}/events` with the task owner's bearer token. The response is an SSE stream; each event uses `task_event_seq` as its `id`, the persisted event type as its `event`, and compact JSON as its `data`. Send the last received sequence in `Last-Event-ID` when reconnecting to replay all later persisted events. Delivery is at least once across reconnects, so clients should deduplicate by event ID. Keep the latest parsed event ID as the resume cursor; heartbeats are SSE comments and have no ID.

The PostgreSQL init script creates `agentforge_migrator` as a non-superuser with `CREATEROLE`, assigns database ownership, and creates `agentforge_registry_cli` using `AGENTFORGE_REGISTRY_DB_PASSWORD`. Migration 0001 creates the API, worker, business read-only, and evaluation roles. The one-shot migration job connects as `agentforge_migrator`; the API connects as `agentforge_api` and never runs migrations. If you change a password while keeping an existing database volume, update the corresponding role password in PostgreSQL to match.

`agentforge-registry` reads Registry data using `agentforge_registry_cli`. Migration 0013 makes that role read-only; every write command uses the authenticated Registry API and requires `AGENTFORGE_API_URL` plus an active Operator's `AGENTFORGE_ACCESS_TOKEN`. Model `credential_ref` values must be references such as `secret://agentforge/provider`, never secret values. Published Agent versions retain their frozen model configuration if the source profile changes.

The first Operator is created with a short-lived local Bootstrap secret. Identity provisioning, recovery, and Registry CLI examples are in the [runbook](docs/runbook.md); API request and response contracts are in [docs/api.md](docs/api.md).

Migration filenames follow the order that completed slices were integrated. Versions 0001–0005 contain roles/schemas, identity, the business dataset, Registry, and snapshot integrity respectively. The design baseline's original migration table describes its planned grouping; the checked-in `migrations/versions` directory defines the executable order. New changes append the next contiguous version. Applied filenames and content checksums must stay unchanged.

To remove the local database and reinitialize from scratch, use `docker compose down -v`.

## Test tiers

Offline tests are deterministic and need no running database:

```sh
uv run pytest -m 'not integration'
```

Integration tests use a real ephemeral PostgreSQL 17 container through Docker. They apply the actual migration runner, verify schemas, roles, grants, idempotency, and API readiness, then drop their databases and container:

```sh
uv run pytest -m integration
```

Run both tiers with:

```sh
uv run pytest
```
