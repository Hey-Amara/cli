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

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("Refusing to write secret file through symlink", result.output)
            self.assertNotIn("leaked-value", result.output)
            self.assertEqual(target.read_text(), "SAFE=1")

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
            self.assertEqual((output_dir / "ats-backend.staging.env").read_text(), "TOKEN=secret\n")
            self.assertEqual((output_dir / "ae-backend.staging.env").read_text(), "TOKEN=secret\n")
            self.assertNotIn("secret", result.output)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((output_dir / "ats-backend.staging.env").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((output_dir / "ae-backend.staging.env").stat().st_mode), 0o600)

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

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("Refusing to write secret file through symlink", result.output)
            self.assertFalse((real_dir / "ats-backend.staging.env").exists())
            self.assertNotIn("leaked-value", result.output)


if __name__ == "__main__":
    unittest.main()
