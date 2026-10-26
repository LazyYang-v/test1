"""FastAPI application factory for the AgentForge control plane."""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agentforge_api.auth import (
    ApiError,
    create_access_token,
    current_principal,
    log_auth_failure,
    verify_password,
)
from agentforge_api.config import Settings
from agentforge_api.evaluations import router as evaluation_router
from agentforge_api.readiness import check_database_readiness
from agentforge_api.observability import (
    configure_json_logging,
    configure_telemetry,
    log_event,
)
from agentforge_api.rate_limit import StreamConcurrencyLimiter, TokenBucketLimiter
from agentforge_api.tasks import router as task_router
from agentforge_core.observability import span

logger = logging.getLogger("agentforge.api")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@asynccontextmanager
async def _database_connection(dsn: str, *, timeout: float | None = None):
    connect_kwargs = {} if timeout is None else {"timeout": timeout}
    conn = await asyncpg.connect(dsn, **connect_kwargs)
    try:
        yield conn
    finally:
        await conn.close()


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


async def require_region_grant(
    conn: asyncpg.Connection,
    *,
    principal_id: str,
    region_code: str,
    permission: str = "read",
) -> bool:
    """Check an active grant against PostgreSQL at the time of the operation."""
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM identity.region_grants
                WHERE user_id = $1::uuid
                  AND region_code = $2
                  AND permission IN ($3, 'admin')
                  AND revoked_at IS NULL
            )
            """,
            principal_id,
            region_code,
            permission,
        )
    )


async def _audit_region_denial(
    conn: asyncpg.Connection, request: Request, principal_id: str, region_code: str
) -> None:
    await conn.execute(
        """
        INSERT INTO audit.audit_events
            (actor_type, actor_id, action, resource_type, resource_id, decision,
             reason_code, metadata_json, trace_id)
        VALUES ('user', $1::uuid, 'region.authorization', 'region', $2::text, 'denied',
                'REGION_ACCESS_DENIED', jsonb_build_object('region_code', $2::text), $3)
        """,
        principal_id,
        region_code,
        request.state.request_id,
    )


async def authorize_region(
    conn: asyncpg.Connection,
    request: Request,
    principal: dict[str, Any],
    region_code: str,
    permission: str = "read",
) -> None:
    if await require_region_grant(
        conn,
        principal_id=principal["id"],
        region_code=region_code,
        permission=permission,
    ):
        return
    await _audit_region_denial(conn, request, principal["id"], region_code)
    raise ApiError(403, "REGION_ACCESS_DENIED", "Access to this region is denied")


def _error_response(request: Request, error: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        headers=error.headers,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "details": error.details,
            },
            "request_id": request.state.request_id,
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_json_logging()
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = app_settings
        app.state.connect = lambda: _database_connection(app_settings.api_db_dsn)
        app.state.connect_events = lambda: _database_connection(
            app_settings.api_db_dsn, timeout=1.0
        )
        configure_telemetry(
            app_settings.otel_enabled, app_settings.otel_exporter_otlp_endpoint
        )
        yield

    app = FastAPI(title="AgentForge API", lifespan=lifespan)
    app.include_router(task_router)
    app.include_router(evaluation_router)
    # Fault hooks are an optional deterministic test seam, inert in production.
    app.state.fault_injector = NOOP_FAULT_INJECTOR
    # ASGI tests may use HTTPX's ASGITransport without starting lifespan.
    app.state.settings = app_settings
    app.state.connect = lambda: _database_connection(app_settings.api_db_dsn)
    app.state.connect_events = lambda: _database_connection(
        app_settings.api_db_dsn, timeout=1.0
    )
    app.state.stream_limiter = StreamConcurrencyLimiter(
        global_limit=100, per_principal_limit=5
    )
    app.state.rate_limiter = TokenBucketLimiter(
        rate=app_settings.rate_limit_per_second,
        burst=app_settings.rate_limit_burst,
        max_entries=app_settings.rate_limit_max_entries,
        idle_timeout=app_settings.rate_limit_idle_seconds,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if _REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        request.state.trace_id = request_id
        with span(
            "http.request",
            request_id=request_id,
            method=request.method,
            route=request.url.path,
        ):
            try:
                response = await call_next(request)
            except Exception:
                log_event(
                    logger,
                    logging.ERROR,
                    "request.failed",
                    request_id=request_id,
                    route=request.url.path,
                    method=request.method,
                    error_code="INTERNAL_ERROR",
                )
                raise
        if response.status_code >= 500:
            log_event(
                logger,
                logging.ERROR,
                "request.failed",
                request_id=request_id,
                status_code=response.status_code,
                error_code="INTERNAL_ERROR",
            )
        else:
            log_event(
                logger,
                logging.INFO,
                "request.completed",
                request_id=request_id,
                status_code=response.status_code,
            )
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return _error_response(request, error)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        if error.status_code == 404:
            api_error = ApiError(
                404,
                "NOT_FOUND",
                "The requested resource was not found",
            )
        elif error.status_code == 405:
            api_error = ApiError(
                405,
                "METHOD_NOT_ALLOWED",
                "The request method is not allowed",
            )
        else:
            api_error = ApiError(
                error.status_code,
                "HTTP_ERROR",
                "The request could not be completed",
            )
        return _error_response(request, api_error)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        del error
        return _error_response(
            request,
            ApiError(500, "INTERNAL_ERROR", "The request could not be completed"),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            request,
            ApiError(
                400,
                "INVALID_REQUEST",
                "The request is invalid",
                details={
                    "fields": sorted(
                        ".".join(str(part) for part in item["loc"])
                        for item in error.errors()
                    )
                },
            ),
        )

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        body = await check_database_readiness(app_settings.api_db_dsn)
        status_code = 200 if body["status"] == "ready" else 503
        return JSONResponse(status_code=status_code, content=body)

    @app.post("/api/v1/auth/login")
    async def login(body: LoginRequest, request: Request) -> dict[str, Any]:
        normalized_email = body.email.strip().lower()
        async with app.state.connect() as conn:
            user = await conn.fetchrow(
                """
                SELECT id::text AS id, password_hash, status, token_version
                FROM identity.users WHERE email = $1
                """,
                normalized_email,
            )
            valid_password = await verify_password(
                body.password, user["password_hash"] if user is not None else None
            )
            if user is None or not valid_password or user["status"] != "active":
                await log_auth_failure(conn, request, "invalid_credentials")
                raise ApiError(401, "UNAUTHENTICATED", "Invalid email or password")

            token, expires_in = create_access_token(
                user_id=user["id"],
                token_version=user["token_version"],
                secret=app_settings.jwt_secret,
                ttl_seconds=app_settings.jwt_ttl_seconds,
            )
        return {"access_token": token, "token_type": "bearer", "expires_in": expires_in}

    @app.get("/api/v1/auth/me")
    async def current_user(
        request: Request, principal: dict[str, Any] = Depends(current_principal)
    ) -> dict[str, Any]:
        async with app.state.connect() as conn:
            grants = await conn.fetch(
                """
                SELECT region_code, permission
                FROM identity.region_grants
                WHERE user_id = $1::uuid AND revoked_at IS NULL
                ORDER BY region_code, permission
                """,
                principal["id"],
            )
        return {
            "id": principal["id"],
            "email": principal["email"],
            "display_name": principal["display_name"],
            "regions": [dict(grant) for grant in grants],
        }

    @app.get("/api/v1/regions/{region_code}/access")
    async def check_region_access(
        region_code: str,
        request: Request,
        principal: dict[str, Any] = Depends(current_principal),
    ) -> dict[str, Any]:
        async with app.state.connect() as conn:
            await authorize_region(conn, request, principal, region_code)
        return {"region_code": region_code, "permission": "read", "allowed": True}

    return app


app = create_app()
