"""FastAPI application for the LocalTrace local telemetry service."""

from __future__ import annotations

import asyncio
import platform
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from localtrace_backend import __version__
from localtrace_backend.capacity_alerts import CapacityAlertService
from localtrace_backend.collectors.fs_benchmark import FilesystemBenchmarkService
from localtrace_backend.collectors.io import DiskIOSampler
from localtrace_backend.collectors.quotas import (
    QuotaCollector,
    reconcile_quota_semantics,
)
from localtrace_backend.collectors.volumes import VolumeCollector
from localtrace_backend.collectors.storage_health import StorageHealthCollector
from localtrace_backend.collectors.usage import DiskUsageScanner
from localtrace_backend.evidence import FileEvidenceService
from localtrace_backend.notify import AlertDeliveryService
from localtrace_backend.models import (
    Alert,
    AlertsResponse,
    BenchmarkJob,
    BenchmarkRequest,
    BenchmarksResponse,
    CapabilityStatus,
    CapabilitySummary,
    DashboardResponse,
    EventsResponse,
    HealthResponse,
    IOResponse,
    QuotasResponse,
    StorageHealthResponse,
    UsageResponse,
    ServiceStatus,
    VolumesResponse,
)


LOCAL_DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://[::1]:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def _overall_status(statuses: Sequence[CapabilityStatus]) -> CapabilityStatus:
    if statuses and all(status == CapabilityStatus.AVAILABLE for status in statuses):
        return CapabilityStatus.AVAILABLE
    if statuses and all(status == CapabilityStatus.ERROR for status in statuses):
        return CapabilityStatus.ERROR
    if statuses and all(status == CapabilityStatus.UNAVAILABLE for status in statuses):
        return CapabilityStatus.UNAVAILABLE
    if statuses and all(status == CapabilityStatus.WARMING_UP for status in statuses):
        return CapabilityStatus.WARMING_UP
    return CapabilityStatus.PARTIAL


