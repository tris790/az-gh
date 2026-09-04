from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from azgh.repository import parse_repo_flag, resolve


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "gh"


class AzGhTests(unittest.TestCase):
    def test_azure_project_urls_resolve_legacy_and_modern_forms(self) -> None:
        self.assertEqual(
            parse_repo_flag("https://tris790.visualstudio.com/ClaudeOps"),
            ("https://tris790.visualstudio.com", "ClaudeOps", None),
        )
        self.assertEqual(
            parse_repo_flag("https://dev.azure.com/tris790/ClaudeOps"),
            ("https://dev.azure.com/tris790", "ClaudeOps", None),
        )
        self.assertEqual(
            parse_repo_flag("https://tris790.visualstudio.com/ClaudeOps/_git/widget"),
            ("https://tris790.visualstudio.com", "ClaudeOps", "widget"),
        )

    def test_project_url_can_supply_environment_context(self) -> None:
        previous = os.environ.get("AZDO_ORG_URL")
        try:
            os.environ["AZDO_ORG_URL"] = "https://tris790.visualstudio.com/ClaudeOps"
            context = resolve(cwd=Path("/does/not/exist"))
        finally:
            if previous is None:
                os.environ.pop("AZDO_ORG_URL", None)
            else:
                os.environ["AZDO_ORG_URL"] = previous
        self.assertEqual(context.organization, "https://tris790.visualstudio.com")
        self.assertEqual(context.project, "ClaudeOps")

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
            self.assertEqual(
                completed.stdout,
                b"gh version 0.1.0 (Azure DevOps)\n"
                b"https://github.com/cli/cli/releases/tag/v0.1.0\n",
            )
            records = self.read_records(log)
            self.assertEqual([item["event"] for item in records], ["start", "output", "result"])
            self.assertEqual(records[0]["argv"], ["--version"])

    def test_bare_invocation_prints_help_like_real_gh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "commands.jsonl"
            completed = self.run_cli(Path("az"), log)
            self.assertEqual(completed.returncode, 0)
            self.assertIn(b"USAGE\n  gh <command> <subcommand> [flags]", completed.stdout)
            self.assertIn(b"pr", completed.stdout)

    def test_empty_subcommands_follow_gh_output_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            auth = self.run_cli(Path("az"), directory / "auth.jsonl", "auth")
            self.assertEqual(auth.returncode, 0)
            self.assertIn(b"USAGE\n  gh auth <command> [flags]", auth.stdout)

            pr = self.run_cli(Path("az"), directory / "pr.jsonl", "pr")
            self.assertEqual(pr.returncode, 0)
            self.assertIn(b"USAGE\n  gh pr <command> [flags]", pr.stdout)

            api = self.run_cli(Path("az"), directory / "api.jsonl", "api")
            self.assertEqual(api.returncode, 1)
            self.assertEqual(api.stdout, b"")
            self.assertEqual(api.stderr, b"accepts 1 arg(s), received 0\n")

    def test_graphql_pull_request_search_is_translated_to_azure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                "query($searchQuery: String!, $first: Int!, $after: String) "
                "{ search(query: $searchQuery, type: ISSUE, first: $first, after: $after) "
                "{ issueCount nodes { __typename ... on PullRequest { number title } } "
                "pageInfo { endCursor hasNextPage } } }"
            )
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                "searchQuery=is:pr archived:false user-review-requested:@me is:open sort:updated-desc",
                "-f",
                "after=null",
                "-F",
                "first=50",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            search = result["data"]["search"]
            self.assertEqual(search["issueCount"], 1)
            self.assertEqual(search["nodes"][0]["__typename"], "PullRequest")
            self.assertEqual(search["nodes"][0]["number"], 42)
            self.assertEqual(search["pageInfo"], {"endCursor": None, "hasNextPage": False})

    def test_graphql_single_pull_request_queries_are_translated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                "query($owner: String!, $repo: String!, $number: Int!) "
                "{ viewer { login } repository(owner: $owner, name: $repo) "
                "{ pullRequest(number: $number) { body bodyHTML } } }"
            )
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                "owner=forgemo",
                "-f",
                "repo=ironstorm_lookup",
                "-F",
                "number=2",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["data"]["repository"]["pullRequest"]["body"], "")
            self.assertEqual(result["data"]["viewer"]["login"], "alice@example.com")

    def test_api_pull_request_url_is_converted_to_browser_url(self) -> None:
        from azgh.prs import pr_url

        self.assertEqual(
            pr_url({
                "pullRequestId": 42,
                "url": "https://tris790.visualstudio.com/project-id/_apis/git/repositories/repo-id/pullRequests/42",
                "repository": {
                    "name": "ClaudeOps",
                    "project": {"name": "ClaudeOps"},
                },
            }),
            "https://tris790.visualstudio.com/ClaudeOps/_git/ClaudeOps/pullrequest/42",
        )

    def test_github_repository_commands_are_delegated(self) -> None:
        from azgh.github import should_delegate

        self.assertTrue(should_delegate(["pr", "list", "--repo", "https://github.com/tris790/az-gh"]))
        self.assertTrue(should_delegate(["pr", "list", "--repo", "tris790/az-gh"]))
        self.assertTrue(should_delegate(["api", "graphql", "--hostname", "github.com"]))
        self.assertFalse(should_delegate(["pr", "list", "--repo", "https://tris790.visualstudio.com/ClaudeOps"]))

    def test_pr_list_text_matches_gh_tabular_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            completed = self.run_cli(fake, directory / "commands.jsonl", "pr", "list")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, b"42\tImprove parser\tmain\tOPEN\t\n")

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
            self.assertEqual(
                completed.stdout,
                b'[{"headRefName":"main","number":42,"state":"OPEN","url":"https://dev.azure.com/acme/Tools/_git/widget/pullrequest/42"}]\n',
            )
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
