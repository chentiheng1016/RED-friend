"""Regression tests for Cloud Run deploy command assembly."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]


class GcpDeployCloudRunTests(unittest.TestCase):
    def _run_with_fake_gcloud(
        self,
        *,
        operational_secret: bool,
        require_db: bool = False,
        cloud_sql_instance: str | None = None,
        skip_telegram: bool = True,
        skip_jobs: bool = True,
    ):
        with tempfile.TemporaryDirectory(prefix="red_fake_gcloud_") as tmp:
            tmp_path = Path(tmp)
            calls_path = tmp_path / "calls.jsonl"
            fake = tmp_path / "gcloud"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json
                    import os
                    import sys

                    args = sys.argv[1:]
                    with open(os.environ["RED_FAKE_GCLOUD_CALLS"], "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(args) + "\\n")

                    if args[:2] == ["secrets", "describe"]:
                        secret = args[2] if len(args) > 2 else ""
                        required = {{
                            "gemini-api-key",
                            "telegram-bot-token",
                            "telegram-chat-id",
                            "web-secret-key",
                            "google-oauth-client-id",
                            "google-oauth-client-secret",
                            "google-oauth-token-json",
                        }}
                        if secret in required:
                            sys.exit(0)
                        if secret == "operational-db-url":  # pragma: allowlist secret
                            sys.exit(0 if {str(operational_secret)} else 1)
                        sys.exit(1)

                    if args[:4] == ["run", "services", "describe"]:
                        sys.exit(0)

                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
            env["RED_FAKE_GCLOUD_CALLS"] = str(calls_path)
            cmd = [
                "scripts/gcp_deploy_cloud_run.sh",
                "--project",
                "fake-project",
                "--region",
                "asia-east1",
                "--tag",
                "testtag",
            ]
            if cloud_sql_instance is not None:
                cmd.extend(["--cloud-sql-instance", cloud_sql_instance])
            if skip_telegram:
                cmd.append("--skip-telegram")
            if skip_jobs:
                cmd.append("--skip-jobs")
            if require_db:
                cmd.append("--require-operational-db")
            proc = subprocess.run(
                cmd,
                cwd=_REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            calls = []
            if calls_path.exists():
                calls = [
                    json.loads(line)
                    for line in calls_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            return proc, calls

    def test_operational_secret_enables_postgres_env_and_secret_binding(self):
        proc, calls = self._run_with_fake_gcloud(operational_secret=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        deploy_calls = [
            call for call in calls
            if call[:3] == ["run", "deploy", "red-web"]
        ]
        self.assertEqual(len(deploy_calls), 1)
        deploy = deploy_calls[0]
        env_vars = deploy[deploy.index("--set-env-vars") + 1]
        secrets = deploy[deploy.index("--set-secrets") + 1]

        self.assertIn("RED_OPERATIONAL_DB_POOL=1", env_vars)
        self.assertIn("RED_TASK_QUEUE_BACKEND=postgres", env_vars)
        self.assertIn("RED_GEMINI_CIRCUIT_BACKEND=postgres", env_vars)
        self.assertIn("RED_OPERATIONAL_DB_URL=operational-db-url:latest", secrets)

    def test_cloud_sql_instance_is_attached_to_run_resources(self):
        instance = "fake-project:asia-east1:red-opdb"
        proc, calls = self._run_with_fake_gcloud(
            operational_secret=True,
            cloud_sql_instance=instance,
            skip_telegram=False,
            skip_jobs=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        deploy_calls = [
            call for call in calls
            if call[:3] == ["run", "deploy", "red-web"]
        ]
        worker_calls = [
            call for call in calls
            if call[:5] == ["beta", "run", "worker-pools", "deploy", "red-telegram"]
        ]
        job_calls = [
            call for call in calls
            if call[:3] == ["run", "jobs", "deploy"]
        ]

        self.assertEqual(len(deploy_calls), 1)
        self.assertEqual(len(worker_calls), 1)
        self.assertGreaterEqual(len(job_calls), 1)
        for call in deploy_calls + worker_calls:
            self.assertEqual(call[call.index("--add-cloudsql-instances") + 1], instance)
        for call in job_calls:
            self.assertEqual(call[call.index("--set-cloudsql-instances") + 1], instance)

    def test_require_operational_db_fails_before_build_when_secret_missing(self):
        proc, calls = self._run_with_fake_gcloud(
            operational_secret=False,
            require_db=True,
        )

        self.assertEqual(proc.returncode, 1)
        self.assertIn("Missing Secret Manager secret: operational-db-url", proc.stderr)
        self.assertFalse(any(call[:2] == ["builds", "submit"] for call in calls))


if __name__ == "__main__":
    unittest.main()
