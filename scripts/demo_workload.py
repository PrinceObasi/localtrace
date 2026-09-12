#!/usr/bin/env python3
"""Generate a bounded, recognizable storage workload for the LocalTrace demo.

The file resembles an AI model only by its ``.gguf`` extension. It contains no
model data. Cleanup is deliberately conservative: it requires an explicit path
and removes the file only when the LocalTrace marker is present.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time


MARKER = b"LOCALTRACE_DEMO_GGUF\x00v1\n"
MIB = 1024 * 1024
MAX_SIZE_MIB = 4096
MAX_CHUNK_MIB = 64
MAX_FSYNC_INTERVAL_CHUNKS = 1024
MIN_FREE_BYTES_AFTER_DEMO = 512 * MIB
DEFAULT_TARGET = (
    Path(tempfile.gettempdir()) / "localtrace-demo" / "localtrace-demo-model.gguf"
)


def positive_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return number


def nonnegative_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("value cannot be negative")
    return number


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    if number > MAX_FSYNC_INTERVAL_CHUNKS:
        raise argparse.ArgumentTypeError(
            f"value cannot exceed {MAX_FSYNC_INTERVAL_CHUNKS}"
        )
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        help=f"output path (generation default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--size-mb",
        type=positive_number,
        default=256,
        help="total file size in MiB (default: 256)",
    )
    parser.add_argument(
        "--chunk-mb",
        type=positive_number,
        default=4,
        help="write size in MiB (default: 4)",
    )
    parser.add_argument(
        "--delay-ms",
        type=nonnegative_number,
        default=100,
        help="pause between writes in milliseconds (default: 100)",
    )
    parser.add_argument(
        "--fsync-every-chunks",
        type=positive_integer,
        default=1,
        help="flush writes to storage after this many chunks (default: 1)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="remove a marked demo file; requires an explicit --target",
    )
    return parser


def validate_target(target: Path) -> Path:
    target = target.expanduser().absolute()
    if target.suffix.lower() != ".gguf":
        raise ValueError("demo target must end in .gguf")
    if target.is_symlink():
        raise ValueError("refusing to use a symbolic link as the demo target")
    return target


def cleanup(target: Path) -> None:
    if not target.exists():
        print(f"Nothing to clean: {target}")
        return
    if not target.is_file() or target.is_symlink():
        raise ValueError("refusing to remove anything except a regular file")
    with target.open("rb") as demo_file:
        if demo_file.read(len(MARKER)) != MARKER:
            raise ValueError("refusing to remove a file not created by this script")
    target.unlink()
    print(f"Removed verified LocalTrace demo file: {target}")


def generate(
    target: Path,
    size_mb: float,
    chunk_mb: float,
    delay_ms: float,
    fsync_every_chunks: int = 1,
) -> None:
    if size_mb > MAX_SIZE_MIB:
        raise ValueError(f"--size-mb cannot exceed {MAX_SIZE_MIB}")
    if chunk_mb > MAX_CHUNK_MIB:
        raise ValueError(f"--chunk-mb cannot exceed {MAX_CHUNK_MIB}")
    total_bytes = max(len(MARKER), int(size_mb * MIB))
    chunk_bytes = max(len(MARKER), int(chunk_mb * MIB))
    if target.parent.is_symlink():
        raise ValueError("refusing to use a symbolic link as the demo directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise ValueError("refusing to use a symbolic link as the demo directory")
    if target.exists() or target.is_symlink():
        raise FileExistsError(
            f"target already exists: {target}\n"
            "Run --cleanup with an explicit --target, or choose another path."
        )
    free_bytes = shutil.disk_usage(target.parent).free
    if total_bytes + MIN_FREE_BYTES_AFTER_DEMO > free_bytes:
        raise ValueError(
            "not enough free space to create the demo while preserving "
            f"{MIN_FREE_BYTES_AFTER_DEMO / MIB:.0f} MiB"
        )

    # A deterministic, non-sparse payload exercises real writes without reading
    # user data or spending CPU time generating random bytes.
    payload = (b"LOCALTRACE-DEMO-DATA\n" * ((chunk_bytes // 21) + 1))[:chunk_bytes]
    written = 0
    chunks_written = 0
    started = time.monotonic()

    created = False
    try:
        # Exclusive creation prevents accidental overwrite and symlink races.
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as demo_file:
            demo_file.write(MARKER)
            written = len(MARKER)
            while written < total_bytes:
                amount = min(len(payload), total_bytes - written)
                demo_file.write(payload[:amount])
                demo_file.flush()
                written += amount
                chunks_written += 1
                if chunks_written % fsync_every_chunks == 0:
                    # Periodic fsync makes the physical-device counter react
                    # during the demo instead of deferring all I/O until close.
                    os.fsync(demo_file.fileno())
                percent = (written / total_bytes) * 100
                print(
                    f"\rWriting {target.name}: {written / MIB:,.1f} MiB "
                    f"({percent:5.1f}%)",
                    end="",
                    flush=True,
                )
                if delay_ms:
                    time.sleep(delay_ms / 1000)
            os.fsync(demo_file.fileno())
    except BaseException:
        # Remove only the exact file this invocation exclusively created.
        if created and target.exists() and not target.is_symlink():
            try:
                with target.open("rb") as partial:
                    owned = partial.read(len(MARKER)) == MARKER
                if owned:
                    target.unlink()
            except OSError:
                pass
        raise

    elapsed = max(time.monotonic() - started, 0.001)
    print(
        f"\nCreated {target} ({written / MIB:,.1f} MiB in {elapsed:.1f}s; "
        f"average {written / MIB / elapsed:,.1f} MiB/s)"
    )
    print("Leave it in place for inspection, then run `make demo-clean`.")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.cleanup and args.target is None:
        parser.error("--cleanup requires an explicit --target")

    try:
        target = validate_target(args.target or DEFAULT_TARGET)
        if args.cleanup:
            cleanup(target)
        else:
            generate(
                target,
                args.size_mb,
                args.chunk_mb,
                args.delay_ms,
                args.fsync_every_chunks,
            )
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
