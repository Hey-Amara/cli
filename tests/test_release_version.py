import subprocess
import unittest
from unittest import mock

from heyamara_cli.release_version import (
    canonical_release_version,
    fetch_latest_release_version,
    parse_git_tag_output,
)


ANNOTATED_TAGS = """\
aaaaaaaa\trefs/tags/v1.8.4^{}
bbbbbbbb\trefs/tags/v1.8.4
cccccccc\trefs/tags/v1.8.3^{}
dddddddd\trefs/tags/v1.8.3
"""


class ReleaseVersionTests(unittest.TestCase):
    def test_parser_ignores_peeled_annotated_tag_refs(self):
        self.assertEqual(parse_git_tag_output(ANNOTATED_TAGS), "1.8.4")

    def test_parser_prefers_latest_stable_version_over_prereleases(self):
        output = "\n".join(
            [
                "aaaaaaaa\trefs/tags/v2.0.0-rc.10",
                "bbbbbbbb\trefs/tags/v2.0.0-rc.2",
                "cccccccc\trefs/tags/v2.0.0",
                "dddddddd\trefs/tags/v1.9.0",
            ]
        )

        self.assertEqual(parse_git_tag_output(output), "2.0.0")

    def test_parser_selects_semver_max_instead_of_trusting_input_order(self):
        output = "\n".join(
            [
                "aaaaaaaa\trefs/tags/v1.9.9",
                "bbbbbbbb\trefs/tags/v2.0.0",
                "cccccccc\trefs/tags/v1.10.0",
            ]
        )

        self.assertEqual(parse_git_tag_output(output), "2.0.0")

    def test_parser_does_not_offer_prerelease_only_tags(self):
        self.assertEqual(
            parse_git_tag_output("aaaaaaaa\trefs/tags/v2.0.0-rc.1"),
            "",
        )

    def test_canonical_version_requires_v_prefixed_stable_semver(self):
        self.assertEqual(canonical_release_version("v1.2.3"), "1.2.3")
        for invalid in (
            "1.2.3",
            "v1.2.3-rc.1",
            "v1.2.3_",
            "v1.2.3+foo+bar",
            "v1.2.3foo",
        ):
            with self.subTest(tag=invalid):
                self.assertEqual(canonical_release_version(invalid), "")

    def test_canonical_version_rejects_non_canonical_leading_zero_components(self):
        # Leading zeros are not canonical SemVer; accepting them let a bogus
        # `v010.0.0` parse to (10, 0, 0) and outrank a real `v1.9.0`.
        for invalid in ("v01.2.3", "v1.02.3", "v1.2.03", "v010.0.0"):
            with self.subTest(tag=invalid):
                self.assertEqual(canonical_release_version(invalid), "")

    def test_parser_ignores_leading_zero_tags_and_keeps_the_canonical_max(self):
        output = "\n".join(
            [
                "aaaaaaaa\trefs/tags/v1.9.0",
                "bbbbbbbb\trefs/tags/v010.0.0",
            ]
        )

        self.assertEqual(parse_git_tag_output(output), "1.9.0")

    def test_canonical_version_rejects_unicode_digit_tags(self):
        # `[0-9]` (not `\d`) must keep Unicode decimal digits out; int() would
        # otherwise happily parse them into a bogus version tuple.
        self.assertEqual(canonical_release_version("v١.٢.٣"), "")
        self.assertEqual(parse_git_tag_output("aaaaaaaa\trefs/tags/v١.٢.٣"), "")

    def test_parser_does_not_crash_on_pathologically_long_numeric_tag(self):
        # A >4300-digit numeric component would blow int()'s conversion limit and
        # crash the resolver; the bounded pattern must simply skip such a tag.
        huge = "v" + ("9" * 5000) + ".0.0"
        output = "\n".join(
            [
                f"aaaaaaaa\trefs/tags/{huge}",
                "bbbbbbbb\trefs/tags/v1.7.0",
            ]
        )

        self.assertEqual(parse_git_tag_output(output), "1.7.0")
        self.assertEqual(canonical_release_version(huge), "")

    @mock.patch("heyamara_cli.release_version.shutil.which", return_value=None)
    @mock.patch("heyamara_cli.release_version.subprocess.run")
    def test_fetch_fails_soft_to_empty_on_pathologically_long_tag(
        self, run_mock, _which_mock
    ):
        # The public entry point must degrade to "" (no update offered), never
        # propagate a ValueError out of a malformed upstream tag.
        huge = "v" + ("9" * 5000) + ".0.0"
        run_mock.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"aaaaaaaa\trefs/tags/{huge}\n", stderr=""
        )

        self.assertEqual(fetch_latest_release_version("Hey-Amara/cli", timeout=5), "")

    def test_parser_skips_malformed_and_non_version_tags(self):
        output = "\n".join(
            [
                "not-a-ref",
                "aaaaaaaa\trefs/tags/latest",
                "bbbbbbbb\trefs/heads/main",
                "cccccccc\trefs/tags/v1.7.0",
            ]
        )

        self.assertEqual(parse_git_tag_output(output), "1.7.0")

    @mock.patch("heyamara_cli.release_version.shutil.which", return_value=None)
    @mock.patch("heyamara_cli.release_version.subprocess.run")
    def test_fallback_requests_unpeeled_refs_and_returns_canonical_version(
        self, run_mock, _which_mock
    ):
        run_mock.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=ANNOTATED_TAGS, stderr=""
        )

        version = fetch_latest_release_version("Hey-Amara/cli", timeout=5)

        self.assertEqual(version, "1.8.4")
        command = run_mock.call_args.args[0]
        self.assertIn("--refs", command)
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 5)

    @mock.patch("heyamara_cli.release_version.shutil.which", return_value="/usr/bin/gh")
    @mock.patch("heyamara_cli.release_version.subprocess.run")
    def test_git_fallback_runs_when_github_cli_fails(self, run_mock, _which_mock):
        run_mock.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="failed"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=ANNOTATED_TAGS, stderr=""),
        ]

        self.assertEqual(fetch_latest_release_version("Hey-Amara/cli"), "1.8.4")
        self.assertEqual(run_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
