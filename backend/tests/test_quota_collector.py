from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from localtrace_backend.collectors.quotas import (
    QUOTA_TIMEOUT_SECONDS,
    QuotaCollector,
    parse_quota_output,
    reconcile_quota_semantics,
)
from localtrace_backend.models import (
    CapabilityStatus,
    FilesystemFamily,
    NFSDetails,
    QuotaEntry,
    QuotasResponse,
    Volume,
    VolumeHealth,
    VolumesResponse,
)


NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def command(stdout: str, *, returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def collector_for(runner, *, monotonic=lambda: 0.0, ttl: float = 30):
    return QuotaCollector(
        runner=runner,
        system_provider=lambda: "Darwin",
        user_provider=lambda: ("dikachi", 501),
        partitions_provider=lambda **_: [
            SimpleNamespace(
                device="server:/models",
                mountpoint="/Volumes/Shared Models",
                fstype="nfs4",
            )
        ],
        now=lambda: NOW,
        monotonic=monotonic,
        cache_ttl_seconds=ttl,
    )


def test_parses_all_apple_star_and_grace_layouts() -> None:
    output = """
Disk quotas for user dikachi (uid 501):
     Filesystem    1K blocks         quota        limit    grace   files   quota   limit   grace
/Volumes/a 10 20 30 1 2 3
/Volumes/b 11* 20 30 7days 2 3 4
/Volumes/c 12 20 30 3* 4 5 01:20
/Volumes/d 13* 20 30 2days 4* 5 6 00:30
"""

    result = parse_quota_output(output)

    assert result.malformed_rows == 0
    assert len(result.rows) == 4
    assert [(row.block_over, row.file_over) for row in result.rows] == [
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ]
    assert result.rows[1].block_grace == "7days"
    assert result.rows[2].file_grace == "01:20"


def test_collects_current_user_limits_with_spaces_and_nfs4_caveat() -> None:
    calls: list[tuple[list[str], float]] = []

    def runner(argv, timeout):
        calls.append((list(argv), timeout))
        return command(
            """Disk quotas for user dikachi (uid 501):
     Filesystem    1K blocks         quota        limit    grace   files   quota   limit   grace
/Volumes/Shared Models 1024* 2048 4096 7days 5 10 20
"""
        )

    result = collector_for(runner).collect()

    assert calls == [(["/usr/bin/quota", "-uv"], QUOTA_TIMEOUT_SECONDS)]
    assert result.status == CapabilityStatus.AVAILABLE
    assert "exceeded" in (result.message or "")
    assert "NFSv4" in (result.message or "")
    assert len(result.items) == 1
    item = result.items[0]
    assert item.user == "dikachi"
    assert item.uid == 501
    assert item.filesystem == "/Volumes/Shared Models"
    assert item.filesystem_family == FilesystemFamily.NFS
    assert item.limit_semantics == "remaining_availability"
    assert item.mount_point == "/Volumes/Shared Models"
    assert item.used_bytes == 1024 * 1024
    assert item.soft_limit_bytes == 2048 * 1024
    assert item.hard_limit_bytes == 4096 * 1024
    assert item.files_used == 5
    assert item.file_soft_limit == 10
    assert item.file_hard_limit == 20
    assert item.block_over_limit is True
    assert item.file_over_limit is False
    assert item.block_grace == "7days"
    assert item.file_grace is None


def test_wrapped_mountpoint_with_digits_is_joined_to_numeric_row() -> None:
    parsed = parse_quota_output(
        "/Volumes/Models 2026\n1024 2048 4096 5 10 20\n"
    )

    assert parsed.malformed_rows == 0
    assert parsed.rows[0].filesystem == "/Volumes/Models 2026"


def test_unrecognized_prose_cannot_be_prepended_to_valid_mountpoint() -> None:
    parsed = parse_quota_output(
        "localized or unexpected heading\n/Volumes/team 10 20 30 1 2 3\n"
    )

    assert len(parsed.rows) == 1
    assert parsed.rows[0].filesystem == "/Volumes/team"


@pytest.mark.parametrize(
    "output",
    [
        "Disk quotas for user dikachi (uid 501): none\n",
        """Disk quotas for user dikachi (uid 501):
Filesystem 1K blocks quota limit grace files quota limit grace
/Volumes/Shared Models 1024 0 0 5 0 0
""",
    ],
)
def test_successful_none_or_zero_limits_is_available_empty(output: str) -> None:
    result = collector_for(lambda *_: command(output)).collect()

    assert result.status == CapabilityStatus.AVAILABLE
    assert result.items == []
    assert "quota" in (result.message or "").casefold()


@pytest.mark.parametrize(
    ("output", "returncode", "stderr", "message_fragment"),
    [
        (
            "Disk quotas for user dikachi (uid 501): none\n",
            0,
            "RPC warning",
            "RPC warning",
        ),
        (
            "/Volumes/Shared Models 1024 0 0 5 0 0\n",
            2,
            "RPC failure",
            "status 2",
        ),
        (
            "/Volumes/Shared Models 1024 0 0 5 0 0\n"
            "/Volumes/broken 123 456 broken\n",
            0,
            "",
            "malformed",
        ),
    ],
)
def test_empty_results_preserve_command_diagnostics(
    output: str,
    returncode: int,
    stderr: str,
    message_fragment: str,
) -> None:
    result = collector_for(
        lambda *_: command(output, returncode=returncode, stderr=stderr)
    ).collect()

    assert result.status == CapabilityStatus.PARTIAL
    assert result.items == []
    assert message_fragment in (result.message or "")


def test_blank_success_output_is_an_error_not_a_clean_empty_result() -> None:
    result = collector_for(lambda *_: command("")).collect()

    assert result.status == CapabilityStatus.ERROR
    assert result.items == []
    assert "no recognizable data" in (result.message or "")


def test_non_darwin_is_unavailable_without_running_command() -> None:
    result = QuotaCollector(
        runner=lambda *_: (_ for _ in ()).throw(AssertionError("must not run")),
        system_provider=lambda: "Linux",
        user_provider=lambda: ("dikachi", 1000),
        now=lambda: NOW,
    ).collect()

    assert result.status == CapabilityStatus.UNAVAILABLE
    assert result.items == []


def test_timeout_and_malformed_output_are_explicit_errors() -> None:
    timeout = collector_for(
        lambda *_: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["/usr/bin/quota", "-uv"], 2)
        )
    ).collect()
    malformed = collector_for(
        lambda *_: command("/Volumes/models 123 456 broken\n")
    ).collect()

    assert timeout.status == CapabilityStatus.ERROR
    assert "timeout" in (timeout.message or "").casefold()
    assert malformed.status == CapabilityStatus.ERROR
    assert malformed.items == []


