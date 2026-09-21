"""Regression tests for the startup update check.

PR #8 hardened this path (malformed-cache tolerance, failure backoff, atomic
writes); PR #11 rewrote the module from a pre-#8 base and silently dropped all
of it along with the guarding tests. See commits 31bbd86 and 002c50f.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from heyamara_cli import version_check
from heyamara_cli.commands import update as update_cmd


class UpdateCheckCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.addCleanup(setattr, version_check, "CACHE_DIR", version_check.CACHE_DIR)
        self.addCleanup(setattr, version_check, "CACHE_FILE", version_check.CACHE_FILE)

        self.cache_dir = Path(self.temp_dir.name) / ".heyamara-cache"
        self.cache_file = self.cache_dir / ".update-check"
        version_check.CACHE_DIR = self.cache_dir
        version_check.CACHE_FILE = self.cache_file

    def _notify(self, current="1.8.5", latest=""):
        with mock.patch(
            "heyamara_cli.version_check.importlib.metadata.version",
            return_value=current,
        ):
            with mock.patch(
                "heyamara_cli.version_check._fetch_latest_version", return_value=latest
            ) as fetch:
                with mock.patch("heyamara_cli.version_check.click.secho"):
                    version_check.check_and_notify()
        return fetch

    def test_backs_off_after_failed_fetch(self):
        """A failed probe is cached, so the next command does not re-pay for it."""
        with mock.patch(
            "heyamara_cli.version_check.importlib.metadata.version",
            return_value="1.8.5",
        ):
            with mock.patch(
                "heyamara_cli.version_check._fetch_latest_version", return_value=""
            ) as fetch:
                version_check.check_and_notify()
                version_check.check_and_notify()

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(version_check._read_cache()["latest"], "1.8.5")
        self.assertTrue(version_check._read_cache()["failed"])

    def test_failed_cache_retries_after_backoff_window(self):
        """A cached failure is retried once the short retry interval elapses."""
        self._notify()

        stale = version_check._read_cache()
        stale["checked_at"] -= version_check.FAILURE_RETRY_INTERVAL + 1
        self.cache_file.write_text(json.dumps(stale))

        fetch = self._notify(latest="1.9.0")
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(version_check._read_cache()["failed"])

    def test_successful_cache_is_not_retried_within_24h(self):
        fetch = self._notify(latest="1.9.0")
        self.assertEqual(fetch.call_count, 1)
        fetch = self._notify(latest="1.9.0")
        self.assertEqual(fetch.call_count, 0)

    def test_ignores_malformed_cache_files(self):
        """Malformed cache is a cache miss, never a traceback on every command."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        malformed_payloads = [
            b"[]",
            b'{"checked_at": "oops", "latest": "9.9.9"}',
            b'{"checked_at": true, "latest": "9.9.9"}',
            b"\xff\xfe\x00",
            b'{"latest": "1.8.6", "checke',
        ]

        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                self.cache_file.write_bytes(payload)
                self.assertEqual(version_check._read_cache(), {})
                self._notify()

    def test_cache_without_timestamp_is_treated_as_a_miss(self):
        """A missing checked_at normalizes to 0, so the probe still re-runs."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text('{"latest": "9.9.9"}')

        self.assertFalse(version_check._read_cache().get("checked_at"))
        fetch = self._notify(latest="1.9.0")
        self.assertEqual(fetch.call_count, 1)

    def test_write_failure_leaves_existing_cache_intact(self):
        """The write is atomic: an interrupted probe cannot truncate a good cache."""
        version_check._write_cache("1.9.0")
        good = self.cache_file.read_text()

        with mock.patch("heyamara_cli.version_check.os.replace", side_effect=OSError):
            version_check._write_cache("2.0.0")

        self.assertEqual(self.cache_file.read_text(), good)
        self.assertEqual(list(self.cache_dir.glob(".*tmp")), [])


class UpdateCommandTimeoutTests(unittest.TestCase):
    def test_version_lookup_is_bounded(self):
        """`heyamara update` must not block forever resolving the latest release."""
        with mock.patch.object(
            update_cmd, "fetch_latest_release_version", return_value="1.9.0"
        ) as fetch:
            update_cmd._get_latest_version()

        self.assertEqual(fetch.call_count, 1)
        timeout = fetch.call_args.kwargs.get("timeout")
        self.assertIsNotNone(timeout, "update lookup passed no timeout")
        self.assertGreater(timeout, 0)


if __name__ == "__main__":
    unittest.main()
