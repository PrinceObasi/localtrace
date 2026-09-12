from datetime import datetime, timezone

from fastapi.testclient import TestClient

from localtrace_backend.app import create_app
from localtrace_backend.evidence import FileEvidenceService
from localtrace_backend.models import (
    AlertsResponse,
    CapabilityStatus,
    EventsResponse,
    FilesystemFamily,
    IOResponse,
    IORate,
    NFSDetails,
    QuotaEntry,
    QuotasResponse,
    Volume,
    VolumeHealth,
    VolumesResponse,
)


NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


class FakeVolumes:
    def __init__(self, items: list[Volume] | None = None) -> None:
        self.calls = 0
        self.items = items or []

    def collect(self) -> VolumesResponse:
        self.calls += 1
        return VolumesResponse(
            sampled_at=NOW,
            status=CapabilityStatus.AVAILABLE,
            source="test-volumes",
            items=self.items,
        )


class FakeIO:
    def __init__(self) -> None:
        self.calls = 0

    def sample(self) -> IOResponse:
        self.calls += 1
        return IOResponse(
            sampled_at=NOW,
            status=CapabilityStatus.WARMING_UP,
            source="test-io",
            message="baseline captured",
            interval_seconds=0,
            aggregate=IORate(
                read_bytes_per_second=0,
                write_bytes_per_second=0,
                read_gigabytes_per_second=0,
                write_gigabytes_per_second=0,
            ),
            devices=[],
        )


class FakeAvailableIO(FakeIO):
    def sample(self) -> IOResponse:
        result = super().sample()
        return result.model_copy(
            update={"status": CapabilityStatus.AVAILABLE, "message": None}
        )


class FakeQuotas:
    def __init__(
        self,
        status: CapabilityStatus = CapabilityStatus.AVAILABLE,
        items: list[QuotaEntry] | None = None,
    ) -> None:
        self.calls = 0
        self.status = status
        self.items = items or []

    def collect(self) -> QuotasResponse:
        self.calls += 1
        return QuotasResponse(
            sampled_at=NOW,
            status=self.status,
            source="test-quotas",
            message="test quota state",
            user="dikachi",
            uid=501,
            items=self.items,
        )


class AvailableEvidence:
    watched_path = "/tmp/localtrace-test"
    threshold_bytes = 64

    def events_snapshot(self, limit=500) -> EventsResponse:
        return EventsResponse(
            sampled_at=NOW,
            status=CapabilityStatus.AVAILABLE,
            source="test-events",
            message=None,
            watched_path=self.watched_path,
            items=[],
        )

    def alerts_snapshot(self, limit=100) -> AlertsResponse:
        return AlertsResponse(
            sampled_at=NOW,
            status=CapabilityStatus.AVAILABLE,
            source="test-file-alerts",
            message=None,
            watched_path=self.watched_path,
            threshold_bytes=self.threshold_bytes,
            capacity_threshold_percent=90,
            items=[],
        )

    def get_alert(self, _alert_id):
        return None


def make_client(
    evidence_service: FileEvidenceService | None = None,
    volume_collector: FakeVolumes | None = None,
    io_sampler: FakeIO | None = None,
    quota_collector: FakeQuotas | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            volume_collector=volume_collector or FakeVolumes(),  # type: ignore[arg-type]
            io_sampler=io_sampler or FakeIO(),  # type: ignore[arg-type]
            evidence_service=evidence_service,
            quota_collector=quota_collector or FakeQuotas(),  # type: ignore[arg-type]
            prime_io=False,
            start_watcher=False,
        )
    )


def test_dashboard_has_stable_degraded_shape_while_io_warms() -> None:
    with make_client() as client:
        response = client.get("/api/v1/dashboard")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["overall_status"] == "partial"
    assert body["volumes"]["status"] == "available"
    assert body["io"]["status"] == "warming_up"
    assert body["io"]["aggregate"]["read_bytes_per_second"] == 0


