import os
import stat
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from heyamara_cli import config
from heyamara_cli.commands.config_cmd import config_cmd


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

    def test_config_set_masks_grafana_token_and_writes_private_file(self):
        token = "grafana-secret-token-123456"

        result = self.runner.invoke(config_cmd, ["set", "grafana_token", token])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(token, result.output)
        self.assertIn("grafana_token = ********3456", result.output)
        self.assertTrue(config.CONFIG_FILE.exists())

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

    def test_config_get_all_masks_short_grafana_token(self):
        token = "abcd"
        config.save_user_config({**config.DEFAULTS, "grafana_token": token})

        result = self.runner.invoke(config_cmd, ["get"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(f"grafana_token = {token}", result.output)
        self.assertIn("grafana_token = ********", result.output)


if __name__ == "__main__":
    unittest.main()
