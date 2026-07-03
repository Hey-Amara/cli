import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

from heyamara_cli.commands import env as env_module


class EnvSecretOutputTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _symlink_or_skip(self, target: Path, link: Path) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink support unavailable")
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def _invoke_with_fake_ssm(self, args, *, content="TOKEN=secret"):
        def fake_get_ssm_param(*_args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout=content, stderr="")

        patches = [
            mock.patch.object(env_module, "_resolve", return_value=("profile", "ap-southeast-2")),
            mock.patch.object(env_module, "require_aws_session", lambda _profile: None),
            mock.patch.object(env_module, "_get_ssm_param", fake_get_ssm_param),
        ]
        with patches[0], patches[1], patches[2]:
            return self.runner.invoke(env_module.env, args)

    def _combined_output(self, result):
        try:
            stderr = result.stderr or ""
        except ValueError:
            stderr = ""
        if stderr and stderr not in result.output:
            return result.output + stderr
        return result.output

    def test_env_pull_writes_private_output_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "service.env"
            old_umask = os.umask(0)
            try:
                result = self._invoke_with_fake_ssm(
                    ["pull", "ats-backend", "staging", "-o", str(output)],
                    content="TOKEN=secret",
                )
            finally:
                os.umask(old_umask)

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(output.read_text(), "TOKEN=secret\n")
            self.assertNotIn("secret", result.output)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_env_pull_refuses_symlink_output_without_overwriting_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target.env"
            link = Path(temp_dir) / "link.env"
            target.write_text("SAFE=1")
            self._symlink_or_skip(target, link)

            result = self._invoke_with_fake_ssm(
                ["pull", "ats-backend", "staging", "-o", str(link)],
                content="TOKEN=leaked-value",
            )

            combined = self._combined_output(result)
            self.assertEqual(result.exit_code, 1, combined)
            self.assertIn("Refusing to write secret file through symlink", combined)
            self.assertNotIn("leaked-value", combined)
            self.assertNotIn("Traceback", combined)
            self.assertEqual(target.read_text(), "SAFE=1")

    def test_env_pull_refuses_default_relative_output_from_symlinked_logical_cwd(self):
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
                result = self._invoke_with_fake_ssm(
                    ["pull", "ats-backend", "staging"],
                    content="TOKEN=leaked-value",
                )
            finally:
                os.chdir(old_cwd)
                if old_pwd is None:
                    os.environ.pop("PWD", None)
                else:
                    os.environ["PWD"] = old_pwd

            combined = self._combined_output(result)
            self.assertEqual(result.exit_code, 1, combined)
            self.assertIn("Refusing to write secret file through symlink", combined)
            self.assertNotIn("leaked-value", combined)
            self.assertNotIn("Traceback", combined)
            self.assertFalse((real_dir / "ats-backend.staging.env").exists())

    def test_env_pull_all_writes_private_directory_and_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "env-files"
            old_umask = os.umask(0)
            try:
                with mock.patch.object(env_module, "SERVICES", ["ats-backend", "ae-backend"]):
                    result = self._invoke_with_fake_ssm(
                        ["pull-all", "staging", "-d", str(output_dir)],
                        content="TOKEN=secret",
                    )
            finally:
                os.umask(old_umask)

            self.assertEqual(result.exit_code, 0, result.output)
            backend_file = output_dir / "ats-backend.staging.env"
            ae_file = output_dir / "ae-backend.staging.env"
            self.assertEqual(backend_file.read_text(), "TOKEN=secret\n")
            self.assertEqual(ae_file.read_text(), "TOKEN=secret\n")
            self.assertNotIn("secret", result.output)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(backend_file.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(ae_file.stat().st_mode), 0o600)

    def test_env_pull_all_does_not_chmod_existing_output_directory(self):
        if os.name == "nt":
            self.skipTest("POSIX directory modes are not meaningful on Windows")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "existing-env-files"
            output_dir.mkdir()
            os.chmod(output_dir, 0o755)

            with mock.patch.object(env_module, "SERVICES", ["ats-backend"]):
                result = self._invoke_with_fake_ssm(
                    ["pull-all", "staging", "-d", str(output_dir)],
                    content="TOKEN=secret",
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o755)
            output_file = output_dir / "ats-backend.staging.env"
            self.assertEqual(output_file.read_text(), "TOKEN=secret\n")
            self.assertEqual(stat.S_IMODE(output_file.stat().st_mode), 0o600)

    def test_env_pull_reports_invalid_parent_without_traceback_or_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent_file = Path(temp_dir) / "not-a-directory"
            parent_file.write_text("blocker")

            result = self._invoke_with_fake_ssm(
                ["pull", "ats-backend", "staging", "-o", str(parent_file / "secret.env")],
                content="TOKEN=leaked-value",
            )

            combined = self._combined_output(result)
            self.assertEqual(result.exit_code, 1, combined)
            self.assertIn("Could not", combined)
            self.assertIn("secret output", combined)
            self.assertNotIn("leaked-value", combined)
            self.assertNotIn("Traceback", combined)

    def test_env_pull_all_reports_file_output_dir_without_traceback_or_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "not-a-directory"
            output_dir.write_text("blocker")

            with mock.patch.object(env_module, "SERVICES", ["ats-backend"]):
                result = self._invoke_with_fake_ssm(
                    ["pull-all", "staging", "-d", str(output_dir)],
                    content="TOKEN=leaked-value",
                )

            combined = self._combined_output(result)
            self.assertEqual(result.exit_code, 1, combined)
            self.assertIn("Could not", combined)
            self.assertIn("secret output", combined)
            self.assertNotIn("leaked-value", combined)
            self.assertNotIn("Traceback", combined)

    def test_env_pull_all_refuses_symlink_output_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            real_dir = Path(temp_dir) / "real"
            link_dir = Path(temp_dir) / "link"
            real_dir.mkdir()
            self._symlink_or_skip(real_dir, link_dir)

            with mock.patch.object(env_module, "SERVICES", ["ats-backend"]):
                result = self._invoke_with_fake_ssm(
                    ["pull-all", "staging", "-d", str(link_dir)],
                    content="TOKEN=leaked-value",
                )

            combined = self._combined_output(result)
            self.assertEqual(result.exit_code, 1, combined)
            self.assertIn("Refusing to write secret file through symlink", combined)
            self.assertFalse((real_dir / "ats-backend.staging.env").exists())
            self.assertNotIn("leaked-value", combined)
            self.assertNotIn("Traceback", combined)


if __name__ == "__main__":
    unittest.main()
