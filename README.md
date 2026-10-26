# AgentForge P0 walking skeleton

## Local Compose startup

The local defaults are for development only. Copy `.env.example` to `.env` and replace its passwords before exposing the services beyond your machine. Start the PostgreSQL database, one-shot bootstrap/migration service, and non-root API with:

```sh
docker compose up --build
```

The API is available at `http://localhost:8000`. `/health/live` reports process liveness; `/health/ready` returns success only when PostgreSQL is reachable and the applied migration version matches this API. The PostgreSQL healthcheck probes TCP so the temporary Unix-socket-only initialization server cannot satisfy it.

The PostgreSQL init script creates `agentforge_migrator` as a non-superuser with `CREATEROLE`, assigns database ownership, and creates the separate `agentforge_registry_cli` login using `AGENTFORGE_REGISTRY_DB_PASSWORD`. This is one-time cluster bootstrap, executed only when the PostgreSQL data volume is first initialized. Migration 0001 creates the API, worker, business read-only, and evaluation roles. The one-shot migration job connects as `agentforge_migrator`; the API connects as `agentforge_api` and never runs migrations. If you change a password while keeping an existing database volume, update the corresponding role password in PostgreSQL to match.

Registry operations use the `agentforge-registry` command with `AGENTFORGE_REGISTRY_DSN` pointing to a connection as `agentforge_registry_cli`. That login has schema usage and DML on the four Registry tables; publication and retirement rules are enforced by database triggers. Published Agent versions retain their frozen model configuration even if the source model profile changes.

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
