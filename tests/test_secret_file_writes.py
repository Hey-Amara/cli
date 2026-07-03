import os
import stat
import tempfile
import unittest
from pathlib import Path

from heyamara_cli.commands.connect import _write_env_for
from heyamara_cli.secret_files import UnsafeSecretFileError, write_secret_text


class SecretFileWriteTests(unittest.TestCase):
    def test_write_secret_text_creates_private_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "secret.env"

            write_secret_text(output, "TOKEN=secret", trailing_newline=True)

            self.assertEqual(output.read_text(), "TOKEN=secret\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    @unittest.skipIf(not hasattr(os, "symlink"), "symlink support unavailable")
    def test_write_secret_text_refuses_symlink_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target.env"
            link = Path(temp_dir) / "link.env"
            target.write_text("SAFE=1")
            os.symlink(target, link)

            with self.assertRaises(UnsafeSecretFileError):
                write_secret_text(link, "TOKEN=secret", trailing_newline=True)

            self.assertEqual(target.read_text(), "SAFE=1")

    def test_connect_env_for_output_is_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / ".env.local"

            _write_env_for(
                str(output),
                "DATABASE_URL=postgres://example\n",
                ["DATABASE_URL"],
                "ats-backend",
                5432,
            )

            self.assertIn("DATABASE_URL=postgres://example", output.read_text())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