def test_health_is_cheap_and_exposes_last_sampled_capability_statuses() -> None:
    volumes = FakeVolumes()
    io = FakeIO()
    with make_client(volume_collector=volumes, io_sampler=io) as client:
        initial = client.get("/api/v1/health")
        assert volumes.calls == 0
        assert io.calls == 0
        client.get("/api/v1/dashboard")
        response = client.get("/api/v1/health")
        client.get("/api/v1/health")

    assert initial.json()["capabilities"]["disk_io"]["source"] == "not-sampled"
    assert volumes.calls == 1
    assert io.calls == 1
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["capabilities"]["volumes"]["source"] == "test-volumes"
    assert body["capabilities"]["disk_io"]["status"] == "warming_up"


def test_cors_is_limited_to_loopback_development_origins() -> None:
    with make_client() as client:
        local = client.get(
            "/api/v1/io", headers={"Origin": "http://localhost:5173"}
        )
        external = client.get(
            "/api/v1/io", headers={"Origin": "https://example.com"}
        )

    assert local.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in external.headers


def test_host_header_is_limited_to_loopback_names() -> None:
    with make_client() as client:
        loopback = client.get(
            "/api/v1/health", headers={"Host": "localhost:8000"}
        )
        rebound = client.get(
            "/api/v1/health", headers={"Host": "telemetry.attacker.example"}
        )

    assert loopback.status_code == 200
    assert rebound.status_code == 400


def test_all_required_routes_exist() -> None:
    with make_client() as client:
        for path in (
            "/api/v1/health",
            "/api/v1/volumes",
            "/api/v1/io",
            "/api/v1/dashboard",
            "/api/v1/events",
            "/api/v1/alerts",
            "/api/v1/quotas",
        ):
            assert client.get(path).status_code == 200


def test_alert_detail_and_dashboard_event_relationship(tmp_path) -> None:
    evidence = FileEvidenceService(
        watched_path=str(tmp_path),
        threshold_bytes=4,
        observer_factory=None,
    )
    target = tmp_path / "model.gguf"
    target.write_bytes(b"12345")
    event = evidence.process("created", str(target))
    alert = evidence.alerts_snapshot().items[0]

    with make_client(evidence) as client:
        dashboard = client.get("/api/v1/dashboard")
        detail = client.get(f"/api/v1/alerts/{alert.id}")
        missing = client.get("/api/v1/alerts/not-a-real-id")

    assert dashboard.status_code == 200
    body = dashboard.json()
    assert body["events"]["items"][0]["id"] == event.id
    assert body["alerts"]["items"][0]["related_event_ids"] == [event.id]
    assert detail.status_code == 200
    assert detail.json()["id"] == alert.id
    assert missing.status_code == 404


def test_dashboard_and_health_expose_cached_quota_capability() -> None:
    quotas = FakeQuotas()
    with make_client(quota_collector=quotas) as client:
        dashboard = client.get("/api/v1/dashboard")
        health = client.get("/api/v1/health")

    assert quotas.calls == 1
    assert dashboard.json()["quotas"] == {
        "sampled_at": "2026-09-12T15:00:00Z",
        "status": "available",
        "source": "test-quotas",
        "message": "test quota state",
        "user": "dikachi",
        "uid": 501,
        "items": [],
    }
    assert health.json()["capabilities"]["quotas"]["source"] == "test-quotas"


def test_optional_unavailable_quota_does_not_degrade_healthy_core() -> None:
    quotas = FakeQuotas(CapabilityStatus.UNAVAILABLE)
    client = TestClient(
        create_app(
            volume_collector=FakeVolumes(),  # type: ignore[arg-type]
            io_sampler=FakeAvailableIO(),  # type: ignore[arg-type]
            evidence_service=AvailableEvidence(),  # type: ignore[arg-type]
            quota_collector=quotas,  # type: ignore[arg-type]
            prime_io=False,
            start_watcher=False,
        )
    )

    with client:
        dashboard = client.get("/api/v1/dashboard")
        health = client.get("/api/v1/health")

    assert dashboard.json()["overall_status"] == "available"
    assert health.json()["status"] == "ok"
    assert health.json()["capabilities"]["quotas"]["status"] == "unavailable"


