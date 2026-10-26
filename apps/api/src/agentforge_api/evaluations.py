"""Fixed Benchmark endpoints and service-owned runtime Task creation."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge_api.auth import ApiError, current_principal

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])
SVC_EVAL_EMAIL = "svc-eval@agentforge.internal"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class CreateEvaluationRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_key: str = Field(min_length=1, max_length=128)
    agent_key: str = Field(min_length=1, max_length=128)
    model_mode: Literal["fake", "live"] = "fake"
    live_call_budget: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def require_live_budget(self):
        if self.model_mode == "fake" and self.live_call_budget is not None:
            raise ValueError("fake evaluations do not accept a live call budget")
        if self.model_mode == "live" and self.live_call_budget is None:
            raise ValueError("live evaluations require a call budget")
        return self


@router.get("/datasets")
async def list_datasets(
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
):
    del principal
    async with request.app.state.connect() as conn:
        rows = await conn.fetch(
            """
            SELECT dataset.id::text AS dataset_id, dataset.dataset_key,
                   dataset.version, dataset.name, dataset.description,
                   dataset.content_hash,
                   dataset.data_snapshot_id::text AS data_snapshot_id, dataset.status,
                   count(case_row.id)::integer AS case_count
            FROM evaluation.datasets AS dataset
            LEFT JOIN evaluation.cases AS case_row ON case_row.dataset_id = dataset.id
            WHERE dataset.status = 'active'
            GROUP BY dataset.id
            ORDER BY dataset.dataset_key, dataset.version
            """
        )
    return {"items": [dict(row) for row in rows]}


@router.post("/runs", status_code=202)
async def create_run(
    body: CreateEvaluationRun,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: dict[str, Any] = Depends(current_principal),
):
    if idempotency_key is None or not idempotency_key.strip():
        raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key header is required")
    key = idempotency_key.strip()
    if len(key) > 255:
        raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key is too long")

    if body.model_mode == "live":
        if not request.app.state.settings.live_evaluations_enabled:
            raise ApiError(
                403,
                "LIVE_EVALUATIONS_DISABLED",
                "Live evaluations are disabled",
            )
        if body.live_call_budget is None or body.live_call_budget > request.app.state.settings.live_evaluation_call_budget_limit:
            raise ApiError(
                400,
                "INVALID_REQUEST",
                "Live evaluation budget is required and must not exceed the configured limit",
            )

    canonical_body = body.model_dump(mode="json")
    request_hash = _hash(canonical_body)
    run_id = str(uuid.uuid4())
    async with request.app.state.connect() as conn:
        async with conn.transaction():
            claimed = await conn.fetchval(
                """
                INSERT INTO runtime.idempotency_records
                    (id, actor_user_id, operation, idempotency_key, request_hash,
                     status, expires_at)
                VALUES ($1::uuid, $2::uuid, 'evaluation.run.create', $3, $4,
                        'processing', now() + interval '24 hours')
                ON CONFLICT (actor_user_id, operation, idempotency_key) DO UPDATE
                SET id = EXCLUDED.id, request_hash = EXCLUDED.request_hash,
                    status = 'processing', resource_id = NULL,
                    response_status = NULL, response_json = NULL,
                    expires_at = EXCLUDED.expires_at, updated_at = now()
                WHERE runtime.idempotency_records.expires_at <= now()
                RETURNING id::text
                """,
                str(uuid.uuid4()), principal["id"], key, request_hash,
            )
            if claimed is None:
                existing = await conn.fetchrow(
                    """
                    SELECT request_hash, status, response_status, response_json
                    FROM runtime.idempotency_records
                    WHERE actor_user_id = $1::uuid
                      AND operation = 'evaluation.run.create' AND idempotency_key = $2
                    FOR UPDATE
                    """,
                    principal["id"], key,
                )
                if existing is None or existing["status"] != "completed":
                    raise ApiError(503, "IDEMPOTENCY_RETRY", "Retry evaluation creation", retryable=True)
                if existing["request_hash"].strip() != request_hash:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for a different request")
                return JSONResponse(
                    status_code=existing["response_status"],
                    content=_json_value(existing["response_json"]),
                )

            dataset = await conn.fetchrow(
                """
                SELECT id::text AS dataset_id, dataset_key, version, data_snapshot_id
                FROM evaluation.datasets
                WHERE dataset_key = $1 AND status = 'active'
                ORDER BY version DESC LIMIT 1
                """,
                body.dataset_key,
            )
            if dataset is None:
                raise ApiError(404, "EVALUATION_DATASET_NOT_FOUND", "Evaluation dataset not found")
            agent = await conn.fetchrow(
                """
                SELECT id::text AS agent_version_id, version
                FROM registry.agent_versions
                WHERE agent_key = $1 AND status = 'published'
                ORDER BY published_at DESC, created_at DESC, version DESC LIMIT 1
                """,
                body.agent_key,
            )
            if agent is None:
                raise ApiError(400, "INVALID_REQUEST", "agent_key does not resolve to a published Agent")
            svc_eval = await conn.fetchrow(
                """
                SELECT id::text AS id, token_version
                FROM identity.users WHERE email = $1 AND status = 'active'
                """,
                SVC_EVAL_EMAIL,
            )
            if svc_eval is None:
                raise ApiError(503, "EVALUATION_SERVICE_UNAVAILABLE", "Evaluation service identity is unavailable", retryable=True)
            grants = await conn.fetch(
                """
                SELECT grant_row.region_code, grant_row.permission
                FROM identity.region_grants AS grant_row
                JOIN identity.users AS service_user ON service_user.id = grant_row.user_id
                WHERE grant_row.user_id = $1::uuid AND grant_row.revoked_at IS NULL
                  AND grant_row.permission IN ('read', 'admin')
                ORDER BY grant_row.region_code, grant_row.permission
                """,
                svc_eval["id"],
            )
            grant_regions = {grant["region_code"] for grant in grants}
            cases = await conn.fetch(
                """
                SELECT id::text AS case_id, case_key, input_json, expected_json,
                       assertions_json, data_snapshot_ref
                FROM evaluation.cases WHERE dataset_id = $1::uuid
                ORDER BY case_key
                """,
                dataset["dataset_id"],
            )
            if not cases:
                raise ApiError(409, "EVALUATION_DATASET_EMPTY", "Evaluation dataset has no cases")

            live_snapshot = await conn.fetchval(
                """
                SELECT data_snapshot_id FROM business.dataset_loads
                WHERE status = 'complete' ORDER BY completed_at DESC, data_snapshot_id LIMIT 1
                """
            )
            await conn.execute(
                """
                INSERT INTO evaluation.runs
                    (id, dataset_id, agent_version_id, status, model_mode, live_call_budget)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'queued', $4, $5)
                """,
                run_id,
                dataset["dataset_id"],
                agent["agent_version_id"],
                body.model_mode,
                body.live_call_budget,
            )

            task_ids: list[str] = []
            direct_failures: list[dict[str, Any]] = []
            for case in cases:
                input_data = _json_value(case["input_json"])
                case_snapshot = case["data_snapshot_ref"].strip()
                snapshot = dataset["data_snapshot_id"].strip()
                regions = sorted(set(input_data.get("region_codes", [])))
                failure_code = None
                if case_snapshot != snapshot or live_snapshot is None or live_snapshot.strip() != snapshot:
                    failure_code = "DATA_SNAPSHOT_MISMATCH"
                elif not regions or any(region not in grant_regions for region in regions):
                    failure_code = "REGION_ACCESS_DENIED"

                result_id = str(uuid.uuid4())
                if failure_code:
                    await conn.execute(
                        """
                        INSERT INTO evaluation.case_results
                            (id, eval_run_id, case_id, status, score_json, failure_codes, duration_ms)
                        VALUES ($1::uuid, $2::uuid, $3::uuid, 'failed', $4::jsonb, $5::text[], 0)
                        """,
                        result_id, run_id, case["case_id"],
                        _canonical({"expected": _json_value(case["expected_json"]), "actual": {}, "checks": {}}),
                        [failure_code],
                    )
                    direct_failures.append({"case_key": case["case_key"], "failure_code": failure_code})
                    continue

                task_id = str(uuid.uuid4())
                region_grants = [
                    {"region_code": grant["region_code"], "permission": grant["permission"]}
                    for grant in grants if grant["region_code"] in regions
                ]
                normalized_input = {
                    "question": input_data["question"],
                    "region_codes": regions,
                    "date_range": input_data["date_range"],
                    "compare_mode": input_data.get("compare_mode", "previous_period"),
                }
                task_input_hash = _hash(normalized_input)
                await conn.execute(
                    """
                    INSERT INTO runtime.tasks
                        (id, trace_id, owner_user_id, agent_version_id, task_type, status,
                         input_json, input_hash, region_scope_json, authorization_snapshot,
                         data_snapshot_id, max_attempts, deadline_at, eval_run_id)
                    VALUES ($1::uuid, $1::text, $2::uuid, $3::uuid, 'business_analysis', 'queued',
                            $4::jsonb, $5, $6::jsonb, $7::jsonb, $8, 1,
                            now() + interval '10 minutes', $9::uuid)
                    """,
                    task_id, svc_eval["id"], agent["agent_version_id"],
                    _canonical(normalized_input), task_input_hash,
                    _canonical(regions), _canonical(region_grants), snapshot, run_id,
                )
                await conn.execute(
                    """
                    INSERT INTO runtime.task_events
                        (task_id, task_event_seq, event_type, payload_json, trace_id)
                    VALUES ($1::uuid, 1, 'task.created', $2::jsonb, $3)
                    """,
                    task_id, _canonical({"status": "queued", "source": "evaluation"}),
                    request.state.request_id,
                )
                await conn.execute(
                    """
                    INSERT INTO evaluation.case_results
                        (id, eval_run_id, case_id, task_id, status, score_json, failure_codes, duration_ms)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'pending', '{}'::jsonb, ARRAY[]::text[], 0)
                    """,
                    result_id, run_id, case["case_id"], task_id,
                )
                await conn.execute(
                    """
                    INSERT INTO audit.audit_events
                        (actor_type, actor_id, action, resource_type, resource_id,
                         decision, metadata_json, trace_id)
                    VALUES ('service', $1::uuid, 'task.created', 'task', $2,
                            'allowed', $3::jsonb, $4)
                    """,
                    svc_eval["id"], task_id,
                    _canonical({"evaluation_run_id": run_id, "case_key": case["case_key"], "data_snapshot_id": snapshot}),
                    request.state.request_id,
                )
                task_ids.append(task_id)

            await conn.execute(
                """
                INSERT INTO audit.audit_events
                    (actor_type, actor_id, action, resource_type, resource_id,
                     decision, metadata_json, trace_id)
                VALUES ('user', $1::uuid, 'evaluation.run.started', 'evaluation_run', $2,
                        'allowed', $3::jsonb, $4)
                """,
                principal["id"], run_id,
                _canonical({"dataset_key": body.dataset_key, "agent_key": body.agent_key,
                            "model_mode": body.model_mode, "live_call_budget": body.live_call_budget,
                            "case_count": len(cases)}),
                request.state.request_id,
            )
            response = {
                "eval_run_id": run_id,
                "status": "queued",
                "dataset_key": body.dataset_key,
                "agent_version": agent["version"],
                "model_mode": body.model_mode,
                "case_count": len(cases),
                "task_count": len(task_ids),
                "direct_failures": direct_failures,
            }
            await conn.execute(
                """
                UPDATE runtime.idempotency_records
                SET status = 'completed', resource_id = NULL,
                    response_status = 202, response_json = $2::jsonb, updated_at = now()
                WHERE id = $1::uuid
                """,
                claimed, _canonical(response),
            )
    return JSONResponse(status_code=202, content=response)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
):
    del principal
    async with request.app.state.connect() as conn:
        run = await conn.fetchrow(
            """
            SELECT run.id::text AS eval_run_id, run.status, run.model_mode,
                   run.live_call_budget, run.live_calls_used, run.started_at,
                   run.finished_at, run.summary_json, dataset.dataset_key,
                   agent.agent_key, agent.version AS agent_version
            FROM evaluation.runs AS run
            JOIN evaluation.datasets AS dataset ON dataset.id = run.dataset_id
            JOIN registry.agent_versions AS agent ON agent.id = run.agent_version_id
            WHERE run.id = $1::uuid
            """,
            str(run_id),
        )
        if run is None:
            raise ApiError(404, "EVALUATION_RUN_NOT_FOUND", "Evaluation run not found")
        rows = await conn.fetch(
            """
            SELECT case_row.case_key, result.task_id::text AS task_id, result.status,
                   result.score_json, result.failure_codes, result.duration_ms
            FROM evaluation.case_results AS result
            JOIN evaluation.cases AS case_row ON case_row.id = result.case_id
            WHERE result.eval_run_id = $1::uuid
            ORDER BY case_row.case_key
            """,
            str(run_id),
        )
    results = [dict(row) for row in rows]
    for result in results:
        result["score"] = _json_value(result.pop("score_json"))
        result["failure_codes"] = list(result["failure_codes"])
    return {
        **dict(run),
        "summary": _json_value(run["summary_json"]),
        "case_results": results,
    }
