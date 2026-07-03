import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heyamara_cli.secret_files as secret_files
from heyamara_cli.commands.connect import _write_env_for
from heyamara_cli.secret_files import UnsafeSecretFileError, write_secret_text


class SecretFileWriteTests(unittest.TestCase):
    def _symlink_or_skip(self, target: Path, link: Path) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink support unavailable")
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def test_write_secret_text_creates_private_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "secret.env"

            write_secret_text(output, "TOKEN=secret", trailing_newline=True)

            self.assertEqual(output.read_text(), "TOKEN=secret\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)

    def test_write_secret_text_replaces_existing_broad_file_privately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "secret.env"
            output.write_text("OLD=1\n")
            if os.name != "nt":
                os.chmod(output, 0o644)

            write_secret_text(output, "TOKEN=secret", trailing_newline=True)

            self.assertEqual(output.read_text(), "TOKEN=secret\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_write_secret_text_sets_private_mode_before_writing(self):
        if os.name == "nt":
            self.skipTest("POSIX fd mode check is not meaningful on Windows")

        observed_modes = []
        original_fdopen = secret_files.os.fdopen

        def checking_fdopen(fd, *args, **kwargs):
            observed_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return original_fdopen(fd, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "secret.env"
            with mock.patch.object(secret_files.os, "fdopen", checking_fdopen):
                write_secret_text(output, "TOKEN=secret", trailing_newline=True)

        self.assertEqual(observed_modes, [0o600])

    def test_write_secret_text_preserves_existing_file_if_atomic_replace_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "secret.env"
            output.write_text("ORIGINAL=1\n")
            if os.name != "nt":
                os.chmod(output, 0o600)

            with mock.patch.object(
                secret_files,
                "_replace_from_temp",
                side_effect=OSError("boom"),
            ):
                with self.assertRaises(OSError):
                    write_secret_text(output, "TOKEN=secret", trailing_newline=True)

            self.assertEqual(output.read_text(), "ORIGINAL=1\n")
            self.assertFalse(list(Path(temp_dir).glob(".*.tmp")))

    def test_write_secret_text_refuses_symlink_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target.env"
            link = Path(temp_dir) / "link.env"
            target.write_text("SAFE=1")
            self._symlink_or_skip(target, link)

            with self.assertRaises(UnsafeSecretFileError):
                write_secret_text(link, "TOKEN=secret", trailing_newline=True)

            self.assertEqual(target.read_text(), "SAFE=1")

    def test_write_secret_text_refuses_non_regular_target(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("fifo support unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "secret.env"
            try:
                os.mkfifo(output)
            except OSError as exc:
                self.skipTest(f"fifo creation unavailable: {exc}")

            with self.assertRaises(UnsafeSecretFileError):
                write_secret_text(output, "TOKEN=secret", trailing_newline=True)

    def test_write_secret_text_refuses_symlink_parent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            real_dir = Path(temp_dir) / "real"
            link_dir = Path(temp_dir) / "link"
            real_dir.mkdir()
            self._symlink_or_skip(real_dir, link_dir)

            with self.assertRaises(UnsafeSecretFileError):
                write_secret_text(link_dir / "secret.env", "TOKEN=secret", trailing_newline=True)

            self.assertFalse((real_dir / "secret.env").exists())

    def test_write_secret_text_allows_trusted_platform_tmp_alias(self):
        tmp_path = Path("/tmp")
        try:
            is_trusted_alias = secret_files._is_trusted_platform_symlink(tmp_path)
        except OSError:
            is_trusted_alias = False
        if not is_trusted_alias:
            self.skipTest("/tmp is not a trusted top-level platform alias on this host")

        output = tmp_path / f"heyamara-secret-test-{os.getpid()}-{id(self)}.env"
        try:
            write_secret_text(output, "TOKEN=secret", trailing_newline=True)
            self.assertEqual(output.read_text(), "TOKEN=secret\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        finally:
            output.unlink(missing_ok=True)

    def test_connect_env_for_output_is_private_and_does_not_echo_secret_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / ".env.local"
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                _write_env_for(
                    str(output),
                    "DATABASE_URL=postgres://example\nSECRET_TOKEN=super-secret\n",
                    ["DATABASE_URL"],
                    "ats-backend",
                    5432,
                )

            self.assertIn("DATABASE_URL=postgres://example", output.read_text())
            command_output = stdout.getvalue()
            self.assertIn("✓ Wrote", command_output)
            self.assertNotIn("postgres://example", command_output)
            self.assertNotIn("super-secret", command_output)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_connect_env_for_refuses_symlink_output_without_overwriting_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target.env"
            link = Path(temp_dir) / "link.env"
            target.write_text("SAFE=1")
            self._symlink_or_skip(target, link)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as cm:
                    _write_env_for(
                        str(link),
                        "DATABASE_URL=postgres://example\nSECRET_TOKEN=super-secret\n",
                        ["DATABASE_URL"],
                        "ats-backend",
                        5432,
                    )

            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(target.read_text(), "SAFE=1")
            command_output = stdout.getvalue() + stderr.getvalue()
            self.assertIn("Refusing to write secret file through symlink", command_output)
            self.assertNotIn("postgres://example", command_output)
            self.assertNotIn("super-secret", command_output)


if __name__ == "__main__":
    unittest.main()