def test_parsed_output_with_command_failure_is_partial_not_fabricated() -> None:
    result = collector_for(
        lambda *_: command(
            "/Volumes/Shared Models 10 20 30 1 2 3\n",
            returncode=2,
            stderr="RPC warning",
        )
    ).collect()

    assert result.status == CapabilityStatus.PARTIAL
    assert len(result.items) == 1
    assert "exceeded" not in (result.message or "")
    assert "status 2" in (result.message or "")


def test_results_are_cached_and_absolute_command_is_required() -> None:
    current = [10.0]
    calls = 0

    def runner(*_):
        nonlocal calls
        calls += 1
        return command("Disk quotas for user dikachi (uid 501): none\n")

    collector = collector_for(runner, monotonic=lambda: current[0], ttl=30)
    collector.collect()
    collector.collect()
    assert calls == 1
    current[0] = 40
    collector.collect()
    assert calls == 2

    with pytest.raises(ValueError, match="absolute"):
        QuotaCollector(quota_path="quota")


def _unknown_nfs_quota(mount_point: str = "/Volumes/Shared Models") -> QuotasResponse:
    return QuotasResponse(
        sampled_at=NOW,
        status=CapabilityStatus.AVAILABLE,
        source="test-quota",
        message="Native quota data.",
        user="dikachi",
        uid=501,
        items=[
            QuotaEntry(
                user="dikachi",
                uid=501,
                filesystem=mount_point,
                filesystem_family=FilesystemFamily.NFS,
                limit_semantics="unknown",
                mount_point=mount_point,
                used_bytes=1024,
                soft_limit_bytes=2048,
                hard_limit_bytes=4096,
                files_used=1,
                file_soft_limit=2,
                file_hard_limit=4,
                block_over_limit=False,
                file_over_limit=False,
            )
        ],
    )


def _nfs_volumes(protocol_version: str, mount_point: str = "/Volumes/Shared Models") -> VolumesResponse:
    return VolumesResponse(
        sampled_at=NOW,
        status=CapabilityStatus.AVAILABLE,
        source="test-volumes",
        items=[
            Volume(
                id="nfs-volume",
                name="Shared Models",
                mount_point=mount_point,
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
        ],
    )


@pytest.mark.parametrize(
    ("protocol_version", "expected"),
    [
        ("4", "remaining_availability"),
        ("4.1", "remaining_availability"),
        ("3", "absolute_limit"),
        ("2.0", "absolute_limit"),
        ("3-4", "unknown"),
        ("NFSv4.1", "unknown"),
    ],
)
def test_reconciles_generic_nfs_only_from_an_exact_negotiated_version(
    protocol_version: str, expected: str
) -> None:
    original = _unknown_nfs_quota()

    result = reconcile_quota_semantics(original, _nfs_volumes(protocol_version))

    assert result.items[0].limit_semantics == expected
    assert original.items[0].limit_semantics == "unknown"
    assert result is not original
    if expected == "remaining_availability":
        assert "NFSv4 quota availability" in (result.message or "")
    else:
        assert "NFSv4 quota availability" not in (result.message or "")


def test_reconciliation_requires_an_exact_mountpoint_match() -> None:
    original = _unknown_nfs_quota("/Volumes/Models")

    result = reconcile_quota_semantics(
        original, _nfs_volumes("4.1", "/Volumes/Models-Archive")
    )

    assert result.items[0].limit_semantics == "unknown"
