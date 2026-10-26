"""Worker loop: claim, heartbeat, run the graph, and commit fenced terminal state."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from agentforge_harness.gateway import ToolError, ToolGateway
from agentforge_harness.models import ModelOutputError, ModelTimeoutError
from agentforge_harness.report import ReportValidationError
from agentforge_runtime.contracts import AttemptLease
from agentforge_runtime.errors import LeaseLostError, TaskNotRunningError
from agentforge_runtime.store import RuntimeStore

from agentforge_worker.config import WorkerSettings
from agentforge_worker.executors import MetricsQueryExecutor
from agentforge_worker.gateway_backend import PostgresToolGatewayBackend
from agentforge_worker.graph import AnalysisGraph, UnsupportedGraphError

logger = logging.getLogger("agentforge.worker")


def classify_error(error: BaseException) -> str:
    if isinstance(error, (ModelOutputError, ModelTimeoutError)):
        return error.code
    if isinstance(error, (ReportValidationError, ToolError, UnsupportedGraphError)):
        return error.code
    if isinstance(error, TaskNotRunningError):
        return error.code
    if isinstance(error, LeaseLostError):
        return "LEASE_LOST"
    return "INTERNAL_ERROR"


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        store: RuntimeStore,
        model,
        *,
        graph: AnalysisGraph | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.model = model
        backend = PostgresToolGatewayBackend(store.pool, store)
        self.executor = MetricsQueryExecutor(
            settings.business_ro_db_dsn,
            statement_timeout_ms=settings.business_ro_statement_timeout_ms,
        )
        self.gateway = ToolGateway(
            backend, {"metrics.query": self.executor}
        )
        self.graph = graph or AnalysisGraph(store, model, self.gateway, settings)

    async def close(self) -> None:
        await self.executor.close()
        await self.store.close()

    async def run_once(self) -> bool:
        lease = await self.store.claim()
        if lease is None:
            return False
        await self.run_attempt(lease)
        return True

    async def run_forever(self) -> None:
        logger.info("worker started", extra={"worker_id": self.settings.worker_id})
        while True:
            claimed = await self.run_once()
            if not claimed:
                await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_attempt(self, lease: AttemptLease) -> None:
        stop = asyncio.Event()
        lease_lost = asyncio.Event()

        async def heartbeat_loop() -> None:
            while not stop.is_set():
                try:
                    alive = await self.store.heartbeat(lease)
                except Exception:
                    logger.exception(
                        "heartbeat failed",
                        extra={"task_id": lease.task_id, "attempt_id": lease.attempt_id},
                    )
                    alive = False
                if not alive:
                    lease_lost.set()
                    return
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=self.settings.heartbeat_interval_seconds
                    )
                except TimeoutError:
                    continue

        heartbeat = asyncio.create_task(heartbeat_loop())
        graph_task = asyncio.create_task(self.graph.run(lease))
        lost_task = asyncio.create_task(lease_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {graph_task, lost_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if lost_task in done and not graph_task.done():
                graph_task.cancel()
                with contextlib.suppress(BaseException):
                    await graph_task
                await self.store.abandon_attempt(lease)
                logger.warning(
                    "lease lost during execution",
                    extra={
                        "task_id": lease.task_id,
                        "attempt_id": lease.attempt_id,
                        "fencing_epoch": lease.fencing_epoch,
                    },
                )
                return

            try:
                report = graph_task.result()
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                await self._fail(lease, error)
                return

            try:
                await self.store.complete_task(lease, report)
            except LeaseLostError:
                await self.store.abandon_attempt(lease)
            except TaskNotRunningError as error:
                logger.warning(
                    "task left running before completion",
                    extra={"task_id": lease.task_id, "error_code": error.code},
                )
        finally:
            stop.set()
            lost_task.cancel()
            if not graph_task.done():
                graph_task.cancel()
            await asyncio.gather(heartbeat, lost_task, graph_task, return_exceptions=True)

    async def _fail(self, lease: AttemptLease, error: BaseException) -> None:
        code = classify_error(error)
        logger.info(
            "attempt failed",
            extra={
                "task_id": lease.task_id,
                "attempt_id": lease.attempt_id,
                "error_code": code,
            },
        )
        try:
            await self.store.fail_task(lease, code, str(error))
        except LeaseLostError:
            await self.store.abandon_attempt(lease, error_code=code)
        except TaskNotRunningError:
            # Cancellation/deadline/terminal state is owned by the runtime state
            # machine (tickets 08-09); this attempt cannot write a terminal state.
            return
