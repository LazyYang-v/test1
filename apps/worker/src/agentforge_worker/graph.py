"""LangGraph linear four-node analysis graph with replay-memoized steps."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from agentforge_harness.gateway import ToolContext, ToolGateway
from agentforge_harness.models import (
    ModelOutputError,
    ModelRequest,
    ModelResponse,
    generate_json,
)
from agentforge_harness.report import (
    Evidence,
    ReportValidationError,
    build_report,
    validate_report,
)
from agentforge_harness.schemas import COMPOSE_SCHEMA, PLAN_SCHEMA
from agentforge_runtime.contracts import AttemptLease
from agentforge_runtime.errors import LeaseLostError, TaskNotRunningError
from agentforge_core.observability import log_event, span

logger = logging.getLogger("agentforge.worker")
from agentforge_runtime.store import RuntimeStore
from agentforge_worker.config import WorkerSettings


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class AnalysisState(TypedDict, total=False):
    plan: dict[str, Any]
    tool_result: dict[str, Any]
    evidence_id: str
    evidence_ids: list[str]
    compose: dict[str, Any]
    report: dict[str, Any]


class UnsupportedGraphError(RuntimeError):
    code = "GRAPH_UNSUPPORTED"


class RuntimeModelRecorder:
    """Persists each physical model call, including failed validation attempts."""

    def __init__(
        self,
        *,
        pool,
        lease: AttemptLease,
        agent_version_id: str,
        provider: str,
        model_name: str,
    ) -> None:
        self._pool = pool
        self._lease = lease
        self._agent_version_id = agent_version_id
        self._provider = provider
        self._model_name = model_name
        self._trace_id = lease.trace_id

    async def record_started(self, invocation_key: str, request: ModelRequest) -> None:
        with span(
            "model.invocation",
            trace_id=self._lease.trace_id,
            task_id=self._lease.task_id,
            attempt_id=self._lease.attempt_id,
        ):
            await self._record_started(invocation_key, request)

    async def _record_started(
        self, invocation_key: str, request: ModelRequest
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO harness.model_invocations
                    (id, task_id, attempt_id, invocation_key, agent_version_id,
                     provider, model_name, context_hash, status, started_at, trace_id)
                VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid, $6, $7, $8,
                        'executing', now(), $9)
                ON CONFLICT (task_id, invocation_key) DO NOTHING
                """,
                str(uuid.uuid4()),
                self._lease.task_id,
                self._lease.attempt_id,
                invocation_key,
                self._agent_version_id,
                self._provider,
                self._model_name,
                request.context_hash or _hash(request.messages),
                self._lease.trace_id,
            )

    async def record_finished(
        self,
        invocation_key: str,
        response: ModelResponse | None,
        error: BaseException | None,
    ) -> None:
        error_code = None
        if error is not None:
            error_code = getattr(error, "code", type(error).__name__)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE harness.model_invocations
                SET status = $4, input_tokens = $5, output_tokens = $6,
                    estimated_cost = $7, error_code = $8, finished_at = now(),
                    trace_id = $9
                WHERE task_id = $1::uuid AND invocation_key = $2
                  AND attempt_id = $3::uuid
                """,
                self._lease.task_id,
                invocation_key,
                self._lease.attempt_id,
                "failed" if error is not None else "succeeded",
                0 if response is None else response.input_tokens,
                0 if response is None else response.output_tokens,
                Decimal("0") if response is None else response.estimated_cost,
                error_code,
                self._trace_id,
            )


class AnalysisGraph:
    """Builds the real LangGraph graph for one attempt.

    The in-process MemorySaver is never a recovery source; confirmed
    ``step_executions`` rows are consulted before every node body.
    """

    def __init__(
        self,
        store: RuntimeStore,
        model,
        gateway: ToolGateway,
        settings: WorkerSettings,
    ) -> None:
        self._store = store
        self._model = model
        self._gateway = gateway
        self._settings = settings

    async def run(self, lease: AttemptLease) -> dict[str, Any]:
        task = await self._store.load_task(lease.task_id)
        agent = await self._store.load_agent(task["agent_version_id"])
        if agent["graph_key"] != "metrics-v1":
            raise UnsupportedGraphError(
                f"unsupported graph_key {agent['graph_key']!r}"
            )
        recorder = RuntimeModelRecorder(
            pool=self._store.pool,
            lease=lease,
            agent_version_id=task["agent_version_id"],
            provider=agent["provider"],
            model_name=agent["model_name"],
        )
        model_timeout_seconds = self._model_timeout_seconds(agent)
        max_output_tokens = agent.get("max_output_tokens")

        async def plan_node(state: AnalysisState) -> dict[str, Any]:
            async def work() -> dict[str, Any]:
                request = ModelRequest(
                    messages=self._plan_messages(task),
                    schema=PLAN_SCHEMA,
                    timeout_seconds=model_timeout_seconds,
                    max_output_tokens=max_output_tokens,
                    metadata={
                        "step_key": "plan_metrics:0",
                        "invocation_key_prefix": (
                            f"plan_metrics:0:model:{lease.attempt_no}"
                        ),
                        "task_input": task["input_json"],
                    },
                    context_hash=_hash({
                        "input": task["input_json"],
                        "schema": PLAN_SCHEMA,
                    }),
                )
                content, _ = await generate_json(
                    self._model,
                    request,
                    PLAN_SCHEMA,
                    max_repairs=self._settings.max_model_repairs,
                    recorder=recorder,
                )
                return content

            return {
                "plan": await self._step(
                    lease,
                    step_key="plan_metrics:0",
                    graph_node_key="plan_metrics",
                    input_hash=_hash(task["input_json"]),
                    work=work,
                )
            }

        async def execute_node(state: AnalysisState) -> dict[str, Any]:
            plan = state["plan"]

            async def work() -> dict[str, Any]:
                context = ToolContext(
                    task_id=task["task_id"],
                    attempt_id=lease.attempt_id,
                    agent_version_id=task["agent_version_id"],
                    principal_id=task["owner_user_id"],
                    region_scope=tuple(task["region_scope_json"]),
                    lease_token=lease.lease_token,
                    fencing_epoch=lease.fencing_epoch,
                    trace_id=task["trace_id"],
                    step_key="execute_metrics:0",
                    task_snapshot_id=task["data_snapshot_id"],
                )
                with span(
                    "tool.invocation",
                    trace_id=context.trace_id,
                    task_id=context.task_id,
                    attempt_id=context.attempt_id,
                ):
                    accepted = await self._gateway.invoke(
                        context, "metrics.query", plan
                    )
                return {
                    "tool_result": accepted["result"],
                    "evidence_id": accepted["evidence_id"],
                }

            result = await self._step(
                lease,
                step_key="execute_metrics:0",
                graph_node_key="execute_metrics",
                input_hash=_hash(plan),
                work=work,
            )
            return {
                "tool_result": result["tool_result"],
                "evidence_id": result["evidence_id"],
                "evidence_ids": [result["evidence_id"]],
            }

        async def compose_node(state: AnalysisState) -> dict[str, Any]:
            evidences = await self._store.load_evidences(task["task_id"])
            summaries = [self._evidence_summary(evidence) for evidence in evidences]
            evidence_ids = [evidence["evidence_id"] for evidence in evidences]

            async def work() -> dict[str, Any]:
                request = ModelRequest(
                    messages=self._compose_messages(task, summaries),
                    schema=COMPOSE_SCHEMA,
                    timeout_seconds=model_timeout_seconds,
                    max_output_tokens=max_output_tokens,
                    metadata={
                        "step_key": "compose_analysis:0",
                        "invocation_key_prefix": (
                            f"compose_analysis:0:model:{lease.attempt_no}"
                        ),
                        "evidence_ids": evidence_ids,
                        "task_input": task["input_json"],
                    },
                    context_hash=_hash({
                        "input": task["input_json"],
                        "evidence": summaries,
                        "schema": COMPOSE_SCHEMA,
                    }),
                )
                content, _ = await generate_json(
                    self._model,
                    request,
                    COMPOSE_SCHEMA,
                    max_repairs=self._settings.max_model_repairs,
                    recorder=recorder,
                )
                return content

            return {
                "compose": await self._step(
                    lease,
                    step_key="compose_analysis:0",
                    graph_node_key="compose_analysis",
                    input_hash=_hash({"evidence": summaries}),
                    work=work,
                )
            }

        async def validate_node(state: AnalysisState) -> dict[str, Any]:
            compose = state["compose"]

            async def work() -> dict[str, Any]:
                evidences = [
                    self._to_evidence(evidence)
                    for evidence in await self._store.load_evidences(task["task_id"])
                ]
                report = build_report(compose, evidences)
                try:
                    return validate_report(
                        report,
                        task_id=task["task_id"],
                        data_snapshot_id=task["data_snapshot_id"],
                        evidences=evidences,
                    )
                except ReportValidationError as error:
                    if (
                        error.code == "REPORT_EVIDENCE_MISSING"
                        and error.evidence_id is not None
                    ):
                        owner = await self._store.evidence_owner(error.evidence_id)
                        if owner is not None and owner != task["task_id"]:
                            raise ReportValidationError(
                                "REPORT_EVIDENCE_FOREIGN",
                                "evidence belongs to another task",
                            ) from error
                    raise

            return {
                "report": await self._step(
                    lease,
                    step_key="validate_report:0",
                    graph_node_key="validate_report",
                    input_hash=_hash(compose),
                    work=work,
                )
            }

        builder = StateGraph(AnalysisState)
        builder.add_node("plan_metrics", plan_node)
        builder.add_node("execute_metrics", execute_node)
        builder.add_node("compose_analysis", compose_node)
        builder.add_node("validate_report", validate_node)
        builder.add_edge(START, "plan_metrics")
        builder.add_edge("plan_metrics", "execute_metrics")
        builder.add_edge("execute_metrics", "compose_analysis")
        builder.add_edge("compose_analysis", "validate_report")
        builder.add_edge("validate_report", END)
        graph = builder.compile(checkpointer=InMemorySaver())

        final_state = await graph.ainvoke(
            AnalysisState(),
            config={"configurable": {"thread_id": lease.attempt_id}},
        )
        return final_state["report"]

    async def _step(
        self,
        lease: AttemptLease,
        *,
        step_key: str,
        graph_node_key: str,
        input_hash: str,
        work,
    ) -> dict[str, Any]:
        confirmed = await self._store.get_confirmed_step(lease.task_id, step_key)
        if confirmed is not None and confirmed.result is not None:
            return confirmed.result
        try:
            result = await work()
        except BaseException as error:
            try:
                await self._store.fail_step(
                    lease,
                    step_key=step_key,
                    graph_node_key=graph_node_key,
                    input_hash=input_hash,
                    error_code=self._error_code(error),
                    error_detail=str(error),
                )
            except (LeaseLostError, TaskNotRunningError):
                pass
            raise
        await self._store.confirm_step(
            lease,
            step_key=step_key,
            graph_node_key=graph_node_key,
            input_hash=input_hash,
            result=result,
        )
        return result

    def _model_timeout_seconds(self, agent: dict[str, Any]) -> float:
        snapshot_timeout = agent.get("timeout_ms")
        if (
            isinstance(snapshot_timeout, int)
            and not isinstance(snapshot_timeout, bool)
            and snapshot_timeout > 0
        ):
            return min(
                snapshot_timeout / 1000.0,
                self._settings.model_timeout_seconds,
            )
        return self._settings.model_timeout_seconds

    @staticmethod
    def _error_code(error: BaseException) -> str:
        if isinstance(error, ModelOutputError):
            return error.code
        if isinstance(error, ReportValidationError):
            return error.code
        return getattr(error, "code", "INTERNAL_ERROR")

    @staticmethod
    def _plan_messages(task: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are the metrics planner for an enterprise analysis agent. "
                    "Return only json matching this schema: "
                    + _canonical(PLAN_SCHEMA)
                    + ". The task input is: "
                    + _canonical(task["input_json"])
                    + ". Never write SQL."
                ),
            },
            {"role": "user", "content": task["input_json"]["question"]},
        ]

    @staticmethod
    def _compose_messages(
        task: dict[str, Any], evidence_summaries: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You compose an analysis from platform-verified metrics. Return "
                    "only json matching this schema: "
                    + _canonical(COMPOSE_SCHEMA)
                    + ". Every finding must cite at least one of these evidence "
                    "ids: "
                    + _canonical([item["evidence_id"] for item in evidence_summaries])
                    + ". Mark every hypothesis as unverified. Never invent numbers."
                ),
            },
            {
                "role": "user",
                "content": (
                    task["input_json"]["question"]
                    + " Verified metrics: "
                    + _canonical(evidence_summaries)
                ),
            },
        ]

    @staticmethod
    def _evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
        query_plan = evidence.get("query_plan_json") or {}
        return {
            "evidence_id": evidence["evidence_id"],
            "metric": evidence["metric_key"],
            "metric_version": evidence["metric_version"],
            "unit": evidence["unit"],
            "current_value": f"{evidence['current_value']:.2f}",
            "previous_value": f"{evidence['previous_value']:.2f}",
            "absolute_change": f"{evidence['absolute_change']:.2f}",
            "change_rate": (
                None
                if evidence["change_rate"] is None
                else f"{evidence['change_rate']:.4f}"
            ),
            "data_snapshot_id": evidence["data_snapshot_id"],
            "annotations": list(query_plan.get("annotations") or []),
            "dimension_results": AnalysisGraph._dimension_result_summary(
                query_plan.get("dimension_results"),
                query_plan.get("dimensions"),
            ),
        }

    @staticmethod
    def _dimension_result_summary(
        value: Any, allowed_dimensions: Any
    ) -> list[dict[str, Any]]:
        """Pass bounded, normalized breakdowns to compose without raw query data."""
        if not isinstance(value, list):
            return []
        registered = frozenset(
            dimension
            for dimension in (allowed_dimensions or [])
            if isinstance(dimension, str)
        )
        summary: list[dict[str, Any]] = []
        for result in value[:100]:
            if not isinstance(result, dict):
                continue
            raw_dimensions = result.get("dimensions")
            if not isinstance(raw_dimensions, dict):
                continue
            dimensions = {
                key: dimension_value[:128]
                for key, dimension_value in raw_dimensions.items()
                if key in registered
                and isinstance(dimension_value, str)
                and len(dimension_value) <= 128
            }
            row: dict[str, Any] = {"dimensions": dimensions}
            for key in (
                "current_value", "previous_value", "absolute_change", "change_rate"
            ):
                metric_value = result.get(key)
                if metric_value is None or isinstance(metric_value, str):
                    row[key] = metric_value
            row_count = result.get("row_count")
            if isinstance(row_count, int) and not isinstance(row_count, bool):
                row["row_count"] = row_count
            annotations = result.get("annotations")
            if isinstance(annotations, list):
                row["annotations"] = [
                    item[:64] for item in annotations[:8] if isinstance(item, str)
                ]
            summary.append(row)
        return summary

    @staticmethod
    def _to_evidence(evidence: dict[str, Any]) -> Evidence:
        query_plan = evidence.get("query_plan_json") or {}
        annotations = tuple(
            str(annotation)
            for annotation in (query_plan.get("annotations") or [])
            if isinstance(annotation, str)
        )
        return Evidence(
            evidence_id=evidence["evidence_id"],
            task_id=evidence["task_id"],
            data_snapshot_id=evidence["data_snapshot_id"],
            metric_key=evidence["metric_key"],
            metric_version=evidence["metric_version"],
            unit=evidence["unit"],
            current_value=Decimal(evidence["current_value"]),
            previous_value=Decimal(evidence["previous_value"]),
            absolute_change=Decimal(evidence["absolute_change"]),
            change_rate=(
                None
                if evidence["change_rate"] is None
                else Decimal(evidence["change_rate"])
            ),
            row_count=evidence["row_count"],
            annotations=annotations,
        )
