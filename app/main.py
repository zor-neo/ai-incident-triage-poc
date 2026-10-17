"""FastAPI entry point: middleware, correlation IDs, and API routes."""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import __version__
from app.bedrock_client import TriageClient, TriageUnavailable
from app.config import Settings, get_settings
from app.logging_config import configure_logging, get_logger
from app.metrics import MetricsEmitter
from app.models import (
    AiTriage,
    HealthResponse,
    IncidentCreate,
    IncidentResponse,
    TriageMetrics,
    utc_now_iso,
)
from app.repository import (
    FALLBACK_MESSAGE,
    IncidentRepository,
    new_incident_id,
    new_request_id,
)
from app.sanitizer import sanitize

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _resolve_request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    if supplied and REQUEST_ID_PATTERN.match(supplied):
        return supplied
    return new_request_id()


def _build_response(item: dict[str, Any]) -> IncidentResponse:
    triage = item.get("ai_triage")
    metrics = item.get("metrics")
    return IncidentResponse(
        incident_id=item["incident_id"],
        request_id=item["request_id"],
        service=item["service"],
        environment=item["environment"],
        severity=item["severity"],
        status=item["status"],
        sanitized_log=item["sanitized_log"],
        ai_triage=AiTriage(**triage) if triage else None,
        metrics=TriageMetrics(**metrics) if metrics else None,
        message=item.get("message"),
        created_at=item["created_at"],
        updated_at=item["updated_at"],
    )


def create_app(
    settings: Settings | None = None,
    repo: IncidentRepository | None = None,
    triage: TriageClient | None = None,
    metrics: MetricsEmitter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    logger = get_logger("app.main")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.repo = repo or IncidentRepository(
            table_name=settings.table_name,
            region=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
        app.state.triage = triage or TriageClient(settings)
        app.state.metrics = metrics or MetricsEmitter(settings)
        logger.info(
            "startup",
            environment=settings.environment,
            git_sha=settings.git_sha,
            model_id=settings.bedrock_model_id,
            version=__version__,
        )
        yield
        logger.info("shutdown")

    app = FastAPI(
        title="AI Incident & Log Triage Service",
        description=(
            "Ingests error logs, scrubs PII/secrets, persists raw incident state, "
            "and enriches the record with AI triage via Amazon Bedrock."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    def get_repo(request: Request) -> IncidentRepository:
        return request.app.state.repo

    def get_triage(request: Request) -> TriageClient:
        return request.app.state.triage

    def get_metrics(request: Request) -> MetricsEmitter:
        return request.app.state.metrics

    @app.middleware("http")
    async def correlation_id(
        request: Request, call_next: Callable[[Request], Any]
    ) -> Response:
        request_id = _resolve_request_id(request)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, git_sha=settings.git_sha, environment=settings.environment
        )
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        structlog.contextvars.clear_contextvars()
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.post(
        "/incidents",
        response_model=IncidentResponse,
        status_code=201,
        responses={
            201: {"description": "Incident persisted and AI triage completed"},
            202: {"description": "Incident persisted; AI triage unavailable (degraded)"},
        },
    )
    def create_incident(
        payload: IncidentCreate,
        request: Request,
        response: Response,
        repo_: IncidentRepository = Depends(get_repo),
        triage_: TriageClient = Depends(get_triage),
        metrics_: MetricsEmitter = Depends(get_metrics),
    ) -> IncidentResponse:
        request_id: str = request.state.request_id
        incident_id = new_incident_id()
        structlog.contextvars.bind_contextvars(incident_id=incident_id)

        sanitized = sanitize(payload.error_log)
        repo_.create_pending(
            incident=payload,
            sanitized_log=sanitized.text,
            incident_id=incident_id,
            request_id=request_id,
        )
        logger.info(
            "incident_persisted",
            service=payload.service,
            severity=payload.severity.value,
            redactions=sanitized.redactions,
        )

        try:
            triage, triage_metrics = triage_.triage(
                service=payload.service,
                severity=payload.severity.value,
                environment=payload.environment,
                error_log=sanitized.text,
            )
        except TriageUnavailable as exc:
            repo_.mark_ai_unavailable(incident_id, FALLBACK_MESSAGE, exc.metrics)
            metrics_.emit_triage_fallback(exc.reason)
            logger.warning("triage_fallback", reason=exc.reason, detail=exc.detail)
            response.status_code = 202
        else:
            repo_.mark_triaged(incident_id, triage, triage_metrics)
            metrics_.emit_triage_success(triage_metrics)
            logger.info("triage_completed", status="TRIAGED")
            response.status_code = 201

        stored = repo_.get(incident_id)
        if stored is None:  # pragma: no cover - defensive
            stored = {
                "incident_id": incident_id,
                "request_id": request_id,
                "service": payload.service,
                "environment": payload.environment,
                "severity": payload.severity.value,
                "status": "PENDING_TRIAGE",
                "sanitized_log": sanitized.text,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
        return _build_response(stored)

    @app.get("/incidents/{incident_id}", response_model=IncidentResponse)
    def get_incident(
        incident_id: str,
        repo_: IncidentRepository = Depends(get_repo),
    ) -> IncidentResponse:
        item = repo_.get(incident_id)
        if item is None:
            return JSONResponse(  # type: ignore[return-value]
                status_code=404,
                content={"detail": f"Incident {incident_id} not found"},
            )
        return _build_response(item)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="healthy", version=settings.git_sha, environment=settings.environment
        )

    @app.get("/internal/config", include_in_schema=False)
    def internal_config(request: Request) -> JSONResponse:
        """Prove runtime secret injection.

        The endpoint only exists when INTERNAL_API_KEY is set. In Level 2 that
        value is never in the image or the repo: the ECS task definition pulls it
        from an SSM SecureString at task start (spec section 6.2). Returning the
        non-secret runtime config here is how the SSM -> task -> process chain is
        verified from outside, and the key itself is never echoed back.
        """
        if not settings.internal_api_key:
            return JSONResponse(
                status_code=404,
                content={"detail": "internal config endpoint is not enabled"},
            )
        supplied = request.headers.get("X-Internal-Key", "")
        if not compare_digest(supplied, settings.internal_api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing X-Internal-Key"},
            )
        return JSONResponse(
            status_code=200,
            content={
                "environment": settings.environment,
                "table_name": settings.table_name,
                "bedrock_model_id": settings.bedrock_model_id,
                "metrics_namespace": settings.metrics_namespace,
                "git_sha": settings.git_sha,
                "internal_api_key_source": "injected_at_runtime",
            },
        )

    return app


app = create_app()
