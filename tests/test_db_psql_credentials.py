import contextlib
import os
import subprocess
import unittest
from unittest import mock

import heyamara_cli.commands.db as db_module


IAM_TOKEN = (
    "db.example.internal:5432/?Action=connect&X-Amz-Credential=a/b+c="
    "&X-Amz-Signature=signed:value\\tail"
)


class DbPsqlCredentialTests(unittest.TestCase):
    def _patch_connection_dependencies(self, stack):
        stack.enter_context(
            mock.patch.object(db_module.shutil, "which", return_value="/usr/bin/psql")
        )
        stack.enter_context(
            mock.patch.object(
                db_module,
                "_resolve_env_profile_region",
                return_value=("staging", "test-profile", "ap-southeast-2"),
            )
        )
        stack.enter_context(
            mock.patch.object(db_module, "_pick_service", return_value="ats_staging")
        )
        stack.enter_context(
            mock.patch.object(db_module, "_pick_local_port", return_value=15439)
        )
        stack.enter_context(
            mock.patch.object(
                db_module,
                "require_aws_session",
                return_value="arn:aws:sts::123456789012:assumed-role/developer/test",
            )
        )
        stack.enter_context(
            mock.patch.object(db_module, "_find_eks_node", return_value="i-0123456789")
        )
        stack.enter_context(
            mock.patch.object(
                db_module,
                "_find_rds_endpoint",
                return_value=("db.example.internal", 5432),
            )
        )
        stack.enter_context(
            mock.patch.object(db_module, "preflight_rds_iam_enabled", return_value=True)
        )
        stack.enter_context(mock.patch.object(db_module, "open_tunnel_and_probe"))
        stack.enter_context(mock.patch.object(db_module, "wait_for_tcp", return_value=True))
        return stack.enter_context(
            mock.patch.object(
                db_module,
                "generate_rds_auth_token",
                return_value=IAM_TOKEN,
            )
        )

    def _assert_secret_free_psql_invocation(self, run_mock, expected_argv):
        run_mock.assert_called_once()
        argv = run_mock.call_args.args[0]
        child_env = run_mock.call_args.kwargs["env"]

        self.assertEqual(argv, expected_argv)
        self.assertFalse(any("postgresql://" in argument for argument in argv))
        self.assertFalse(any(IAM_TOKEN in argument for argument in argv))
        self.assertFalse(any("X-Amz-Credential" in argument for argument in argv))
        self.assertEqual(child_env["PGPASSWORD"], IAM_TOKEN)
        self.assertEqual(child_env["PGSSLMODE"], "require")
        self.assertEqual(child_env["PGCONNECT_TIMEOUT"], "10")
        self.assertEqual(child_env["PGAPPNAME"], "heyamara-cli")
        self.assertEqual(child_env["PARENT_MARKER"], "unchanged")

    def test_interactive_psql_keeps_iam_token_out_of_process_arguments(self):
        completed = subprocess.CompletedProcess(args=[], returncode=23)

        with mock.patch.dict(
            os.environ, {"PARENT_MARKER": "unchanged"}, clear=True
        ), contextlib.ExitStack() as stack:
            original_environment = dict(os.environ)
            token_mock = self._patch_connection_dependencies(stack)
            run_mock = stack.enter_context(
                mock.patch.object(db_module.subprocess, "run", return_value=completed)
            )

            with self.assertRaises(SystemExit) as exit_context:
                db_module.psql_cmd.callback(
                    "staging",
                    "ats",
                    "power_user",
                    "custom_database",
                    15432,
                    "test-profile",
                    "ap-southeast-2",
                )

            self.assertEqual(exit_context.exception.code, 23)
            token_mock.assert_called_once_with(
                "db.example.internal",
                5432,
                "power_user",
                "test-profile",
                "ap-southeast-2",
            )
            self._assert_secret_free_psql_invocation(
                run_mock,
                [
                    "psql",
                    "-h",
                    "127.0.0.1",
                    "-p",
                    "15439",
                    "-U",
                    "power_user",
                    "-d",
                    "custom_database",
                ],
            )
            self.assertEqual(dict(os.environ), original_environment)

    def test_doctor_keeps_iam_token_out_of_login_test_arguments(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="current_user | current_database\n-----------+----------\npower_user | ats_staging\n",
            stderr="",
        )

        with mock.patch.dict(
            os.environ, {"PARENT_MARKER": "unchanged"}, clear=True
        ), contextlib.ExitStack() as stack:
            original_environment = dict(os.environ)
            self._patch_connection_dependencies(stack)
            run_mock = stack.enter_context(
                mock.patch.object(db_module.subprocess, "run", return_value=completed)
            )

            db_module.doctor.callback(
                "staging",
                "ats",
                "power_user",
                "test-profile",
                "ap-southeast-2",
            )

            self._assert_secret_free_psql_invocation(
                run_mock,
                [
                    "psql",
                    "-h",
                    "127.0.0.1",
                    "-p",
                    "15439",
                    "-U",
                    "power_user",
                    "-d",
                    "ats_staging",
                    "-c",
                    "SELECT current_user, current_database()",
                ],
            )
            self.assertEqual(
                run_mock.call_args.kwargs["capture_output"],
                True,
            )
            self.assertEqual(run_mock.call_args.kwargs["text"], True)
            self.assertEqual(dict(os.environ), original_environment)

    def test_doctor_preserves_failed_login_reporting(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="psql: authentication failed",
        )

        with contextlib.ExitStack() as stack:
            self._patch_connection_dependencies(stack)
            run_mock = stack.enter_context(
                mock.patch.object(db_module.subprocess, "run", return_value=completed)
            )

            with mock.patch.object(db_module.click, "echo") as echo_mock:
                db_module.doctor.callback(
                    "staging",
                    "ats",
                    "power_user",
                    "test-profile",
                    "ap-southeast-2",
                )

            self.assertIn(
                mock.call("     psql: authentication failed"),
                echo_mock.call_args_list,
            )
            self.assertEqual(
                run_mock.call_args.args[0][-2:],
                ["-c", "SELECT current_user, current_database()"],
            )
            self.assertTrue(run_mock.call_args.kwargs["capture_output"])
            self.assertTrue(run_mock.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
