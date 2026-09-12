from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import demo_workload


class DemoWorkloadTests(unittest.TestCase):
    def test_generate_and_cleanup_exact_marked_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.gguf"
            with (
                patch.object(demo_workload.os, "fsync") as fsync,
                redirect_stdout(io.StringIO()),
            ):
                demo_workload.generate(
                    target,
                    size_mb=0.03,
                    chunk_mb=0.01,
                    delay_ms=0,
                    fsync_every_chunks=2,
                )

            self.assertEqual(target.stat().st_size, int(0.03 * demo_workload.MIB))
            self.assertEqual(
                target.read_bytes()[: len(demo_workload.MARKER)],
                demo_workload.MARKER,
            )
            # Three writes produce one cadence fsync plus the final fsync.
            self.assertEqual(fsync.call_count, 2)

            with redirect_stdout(io.StringIO()):
                demo_workload.cleanup(target)
            self.assertFalse(target.exists())

    def test_cleanup_refuses_unmarked_file_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "important.gguf"
            target.write_bytes(b"not a LocalTrace demo file")

            with self.assertRaisesRegex(ValueError, "not created by this script"):
                demo_workload.cleanup(target)
            self.assertEqual(target.read_bytes(), b"not a LocalTrace demo file")

    def test_generate_refuses_existing_file_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.gguf"
            original = b"keep me"
            target.write_bytes(original)

            with self.assertRaises(FileExistsError):
                demo_workload.generate(target, 1, 1, 0)
            self.assertEqual(target.read_bytes(), original)

    def test_cleanup_rejects_non_gguf_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "must end in .gguf"):
            demo_workload.validate_target(Path("/tmp/not-a-demo.txt"))

    def test_generate_refuses_symbolic_link_demo_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "destination"
            destination.mkdir()
            linked_directory = root / "localtrace-demo"
            linked_directory.symlink_to(destination, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "demo directory"):
                demo_workload.generate(
                    linked_directory / "model.gguf",
                    size_mb=0.03,
                    chunk_mb=0.01,
                    delay_ms=0,
                )
            self.assertFalse((destination / "model.gguf").exists())


if __name__ == "__main__":
    unittest.main()
