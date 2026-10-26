"""Worker loop: claim, heartbeat, run the graph, and commit fenced terminal state.

Cancellation is cooperative. A watcher polls the Runtime state; when a cancel
request (or deadline) is observed the graph is cancelled, no new model/tool
call is issued, and the owning worker performs the fenced terminal transition.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import Any

from agentforge_harness.gateway import ToolGateway
from agentforge_runtime.contracts import AttemptLease
from agentforge_runtime.errors import LeaseLostError, TaskNotRunningError
from agentforge_runtime.store import RuntimeStore
from agentforge_core.observability import log_event

from agentforge_worker.config import WorkerSettings
from agentforge_worker.errors import classify_error_code, is_retryable
from agentforge_worker.executors import MetricsQueryExecutor
from agentforge_worker.gateway_backend import PostgresToolGatewayBackend
from agentforge_worker.graph import AnalysisGraph

logger = logging.getLogger("agentforge.worker")


def classify_error(error: BaseException) -> str:
    """Backwards-compatible name for the frozen classification table."""
    return classify_error_code(error)


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

    async def reconcile_once(self) -> dict[str, int]:
        """Run the state-machine sweep; failures must not stop claiming."""
        try:
            return await self.store.reconcile()
        except Exception:  # pragma: no cover - defensive loop guard
            log_event(
                logger,
                logging.ERROR,
                "reconciliation.failed",
                worker_id=self.settings.worker_id,
                error_code="RECONCILIATION_FAILED",
            )
            return {}

    async def run_once(self) -> bool:
        # Materialize queued cancellations, expired deadlines, and lost leases
        # before claiming so a cancelled Task is never started.
        await self.reconcile_once()
        lease = await self.store.claim()
        if lease is None:
            return False
        await self.run_attempt(lease)
        return True

    async def run_forever(self) -> None:
        log_event(
            logger,
            logging.INFO,
            "worker.started",
            worker_id=self.settings.worker_id,
        )
        while True:
            claimed = await self.run_once()
            if not claimed:
                await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_attempt(self, lease: AttemptLease) -> None:
        stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_failed = asyncio.Event()
        cancel_seen = asyncio.Event()
        deadline_seen = asyncio.Event()

        async def heartbeat_loop() -> None:
            while not stop.is_set():
                try:
                    alive = await self.store.heartbeat(lease)
                except Exception:
                    log_event(
                        logger,
                        logging.ERROR,
                        "heartbeat.failed",
                        trace_id=lease.trace_id,
                        task_id=lease.task_id,
                        attempt_id=lease.attempt_id,
                        error_code="HEARTBEAT_UNCERTAIN",
                    )
                    # A transient DB error says nothing about ownership. Stop
                    # graph work, but leave the Attempt row for expiry and
                    # scanner recovery to decide.
                    heartbeat_failed.set()
                    return
                if not alive:
                    lease_lost.set()
                    return
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=self.settings.heartbeat_interval_seconds
                    )
                except TimeoutError:
                    continue

        async def watch_loop() -> None:
            """Detect cancellation/deadline/lease loss while the graph runs."""
            while not stop.is_set():
                try:
                    reason = await self.store.stop_reason(lease)
                except Exception:
                    log_event(
                        logger,
                        logging.ERROR,
                        "cancellation.watch_failed",
                        trace_id=lease.trace_id,
                        task_id=lease.task_id,
                        attempt_id=lease.attempt_id,
                        error_code="CANCELLATION_WATCH_FAILED",
                    )
                    reason = None
                if reason == "CANCELLED":
                    cancel_seen.set()
                    return
                if reason == "DEADLINE_EXCEEDED":
                    deadline_seen.set()
                    return
                if reason == "LEASE_LOST":
                    lease_lost.set()
                    return
                try:
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=self.settings.cancel_poll_interval_seconds,
                    )
                except TimeoutError:
                    continue

        heartbeat = asyncio.create_task(heartbeat_loop())
        watcher = asyncio.create_task(watch_loop())
        graph_task = asyncio.create_task(self.graph.run(lease))
        lease_lost_waiter = asyncio.create_task(lease_lost.wait())
        heartbeat_failed_waiter = asyncio.create_task(heartbeat_failed.wait())
        cancel_waiter = asyncio.create_task(cancel_seen.wait())
        deadline_waiter = asyncio.create_task(deadline_seen.wait())
        try:
            await asyncio.wait(
                {
                    graph_task,
                    lease_lost_waiter,
                    heartbeat_failed_waiter,
                    cancel_waiter,
                    deadline_waiter,
                },
                return_when=asyncio.FIRST_COMPLETED,
            )

            if heartbeat_failed.is_set():
                await self._stop_graph(graph_task)
                log_event(
                    logger,
                    logging.WARNING,
                    "heartbeat.uncertain_attempt_retained",
                    trace_id=lease.trace_id,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    error_code="HEARTBEAT_UNCERTAIN",
                )
                return

            if lease_lost.is_set():
                await self._stop_graph(graph_task)
                await self.store.abandon_attempt(lease)
                log_event(
                    logger,
                    logging.WARNING,
                    "lease.lost",
                    trace_id=lease.trace_id,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    error_code="LEASE_LOST",
                )
                return

            if deadline_seen.is_set():
                await self._stop_graph(graph_task)
                await self._expire(lease)
                return

            if cancel_seen.is_set():
                await self._stop_graph(graph_task)
                await self._cancel(lease)
                return

            try:
                report = graph_task.result()
            except asyncio.CancelledError:
                raise
            except LeaseLostError:
                await self.store.abandon_attempt(lease)
                return
            except TaskNotRunningError as error:
                await self._handle_refusal(lease, error)
                return
            except BaseException as error:
                await self._handle_failure(lease, error)
                return

            try:
                await self.store.complete_task(lease, report)
            except LeaseLostError:
                await self.store.abandon_attempt(lease)
            except TaskNotRunningError as error:
                # Preserve the Runtime cancellation/deadline transition and log
                # the terminal race with the Task's stable trace identifier.
                log_event(
                    logger,
                    logging.WARNING,
                    "task.completion_skipped",
                    trace_id=lease.trace_id,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    error_code=error.code,
                )
                await self._handle_refusal(lease, error)
        finally:
            stop.set()
            for task in (
                lease_lost_waiter,
                heartbeat_failed_waiter,
                cancel_waiter,
                deadline_waiter,
                watcher,
                heartbeat,
            ):
                task.cancel()
            if not graph_task.done():
                graph_task.cancel()
            await asyncio.gather(
                heartbeat,
                watcher,
                lease_lost_waiter,
                heartbeat_failed_waiter,
                cancel_waiter,
                deadline_waiter,
                graph_task,
                return_exceptions=True,
            )

    @staticmethod
    async def _stop_graph(graph_task: asyncio.Task) -> None:
        if not graph_task.done():
            graph_task.cancel()
        with contextlib.suppress(BaseException):
            await graph_task

    async def _cancel(self, lease: AttemptLease) -> None:
        try:
            await self.store.mark_cancelling(lease)
        except LeaseLostError:
            await self.store.abandon_attempt(lease)
            return
        try:
            await self.store.finalize_cancellation(lease)
        except LeaseLostError:
            await self.store.abandon_attempt(lease)

    async def _expire(self, lease: AttemptLease) -> None:
        try:
            await self.store.expire_task(lease)
        except LeaseLostError:
            await self.store.abandon_attempt(lease)

    async def _handle_refusal(
        self, lease: AttemptLease, error: TaskNotRunningError
    ) -> None:
        if error.code == "CANCELLED":
            await self._cancel(lease)
        elif error.code == "DEADLINE_EXCEEDED":
            await self._expire(lease)
        else:
            await self.store.abandon_attempt(lease, error_code=error.code)

    def _retry_delay(self) -> float:
        return self.settings.retry_base_delay_seconds + random.uniform(
            0, self.settings.retry_jitter_seconds
        )

    async def _handle_failure(
        self, lease: AttemptLease, error: BaseException
    ) -> None:
        code = classify_error_code(error)
        log_event(
            logger,
            logging.ERROR,
            "attempt.failed",
            trace_id=lease.trace_id,
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            error_code=code,
            retryable=is_retryable(code),
        )
        if isinstance(error, LeaseLostError):
            await self.store.abandon_attempt(lease, error_code=code)
            return
        if isinstance(error, TaskNotRunningError):
            await self._handle_refusal(lease, error)
            return
        try:
            if is_retryable(code):
                await self.store.retry_task(
                    lease,
                    code,
                    str(error),
                    delay_seconds=self._retry_delay(),
                )
                log_event(
                    logger,
                    logging.INFO,
                    "attempt.retry_handled",
                    trace_id=lease.trace_id,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    error_code=code,
                )
            else:
                await self.store.fail_task(lease, code, str(error))
        except LeaseLostError:
            await self.store.abandon_attempt(lease, error_code=code)
        except TaskNotRunningError as error:
            # Cancellation/deadline/terminal state owns the Task.
            await self._handle_refusal(lease, error)