def test_quota_error_is_visible_in_dashboard_and_health() -> None:
    quotas = FakeQuotas(CapabilityStatus.ERROR)
    with make_client(quota_collector=quotas) as client:
        dashboard = client.get("/api/v1/dashboard")
        health = client.get("/api/v1/health")

    assert dashboard.json()["overall_status"] == "partial"
    assert health.json()["status"] == "degraded"
    assert health.json()["capabilities"]["quotas"]["status"] == "error"


def _generic_nfs_quota() -> QuotaEntry:
    return QuotaEntry(
        user="dikachi",
        uid=501,
        filesystem="/Volumes/Shared Models",
        filesystem_family=FilesystemFamily.NFS,
        limit_semantics="unknown",
        mount_point="/Volumes/Shared Models",
        used_bytes=1024,
        soft_limit_bytes=2048,
        hard_limit_bytes=4096,
        files_used=1,
        file_soft_limit=2,
        file_hard_limit=4,
        block_over_limit=False,
        file_over_limit=False,
    )


def _nfs_volume(protocol_version: str = "4.1") -> Volume:
    return Volume(
        id="nfs-volume",
        name="Shared Models",
        mount_point="/Volumes/Shared Models",
        device="server:/models",
        filesystem="nfs",
        filesystem_family=FilesystemFamily.NFS,
        remote=True,
        read_only=False,
        total_bytes=10_000,
        used_bytes=2_000,
        available_bytes=8_000,
        used_percent=20,
        health=VolumeHealth(status=CapabilityStatus.UNAVAILABLE),
        nfs=NFSDetails(
            protocol_version=protocol_version,
            pnfs_status=CapabilityStatus.UNAVAILABLE,
            pnfs_message="Unsupported on current macOS.",
        ),
    )


def test_dashboard_reconciles_generic_nfs_quota_from_same_volume_sample() -> None:
    quotas = FakeQuotas(items=[_generic_nfs_quota()])
    with make_client(
        volume_collector=FakeVolumes([_nfs_volume()]), quota_collector=quotas
    ) as client:
        body = client.get("/api/v1/dashboard").json()

    assert body["quotas"]["items"][0]["limit_semantics"] == "remaining_availability"
    assert "NFSv4 quota availability" in body["quotas"]["message"]
    assert quotas.items[0].limit_semantics == "unknown"


def test_quota_route_uses_only_cached_volume_sample_for_reconciliation() -> None:
    volumes = FakeVolumes([_nfs_volume("3")])
    quotas = FakeQuotas(items=[_generic_nfs_quota()])
    with make_client(volume_collector=volumes, quota_collector=quotas) as client:
        before_volume_sample = client.get("/api/v1/quotas").json()
        client.get("/api/v1/volumes")
        after_volume_sample = client.get("/api/v1/quotas").json()

    assert before_volume_sample["items"][0]["limit_semantics"] == "unknown"
    assert after_volume_sample["items"][0]["limit_semantics"] == "absolute_limit"
    assert volumes.calls == 1
    assert quotas.calls == 2


def test_volume_collection_creates_deduplicated_capacity_alert_and_detail() -> None:
    high = Volume(
        id="volume-high",
        name="Macintosh HD",
        mount_point="/",
        device="/dev/disk3s5",
        filesystem="apfs",
        filesystem_family=FilesystemFamily.APFS,
        remote=False,
        read_only=False,
        total_bytes=1_000,
        used_bytes=950,
        available_bytes=50,
        used_percent=95,
        health=VolumeHealth(status=CapabilityStatus.UNAVAILABLE),
    )
    volumes = FakeVolumes([high])

    with make_client(volume_collector=volumes) as client:
        assert client.get("/api/v1/volumes").status_code == 200
        assert client.get("/api/v1/volumes").status_code == 200
        response = client.get("/api/v1/alerts")
        alert = response.json()["items"][0]
        detail = client.get(f"/api/v1/alerts/{alert['id']}")

    assert len(response.json()["items"]) == 1
    assert alert["rule"] == "CAPACITY_PRESSURE"
    assert alert["observed_percent"] == 95
    assert alert["available_bytes"] == 50
    assert detail.status_code == 200
    assert detail.json() == alert
