"""Process entry point for the AgentForge worker."""

from __future__ import annotations

import asyncio
import logging
import signal

import asyncpg
from agentforge_core.migrations import fetch_applied_version
from agentforge_core.schema_version import SCHEMA_VERSION

from agentforge_runtime.store import RuntimeStore

from agentforge_worker.config import WorkerSettings
from agentforge_worker.model_factory import SnapshotModelAdapterFactory
from agentforge_worker.worker import Worker


async def _require_current_schema(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        applied = await fetch_applied_version(conn)
    finally:
        await conn.close()
    if applied != SCHEMA_VERSION:
        raise RuntimeError(
            f"worker requires schema version {SCHEMA_VERSION}, database has {applied}"
        )


async def _run() -> None:
    settings = WorkerSettings()
    await _require_current_schema(settings.worker_db_dsn)
    pool = await asyncpg.create_pool(
        settings.worker_db_dsn,
        min_size=1,
        max_size=max(2, settings.max_concurrent_tasks + 1),
    )
    store = RuntimeStore(
        pool,
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
    )
    model_factory = SnapshotModelAdapterFactory(settings)
    worker = Worker(
        settings,
        store,
        model_factory,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    run = asyncio.create_task(worker.run_forever())
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({run, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        run.cancel()
        stop_task.cancel()
        await asyncio.gather(run, stop_task, return_exceptions=True)
        await worker.close()
        await model_factory.aclose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
