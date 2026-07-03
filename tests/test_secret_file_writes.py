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
from heyamara_cli.secret_files import UnsafeSecretFileError, ensure_private_dir, write_secret_text


class SecretFileWriteTests(unittest.TestCase):
    def _symlink_or_skip(self, target: Path, link: Path) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink support unavailable")
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def test_ensure_private_dir_creates_private_parent_for_secret_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "secret.env"

            ensure_private_dir(output.parent)
            write_secret_text(output, "TOKEN=secret", trailing_newline=True)

            self.assertEqual(output.read_text(), "TOKEN=secret\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)

    def test_ensure_private_dir_tolerates_concurrent_directory_creation(self):
        if not secret_files._can_walk_with_dir_fd():
            self.skipTest("directory fd creation path unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "nested"
            original_mkdir = secret_files.os.mkdir
            simulated_race = {"done": False}

            def racing_mkdir(path, mode=0o777, *, dir_fd=None):
                if path == "nested" and dir_fd is not None and not simulated_race["done"]:
                    simulated_race["done"] = True
                    original_mkdir(path, mode, dir_fd=dir_fd)
                    raise FileExistsError("simulated concurrent mkdir")
                return original_mkdir(path, mode, dir_fd=dir_fd)

            with mock.patch.object(secret_files.os, "mkdir", racing_mkdir):
                ensure_private_dir(target)

            self.assertTrue(target.is_dir())

    def test_write_secret_text_requires_existing_parent_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "missing" / "secret.env"

            with self.assertRaises(UnsafeSecretFileError):
                write_secret_text(output, "TOKEN=secret", trailing_newline=True)

            self.assertFalse(output.exists())

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

    def test_write_secret_text_reports_post_replace_directory_sync_failure(self):
        if not secret_files._can_walk_with_dir_fd() or not secret_files._SUPPORTS_RENAME_DIR_FD:
            self.skipTest("directory fd rename path unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "secret.env"
            output.write_text("ORIGINAL=1\n")
            original_fsync = secret_files.os.fsync

            def failing_directory_fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("directory sync failed")
                return original_fsync(fd)

            with mock.patch.object(secret_files.os, "fsync", failing_directory_fsync):
                with self.assertRaises(UnsafeSecretFileError) as cm:
                    write_secret_text(output, "TOKEN=secret", trailing_newline=True)

            self.assertIn("was written", str(cm.exception))
            self.assertIn("directory sync failed", str(cm.exception))
            self.assertEqual(output.read_text(), "TOKEN=secret\n")
            self.assertFalse(list(Path(temp_dir).glob(".*.tmp")))

    def test_windows_reparse_points_are_treated_as_unsafe_links(self):
        class FakeReparsePoint:
            def is_symlink(self):
                return False

            def is_junction(self):
                return False

            def lstat(self):
                return type("Stat", (), {"st_file_attributes": 0x400})()

        with mock.patch.object(secret_files.os, "name", "nt"), mock.patch.object(
            secret_files.stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
            create=True,
        ):
            self.assertTrue(secret_files._is_unsafe_link_component(FakeReparsePoint()))

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

    def test_write_secret_text_refuses_relative_output_from_symlinked_logical_cwd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            real_dir = Path(temp_dir) / "real"
            link_dir = Path(temp_dir) / "link"
            real_dir.mkdir()
            self._symlink_or_skip(real_dir, link_dir)

            old_cwd = Path.cwd()
            old_pwd = os.environ.get("PWD")
            os.chdir(link_dir)
            os.environ["PWD"] = str(link_dir)
            try:
                with self.assertRaises(UnsafeSecretFileError) as cm:
                    write_secret_text("secret.env", "TOKEN=secret", trailing_newline=True)
            finally:
                os.chdir(old_cwd)
                if old_pwd is None:
                    os.environ.pop("PWD", None)
                else:
                    os.environ["PWD"] = old_pwd

            self.assertIn("Refusing to write secret file through symlink", str(cm.exception))
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
