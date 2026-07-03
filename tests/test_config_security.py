import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from heyamara_cli import config
from heyamara_cli.commands.config_cmd import config_cmd
from heyamara_cli.main import cli


class ConfigSecurityTests(unittest.TestCase):
    def setUp(self):
        self._original_config_dir = config.CONFIG_DIR
        self._original_config_file = config.CONFIG_FILE
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self._restore_config_paths)

        config.CONFIG_DIR = Path(self.temp_dir.name) / ".heyamara"
        config.CONFIG_FILE = config.CONFIG_DIR / "config.json"
        self.runner = CliRunner()

    def _restore_config_paths(self):
        config.CONFIG_DIR = self._original_config_dir
        config.CONFIG_FILE = self._original_config_file

    def _symlink_or_skip(self, target: Path, link: Path) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink support unavailable")
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def test_config_set_masks_grafana_token_persists_raw_value_and_writes_private_file(self):
        token = "grafana-secret-token-123456"

        result = self.runner.invoke(config_cmd, ["set", "grafana_token", token])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(token, result.output)
        self.assertIn("grafana_token = ********3456", result.output)
        self.assertTrue(config.CONFIG_FILE.exists())
        self.assertEqual(config.load_user_config()["grafana_token"], token)

        if os.name != "nt":
            config_dir_mode = stat.S_IMODE(config.CONFIG_DIR.stat().st_mode)
            config_file_mode = stat.S_IMODE(config.CONFIG_FILE.stat().st_mode)
            self.assertEqual(config_dir_mode, 0o700)
            self.assertEqual(config_file_mode, 0o600)

    def test_config_get_masks_single_grafana_token_value(self):
        token = "grafana-secret-token-abcdef"
        config.save_user_config({**config.DEFAULTS, "grafana_token": token})

        result = self.runner.invoke(config_cmd, ["get", "grafana_token"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(token, result.output)
        self.assertIn("grafana_token = ********cdef", result.output)

    def test_config_get_single_unset_grafana_token_preserves_empty_output(self):
        config.save_user_config({**config.DEFAULTS, "grafana_token": ""})

        result = self.runner.invoke(config_cmd, ["get", "grafana_token"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, "grafana_token = \n")

    def test_config_get_all_masks_short_grafana_token(self):
        token = "abcd"
        config.save_user_config({**config.DEFAULTS, "grafana_token": token})

        result = self.runner.invoke(config_cmd, ["get"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(f"grafana_token = {token}", result.output)
        self.assertIn("grafana_token = ********", result.output)

    def test_config_get_masks_manually_edited_non_string_token(self):
        config.CONFIG_DIR.mkdir(parents=True)
        config.CONFIG_FILE.write_text('{"grafana_token": 123456}\n')

        result = self.runner.invoke(config_cmd, ["get", "grafana_token"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("123456", result.output)
        self.assertIn("grafana_token = ********3456", result.output)

    def test_config_get_all_marks_unset_secret_without_leaking_default_marker_to_single_get(self):
        config.save_user_config({**config.DEFAULTS, "grafana_token": ""})

        result = self.runner.invoke(config_cmd, ["get"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("grafana_token = (not set)", result.output)

    def test_config_set_refuses_symlink_config_file_without_overwriting_target(self):
        token = "grafana-secret-token-123456"
        config.CONFIG_DIR.mkdir(parents=True)
        target = Path(self.temp_dir.name) / "target.json"
        target.write_text('{"grafana_token": "safe"}\n')
        self._symlink_or_skip(target, config.CONFIG_FILE)

        result = self.runner.invoke(config_cmd, ["set", "grafana_token", token])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Refusing to write secret file through symlink", result.output)
        self.assertIn("regular output file", result.output)
        self.assertNotIn(token, result.output)
        self.assertEqual(target.read_text(), '{"grafana_token": "safe"}\n')

    def test_switch_refuses_symlink_config_file_without_traceback(self):
        config.CONFIG_DIR.mkdir(parents=True)
        target = Path(self.temp_dir.name) / "target.json"
        target.write_text('{"aws_profile": "safe"}\n')
        self._symlink_or_skip(target, config.CONFIG_FILE)

        with mock.patch("heyamara_cli.version_check.check_and_notify", lambda: None):
            result = self.runner.invoke(cli, ["switch", "new-profile"])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Refusing to write secret file through symlink", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(target.read_text(), '{"aws_profile": "safe"}\n')


if __name__ == "__main__":
    unittest.main()
