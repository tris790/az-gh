from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "gh"


class AzGhTests(unittest.TestCase):
    def make_fake_az(self, directory: Path) -> Path:
        fake = directory / ("az.cmd" if os.name == "nt" else "az")
        fake.write_text(
            """#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args[:2] == ["account", "show"]:
    print(json.dumps({"user": {"name": "alice@example.com", "id": "user-1"}}))
elif args[:3] == ["repos", "pr", "list"]:
    print(json.dumps([{
        "pullRequestId": 42,
        "status": "active",
        "title": "Improve parser",
        "sourceRefName": "refs/heads/main",
        "targetRefName": "refs/heads/trunk",
        "repository": {"webUrl": "https://dev.azure.com/acme/Tools/_git/widget"},
        "createdBy": {"displayName": "Alice"}
    }]))
elif args[:3] == ["repos", "pr", "show"]:
    print(json.dumps({
        "pullRequestId": 42,
        "status": "active",
        "repository": {"id": "repo-1"},
        "lastMergeSourceCommit": {"commitId": "new-sha"},
        "lastMergeTargetCommit": {"commitId": "old-sha"}
    }))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "diffs" in args:
    print(json.dumps({"changes": [{"item": {"path": "/README.md"}, "changeType": "edit"}]}))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "items" in args:
    print(json.dumps({"content": "new\\n" if "new-sha" in " ".join(args) else "old\\n", "contentType": "rawText"}))
else:
    print(json.dumps({}))
""",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return fake

    def run_cli(self, fake: Path, log: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        env.update({"AZ_GH_AZ": str(fake), "AZ_GH_LOG_FILE": str(log)})
        return subprocess.run([str(CLI), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)

    def read_records(self, log: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def test_version_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "commands.jsonl"
            completed = self.run_cli(Path("az"), log, "--version")
            self.assertEqual(completed.returncode, 0)
            self.assertIn(b"gh version", completed.stdout)
            records = self.read_records(log)
            self.assertEqual([item["event"] for item in records], ["start", "output", "result"])
            self.assertEqual(records[0]["argv"], ["--version"])

    def test_bare_invocation_prints_help_like_real_gh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "commands.jsonl"
            completed = self.run_cli(Path("az"), log)
            self.assertEqual(completed.returncode, 0)
            self.assertIn(b"usage: gh", completed.stdout.lower())
            self.assertIn(b"pr", completed.stdout)

    def test_pr_list_maps_gh_fields_and_azure_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            log = directory / "commands.jsonl"
            completed = self.run_cli(
                fake,
                log,
                "pr", "list", "--head", "main", "--author", "@me", "--state", "all",
                "--json", "number,url,state,headRefName", "--repo", "Tools/widget",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = json.loads(completed.stdout)
            self.assertEqual(data, [{
                "number": 42,
                "url": "https://dev.azure.com/acme/Tools/_git/widget/pullrequest/42",
                "state": "OPEN",
                "headRefName": "main",
            }])

    def test_pr_diff_emits_unified_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            log = directory / "commands.jsonl"
            completed = self.run_cli(fake, log, "pr", "diff", "42", "--repo", "Tools/widget")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(b"diff --git a/README.md b/README.md", completed.stdout)
            self.assertIn(b"-old", completed.stdout)
            self.assertIn(b"+new", completed.stdout)

    def test_missing_azure_cli_is_a_recorded_127_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "commands.jsonl"
            completed = self.run_cli(Path(temporary) / "does-not-exist", log, "api", "user")
            self.assertEqual(completed.returncode, 127)
            self.assertIn(b"Azure CLI executable not found", completed.stderr)
            self.assertEqual(self.read_records(log)[-1]["exit_code"], 127)

    def test_log_file_is_private_and_has_expected_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            log = directory / "commands.jsonl"
            completed = self.run_cli(fake, log, "api", "user")
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            records = self.read_records(log)
            output = [item for item in records if item["event"] == "output"]
            self.assertEqual(base64.b64decode(output[-1]["data_b64"]), completed.stdout)


if __name__ == "__main__":
    unittest.main()