def create_app(
    *,
    volume_collector: VolumeCollector | None = None,
    io_sampler: DiskIOSampler | None = None,
    evidence_service: FileEvidenceService | None = None,
    quota_collector: QuotaCollector | None = None,
    capacity_alert_service: CapacityAlertService | None = None,
    usage_scanner: DiskUsageScanner | None = None,
    alert_delivery: AlertDeliveryService | None = None,
    storage_health_collector: StorageHealthCollector | None = None,
    benchmark_service: FilesystemBenchmarkService | None = None,
    prime_io: bool = True,
    start_watcher: bool = True,
) -> FastAPI:
    collector = volume_collector or VolumeCollector()
    sampler = io_sampler or DiskIOSampler()
    evidence = evidence_service or FileEvidenceService.from_environment()
    quotas_collector = quota_collector or QuotaCollector()
    capacity_alerts = capacity_alert_service or CapacityAlertService.from_environment()
    usage = usage_scanner or DiskUsageScanner.from_environment(evidence.watched_directories)
    delivery = alert_delivery or AlertDeliveryService.from_environment()

    def local_apfs_mount_points() -> list[str]:
        latest: VolumesResponse | None = getattr(application.state, "latest_volumes", None)
        if latest is None:
            return ["/"]
        points = [
            item.mount_point
            for item in latest.items
            if item.apfs is not None and not item.remote
        ]
        return points or ["/"]

    storage_health = storage_health_collector or StorageHealthCollector.from_environment(
        local_apfs_mount_points
    )
    benchmarks = benchmark_service or FilesystemBenchmarkService.from_environment(
        lambda: getattr(application.state, "latest_volumes", None)
    )
    # Both alert rules hand new alerts to the same delivery queue.
    for producer in (evidence, capacity_alerts):
        setter = getattr(producer, "set_alert_listener", None)
        if setter is not None:
            setter(delivery.deliver)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        startup_tasks = []
        if prime_io:
            # Establish a cumulative-counter baseline without delaying import or
            # pretending that a zero first sample is a measured zero rate.
            async def prime_disk_io() -> None:
                application.state.latest_io = await run_in_threadpool(
                    application.state.io_sampler.sample
                )

            startup_tasks.append(prime_disk_io())
        if start_watcher:
            # Delivery starts first so any alert raised while the watcher or
            # the first capacity evaluation runs is queued for the worker,
            # never delivered on the raising thread.
            application.state.alert_delivery.start()
            startup_tasks.append(
                run_in_threadpool(application.state.evidence_service.start)
            )
        if startup_tasks:
            await asyncio.gather(*startup_tasks)
        if start_watcher:
            # The scanner reads the watcher's live target list, so it starts
            # only after the watcher has resolved which directories exist.
            application.state.usage_scanner.start()
            application.state.storage_health_collector.start()
        try:
            yield
        finally:
            if start_watcher:
                await run_in_threadpool(application.state.storage_health_collector.stop)
                await run_in_threadpool(application.state.usage_scanner.stop)
                await run_in_threadpool(application.state.evidence_service.stop)
                await run_in_threadpool(application.state.alert_delivery.stop)

    application = FastAPI(
        title="LocalTrace API",
        summary="Local-first macOS storage telemetry",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    application.state.volume_collector = collector
    application.state.io_sampler = sampler
    application.state.evidence_service = evidence
    application.state.quota_collector = quotas_collector
    application.state.capacity_alert_service = capacity_alerts
    application.state.usage_scanner = usage
    application.state.alert_delivery = delivery
    application.state.storage_health_collector = storage_health
    application.state.benchmark_service = benchmarks
    application.state.latest_volumes = None
    application.state.latest_io = None
    application.state.latest_quotas = None

    # Loopback binding is the primary boundary. Host validation also prevents
    # a browser from reading local telemetry through a DNS-rebinding hostname.
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
        www_redirect=False,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(LOCAL_DEV_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Accept", "Content-Type"],
        max_age=600,
    )

    @application.middleware("http")
    async def disable_telemetry_caching(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def combined_alerts(request: Request, limit: int = 100) -> AlertsResponse:
        evidence_alerts = request.app.state.evidence_service.alerts_snapshot(limit)
        capacity_status, capacity_message, capacity_items = (
            request.app.state.capacity_alert_service.snapshot(limit)
        )
        items = sorted(
            [*evidence_alerts.items, *capacity_items],
            key=lambda item: item.occurred_at,
            reverse=True,
        )[:limit]
        messages = [
            message
            for message in (evidence_alerts.message, capacity_message)
            if message
        ]
        return AlertsResponse(
            sampled_at=datetime.now(timezone.utc),
            status=_overall_status((evidence_alerts.status, capacity_status)),
            source=f"{evidence_alerts.source}+{request.app.state.capacity_alert_service.source}",
            message=" ".join(dict.fromkeys(messages)) or None,
            watched_path=evidence_alerts.watched_path,
            watch_targets=list(evidence_alerts.watch_targets),
            threshold_bytes=evidence_alerts.threshold_bytes,
            capacity_threshold_percent=(
                request.app.state.capacity_alert_service.threshold_percent
            ),
            delivery=request.app.state.alert_delivery.snapshot(),
            items=items,
        )

    async def collect_dashboard(request: Request) -> DashboardResponse:
        volumes, io, quotas = await asyncio.gather(
            run_in_threadpool(request.app.state.volume_collector.collect),
            run_in_threadpool(request.app.state.io_sampler.sample),
            run_in_threadpool(request.app.state.quota_collector.collect),
        )
        quotas = reconcile_quota_semantics(quotas, volumes)
        request.app.state.latest_volumes = volumes
        request.app.state.latest_io = io
        request.app.state.latest_quotas = quotas
        request.app.state.capacity_alert_service.evaluate(volumes)
        events = request.app.state.evidence_service.events_snapshot()
        alerts = combined_alerts(request)
        usage = request.app.state.usage_scanner.snapshot()
        storage_health = request.app.state.storage_health_collector.snapshot()
        benchmarks = request.app.state.benchmark_service.snapshot()
        return DashboardResponse(
            sampled_at=datetime.now(timezone.utc),
            overall_status=_overall_status(
                (
                    volumes.status,
                    io.status,
                    events.status,
                    alerts.status,
                    *(
                        (quotas.status,)
                        if quotas.status == CapabilityStatus.ERROR
                        else ()
                    ),
                    # Like quotas, a warming-up or unavailable usage scan is a
                    # normal state and must not degrade the whole dashboard.
                    *((usage.status,) if usage.status == CapabilityStatus.ERROR else ()),
                    *(
                        (storage_health.status,)
                        if storage_health.status == CapabilityStatus.ERROR
                        else ()
                    ),
                )
            ),
            volumes=volumes,
            io=io,
            events=events,
            alerts=alerts,
            quotas=quotas,
            usage=usage,
            storage_health=storage_health,
            benchmarks=benchmarks,
        )

    @application.get("/api/v1/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        latest_volumes: VolumesResponse | None = request.app.state.latest_volumes
        latest_io: IOResponse | None = request.app.state.latest_io
        latest_quotas: QuotasResponse | None = request.app.state.latest_quotas
        events = request.app.state.evidence_service.events_snapshot(limit=1)
        alerts = combined_alerts(request, limit=1)
        usage = request.app.state.usage_scanner.snapshot()
        storage_health = request.app.state.storage_health_collector.snapshot()
        volume_summary = (
            CapabilitySummary(
                status=latest_volumes.status,
                source=latest_volumes.source,
                message=latest_volumes.message,
            )
            if latest_volumes
            else CapabilitySummary(
                status=CapabilityStatus.WARMING_UP,
                source="not-sampled",
                message="Volume telemetry has not been sampled yet.",
            )
        )
        io_summary = (
            CapabilitySummary(
                status=latest_io.status,
                source=latest_io.source,
                message=latest_io.message,
            )
            if latest_io
            else CapabilitySummary(
                status=CapabilityStatus.WARMING_UP,
                source="not-sampled",
                message="Disk I/O telemetry has not been sampled yet.",
            )
        )
        quota_summary = (
            CapabilitySummary(
                status=latest_quotas.status,
                source=latest_quotas.source,
                message=latest_quotas.message,
            )
            if latest_quotas
            else CapabilitySummary(
                status=CapabilityStatus.WARMING_UP,
                source="not-sampled",
                message="Quota telemetry has not been sampled yet.",
            )
        )
        capability_statuses = (
            volume_summary.status,
            io_summary.status,
            events.status,
            alerts.status,
            *(
                (quota_summary.status,)
                if quota_summary.status == CapabilityStatus.ERROR
                else ()
            ),
            *((usage.status,) if usage.status == CapabilityStatus.ERROR else ()),
            *(
                (storage_health.status,)
                if storage_health.status == CapabilityStatus.ERROR
                else ()
            ),
        )
        degraded = _overall_status(capability_statuses) != CapabilityStatus.AVAILABLE
        return HealthResponse(
            service="localtrace-api",
            version=__version__,
            status=ServiceStatus.DEGRADED if degraded else ServiceStatus.OK,
            sampled_at=datetime.now(timezone.utc),
            platform=platform.platform(),
            capabilities={
                "volumes": CapabilitySummary(
                    status=volume_summary.status,
                    source=volume_summary.source,
                    message=volume_summary.message,
                ),
                "disk_io": CapabilitySummary(
                    status=io_summary.status,
                    source=io_summary.source,
                    message=io_summary.message,
                ),
                "file_events": CapabilitySummary(
                    status=events.status,
                    source=events.source,
                    message=events.message,
                ),
                "alerts": CapabilitySummary(
                    status=alerts.status,
                    source=alerts.source,
                    message=alerts.message,
                ),
                "quotas": quota_summary,
                "usage": CapabilitySummary(
                    status=usage.status,
                    source=usage.source,
                    message=usage.message,
                ),
                "storage_health": CapabilitySummary(
                    status=storage_health.status,
                    source=storage_health.source,
                    message=storage_health.message,
                ),
                "alert_delivery": CapabilitySummary(
                    status=alerts.delivery.status,
                    source=alerts.delivery.source,
                    message=alerts.delivery.message,
                )
                if alerts.delivery
                else CapabilitySummary(
                    status=CapabilityStatus.UNAVAILABLE,
                    source="not-configured",
                    message="No alert delivery service is configured.",
                ),
            },
        )

    @application.get("/api/v1/volumes", response_model=VolumesResponse)
    async def volumes(request: Request) -> VolumesResponse:
        result = await run_in_threadpool(request.app.state.volume_collector.collect)
        request.app.state.latest_volumes = result
        request.app.state.capacity_alert_service.evaluate(result)
        return result

    @application.get("/api/v1/io", response_model=IOResponse)
    async def disk_io(request: Request) -> IOResponse:
        result = await run_in_threadpool(request.app.state.io_sampler.sample)
        request.app.state.latest_io = result
        return result

    @application.get("/api/v1/quotas", response_model=QuotasResponse)
    async def quotas(request: Request) -> QuotasResponse:
        result = await run_in_threadpool(request.app.state.quota_collector.collect)
        latest_volumes: VolumesResponse | None = request.app.state.latest_volumes
        if latest_volumes is not None:
            result = reconcile_quota_semantics(result, latest_volumes)
        request.app.state.latest_quotas = result
        return result

    @application.get("/api/v1/benchmarks", response_model=BenchmarksResponse)
    async def benchmarks_route(request: Request) -> BenchmarksResponse:
        return request.app.state.benchmark_service.snapshot()

    @application.post("/api/v1/benchmarks", response_model=BenchmarkJob, status_code=202)
    async def start_benchmark_route(
        request: Request, body: BenchmarkRequest
    ) -> BenchmarkJob:
        try:
            return await run_in_threadpool(request.app.state.benchmark_service.start, body)
        except ValueError as exc:
            raise HTTPException(status_code=409 if "already running" in str(exc) else 400, detail=str(exc)) from exc

    @application.get("/api/v1/benchmarks/{job_id}", response_model=BenchmarkJob)
    async def benchmark_route(request: Request, job_id: str) -> BenchmarkJob:
        job = request.app.state.benchmark_service.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown benchmark job")
        return job

    @application.get("/api/v1/storage-health", response_model=StorageHealthResponse)
    async def storage_health_route(request: Request) -> StorageHealthResponse:
        return request.app.state.storage_health_collector.snapshot()

    @application.get("/api/v1/usage", response_model=UsageResponse)
    async def usage_route(request: Request) -> UsageResponse:
        return request.app.state.usage_scanner.snapshot()

    @application.get("/api/v1/events", response_model=EventsResponse)
    async def events(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> EventsResponse:
        return request.app.state.evidence_service.events_snapshot(limit)

    @application.get("/api/v1/alerts", response_model=AlertsResponse)
    async def alerts(
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
    ) -> AlertsResponse:
        return combined_alerts(request, limit)

    @application.get("/api/v1/alerts/{alert_id}", response_model=Alert)
    async def alert_detail(request: Request, alert_id: str) -> Alert:
        alert = request.app.state.evidence_service.get_alert(alert_id)
        if alert is None:
            alert = request.app.state.capacity_alert_service.get_alert(alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        return alert

    @application.get("/api/v1/dashboard", response_model=DashboardResponse)
    async def dashboard(request: Request) -> DashboardResponse:
        return await collect_dashboard(request)

    return application


app = create_app()


def run() -> None:
    """Run the loopback-only development server."""

    uvicorn.run("localtrace_backend.app:app", host="127.0.0.1", port=8000)
