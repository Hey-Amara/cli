import unittest

from heyamara_cli.loki import build_logql


class BuildLogqlTests(unittest.TestCase):
    def test_no_filters(self):
        self.assertEqual(
            build_logql("production", "ats-backend"),
            '{app="ats-backend", namespace="production"}',
        )

    def test_level_matches_either_casing(self):
        query = build_logql("production", "ats-backend", level="ERROR")
        self.assertIn('level=~"(?i)error"', query)
        self.assertNotIn('level="', query)

    def test_warn_also_matches_warning(self):
        query = build_logql("production", "ats-backend", level="warn")
        self.assertIn('level=~"(?i)warn(ing)?"', query)

    def test_plain_grep_uses_substring_filter(self):
        query = build_logql("production", "ats-backend", grep="Request error")
        self.assertTrue(query.endswith(' |= "Request error"'))

    def test_regex_grep_uses_regex_filter(self):
        query = build_logql("production", "ats-backend", grep="email-sync/push.*statusCode.:50[0-9]")
        self.assertTrue(query.endswith(" |~ `email-sync/push.*statusCode.:50[0-9]`"))

    def test_level_precedes_line_filter(self):
        query = build_logql("production", "ats-backend", grep="x", level="info")
        self.assertEqual(query, '{app="ats-backend", namespace="production", level=~"(?i)info"} |= "x"')


if __name__ == "__main__":
    unittest.main()
