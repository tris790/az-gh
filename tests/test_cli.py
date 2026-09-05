from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from azgh.azcli import AzCli
from azgh.errors import CliError
from azgh.recording import JsonlLogger
from azgh.repository import parse_repo_flag, resolve
from tools.parse_jsonl import json_outputs, load_transcript, shape


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "gh"
OFFICIAL_TRACE = next(
    (ROOT / name for name in ("official_commands.jsonl", "official-commands.jsonl") if (ROOT / name).exists()),
    ROOT / "official_commands.jsonl",
)


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
            parse_repo_flag("dev.azure.com/tris790/ClaudeOps"),
            ("https://dev.azure.com/tris790", "ClaudeOps", None),
        )
        self.assertEqual(
            parse_repo_flag("dev.azure.com/tris790/ClaudeOps/_git/widget"),
            ("https://dev.azure.com/tris790", "ClaudeOps", "widget"),
        )
        self.assertEqual(
            parse_repo_flag("https://dev.azure.com/tris790/video%20cloud"),
            ("https://dev.azure.com/tris790", "video cloud", None),
        )
        self.assertEqual(
            parse_repo_flag("https://tris790.visualstudio.com/ClaudeOps/_git/widget"),
            ("https://tris790.visualstudio.com", "ClaudeOps", "widget"),
        )

    def test_project_names_with_spaces_remain_one_azure_argument(self) -> None:
        from azgh.repository import RepoContext

        self.assertEqual(
            RepoContext("https://dev.azure.com/tris790", "video cloud", None).az_args(),
            ["--organization", "https://dev.azure.com/tris790", "--project", "video cloud"],
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

    def test_github_owner_repo_flag_uses_azure_defaults_from_github_checkout(self) -> None:
        context = resolve("tris790/az-gh", cwd=ROOT)
        self.assertIsNone(context.organization)
        self.assertIsNone(context.project)
        self.assertIsNone(context.repository)

    def test_github_hosted_repo_flag_does_not_become_azure_context(self) -> None:
        for value in ("github.com/availchet/Weather", "https://github.com/availchet/Weather"):
            with self.subTest(value=value):
                context = resolve(value, cwd=ROOT)
                self.assertIsNone(context.organization)
                self.assertIsNone(context.project)
                self.assertIsNone(context.repository)

    def test_azure_cli_timeout_is_reported_as_cli_error(self) -> None:
        with patch(
            "azgh.azcli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["az"], 30),
        ):
            with self.assertRaisesRegex(CliError, "Azure CLI request timed out") as raised:
                AzCli(lambda _stream, _data: None).run(["account", "show"])
        self.assertEqual(raised.exception.exit_code, 124)

    def test_azure_cli_resolves_az_cmd_when_az_is_not_found(self) -> None:
        with patch("azgh.azcli.shutil.which", side_effect=lambda name: "C:/Azure/az.cmd" if name == "az.cmd" else None):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AZ_GH_AZ", None)
                az = AzCli(lambda _stream, _data: None)
        self.assertEqual(az.executable, "C:/Azure/az.cmd")

    def make_fake_az(self, directory: Path) -> Path:
        fake = directory / ("az.cmd" if os.name == "nt" else "az")
        fake.write_text(
            """#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args[:2] == ["account", "show"]:
    print(json.dumps({"user": {"name": "alice@example.com", "id": "user-1"}}))
elif args[:4] == ["devops", "user", "show", "--user"]:
    print(json.dumps({
        "id": "identity-1",
        "user": {
            "displayName": "Alice Example",
            "url": "https://dev.azure.com/acme/_apis/Graph/Users/identity-1"
        }
    }))
elif args[:3] == ["repos", "pr", "list"]:
    records = [{
        "pullRequestId": 42,
        "status": "active",
        "title": "Improve parser",
        "sourceRefName": "refs/heads/main",
        "targetRefName": "refs/heads/trunk",
        "repository": {
            "id": "repo-1",
            "project": {"name": "Tools"},
            "webUrl": "https://dev.azure.com/acme/Tools/_git/widget",
        },
        "lastMergeSourceCommit": {"commitId": "new-sha"},
        "lastMergeTargetCommit": {"commitId": "old-sha"},
        "createdBy": {"displayName": "Alice", "uniqueName": "alice@example.com"}
    }]
    if "--reviewer" not in args and "--source-branch" not in args and "--creator" not in args:
        records.append({
            "pullRequestId": 43,
            "status": "active",
            "title": "Unrelated branch",
            "sourceRefName": "refs/heads/feature/unrelated",
            "targetRefName": "refs/heads/trunk",
            "repository": {"webUrl": "https://dev.azure.com/acme/Tools/_git/widget"},
            "createdBy": {"displayName": "Alice", "uniqueName": "alice@example.com"}
        })
    if "--top" in args and "--reviewer" not in args:
        records.append({
            "pullRequestId": 44,
            "status": "active",
            "title": "Other repository",
            "sourceRefName": "refs/heads/feature/other",
            "targetRefName": "refs/heads/trunk",
            "repository": {"webUrl": "https://dev.azure.com/acme/Tools/_git/other"},
            "createdBy": {"displayName": "Alice", "uniqueName": "alice@example.com"}
        })
    print(json.dumps(records))
elif args[:3] == ["repos", "pr", "show"]:
    number = args[args.index("--id") + 1]
    if number == "4":
        print(json.dumps({
            "pullRequestId": 4,
            "status": "active",
            "title": "Ready",
            "sourceRefName": "refs/heads/feature/ready",
            "targetRefName": "refs/heads/main",
            "description": "# Ready\\n\\nChanges",
            "creationDate": "2026-01-01T00:00:00Z",
            "repository": {
                "id": "repo-1",
                "name": "widget",
                "project": {"name": "Tools"},
                "webUrl": "https://dev.azure.com/acme/Tools/_git/widget"
            },
            "lastMergeSourceCommit": {"commitId": "new-sha"},
            "lastMergeTargetCommit": {"commitId": "old-sha"}
        }))
    else:
        print(json.dumps({
            "pullRequestId": 42,
            "status": "active",
            "repository": {
                "id": "repo-1",
                "project": {"name": "Tools"},
                "webUrl": "https://dev.azure.com/acme/Tools/_git/widget"
            },
            "lastMergeSourceCommit": {"commitId": "new-sha"},
            "lastMergeTargetCommit": {"commitId": "old-sha"}
        }))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "pullRequests" in args:
    if "pullRequestId=4" in args:
        print(json.dumps({
            "pullRequestId": 4,
            "status": "active",
            "title": "Ready",
            "sourceRefName": "refs/heads/feature/ready",
            "targetRefName": "refs/heads/main",
            "description": "# Ready\\n\\nChanges",
            "creationDate": "2026-01-01T00:00:00Z",
            "repository": {
                "id": "repo-1",
                "name": "widget",
                "project": {"name": "Tools"},
                "webUrl": "https://dev.azure.com/acme/Tools/_git/widget"
            },
            "lastMergeSourceCommit": {"commitId": "new-sha"},
            "lastMergeTargetCommit": {"commitId": "old-sha"}
        }))
    else:
        print(json.dumps({
            "pullRequestId": 42,
            "status": "active",
            "repository": {"id": "repo-1"},
            "lastMergeSourceCommit": {"commitId": "new-sha"},
            "lastMergeTargetCommit": {"commitId": "old-sha"}
        }))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "commits" in args:
    print(json.dumps({"value": [{
        "commitId": "new-sha",
        "comment": "Add changes",
        "author": {"name": "Alice", "email": "alice@example.com", "date": "2026-01-01T00:00:00Z"},
        "url": "https://dev.azure.com/acme/Tools/_git/widget/commit/new-sha"
    }]}))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "pullRequestIterations" in args:
    print(json.dumps({"value": [{
        "id": 1,
        "sourceRefCommit": {"commitId": "new-sha"},
        "targetRefCommit": {"commitId": "old-sha"}
    }]}))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "pullRequestIterationChanges" in args:
    print(json.dumps({"changeEntries": [{
        "changeTrackingId": 3,
        "item": {"path": "/TestMod/TestMod.cs"},
        "changeType": "edit"
    }]}))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "pullRequestThreads" in args:
    if "--http-method" in args and args[args.index("--http-method") + 1] == "POST":
        with open(args[args.index("--in-file") + 1], encoding="utf-8") as request_file:
            request = json.load(request_file)
        print(json.dumps({
            "id": 17,
            "comments": [{
                "id": 18,
                "content": request["comments"][0]["content"],
                "author": {"uniqueName": "alice@example.com"}
            }]
        }))
    else:
        print(json.dumps({"value": [
            {
                "id": 7,
                "status": "active",
                "threadContext": {"filePath": "/README.md", "rightFileStart": {"line": 4}},
                "comments": [{
                    "id": 8,
                    "content": "Please update this",
                    "publishedDate": "2026-01-01T00:00:00Z",
                    "author": {"displayName": "Alice", "imageUrl": "https://example.test/alice"}
                }]
            },
            {
                "id": 9,
                "status": "fixed",
                "comments": [{
                    "id": 10,
                    "content": "Alice voted 5",
                    "commentType": "system",
                    "author": {"displayName": "Microsoft.VisualStudio.Services"}
                }]
            },
            {
                "id": 10,
                "status": "active",
                "comments": [{
                    "id": 13,
                    "content": "General conversation comment",
                    "publishedDate": "2026-01-01T00:00:00Z",
                    "author": {"displayName": "Alice", "uniqueName": "alice@example.com"}
                }]
            },
            {
                "id": 11,
                "status": "wontFix",
                "threadContext": {"filePath": "old.txt", "leftFileStart": {"line": 9}},
                "comments": [{
                    "id": 12,
                    "content": "This is on the old side",
                    "publishedDate": "2026-01-01T00:00:00Z",
                    "author": {"displayName": "Alice"}
                }]
            }
        ]}))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "fileDiffs" in args:
    if "--http-method" not in args or args[args.index("--http-method") + 1] != "POST":
        print("missing POST", file=sys.stderr)
        sys.exit(2)
    with open(args[args.index("--in-file") + 1], encoding="utf-8") as request_file:
        request = json.load(request_file)
    if request["baseVersionCommit"] != "old-sha" or request["targetVersionCommit"] != "new-sha":
        print("wrong commit range", file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"value": [{
        "path": "/README.md",
        "lineDiffBlocks": [{
            "changeType": 3,
            "originalLinesCount": 1,
            "modifiedLinesCount": 1,
        }],
    }]}))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "commitDiffs" in args:
    if "project=Tools" not in args:
        print("missing project", file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"changes": [{"item": {"path": "/README.md"}, "changeType": "edit"}]}))
elif args[:4] == ["devops", "invoke", "--area", "git"] and "items" in args:
    is_new = "new-sha" in " ".join(args)
    line = "new line" if is_new else "old line"
    print(json.dumps({
        "content": f"one\\ntwo\\nthree\\n{line}\\nfive\\nsix\\nseven\\n",
        "contentType": "rawText"
    }))
else:
    print(json.dumps({}))
""",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return fake

    def run_cli(self, fake: Path, log: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        env.pop("AZ_GH_PASSTHROUGH", None)
        env.pop("AZ_GH_PROVIDER", None)
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

    def test_auth_status_honors_requested_hostname(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            completed = self.run_cli(
                self.make_fake_az(directory),
                directory / "auth-status.jsonl",
                "auth", "status", "--active", "--hostname", "github.com",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(completed.stdout.startswith(b"github.com\n"))

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
            self.assertEqual(search["nodes"][0], {
                "__typename": "PullRequest",
                "number": 42,
                "title": "Improve parser",
            })
            self.assertEqual(
                search["pageInfo"],
                {"endCursor": "AZ_GH_CURSOR", "hasNextPage": False},
            )

    def test_graphql_pull_request_search_includes_content_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                "query($searchQuery: String!, $first: Int!, $after: String) "
                "{ search(query: $searchQuery, type: ISSUE, first: $first, after: $after) "
                "{ nodes { __typename ... on PullRequest { number body bodyHTML } } } }"
            )
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                "searchQuery=is:pr is:open",
                "-f",
                "after=null",
                "-F",
                "first=50",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout)["data"]["search"]["nodes"][0],
                {"__typename": "PullRequest", "number": 42, "body": "", "bodyHTML": ""},
            )

    def test_graphql_pull_request_search_includes_diff_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                "query($searchQuery: String!, $first: Int!, $after: String) "
                "{ search(query: $searchQuery, type: ISSUE, first: $first, after: $after) "
                "{ nodes { __typename ... on PullRequest { number additions deletions } } } }"
            )
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                "searchQuery=is:pr is:open",
                "-f",
                "after=null",
                "-F",
                "first=50",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            node = json.loads(completed.stdout)["data"]["search"]["nodes"][0]
            self.assertEqual(node["number"], 42)
            self.assertEqual(node["additions"], 1)
            self.assertEqual(node["deletions"], 1)

    def test_graphql_pull_request_search_filters_by_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                "query($searchQuery: String!, $first: Int!) "
                "{ search(query: $searchQuery, type: ISSUE, first: $first) "
                "{ issueCount nodes { __typename ... on PullRequest { number } } } }"
            )
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                "searchQuery=repo:acme/widget is:pr is:open",
                "-F",
                "first=50",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            search = json.loads(completed.stdout)["data"]["search"]
            self.assertEqual(search["issueCount"], 2)
            self.assertEqual([node["number"] for node in search["nodes"]], [42, 43])

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
            official_body = next(
                value
                for _, value in json_outputs(OFFICIAL_TRACE)
                if isinstance(value, dict)
                and isinstance(value.get("data"), dict)
                and isinstance(value["data"].get("repository"), dict)
                and set(value["data"]["repository"].get("pullRequest", {}))
                == {"body", "bodyHTML"}
            )
            self.assertEqual(shape(result), shape(official_body))

    def test_graphql_detail_matches_app_shape_and_loads_azure_subresources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                "query($owner: String!, $repo: String!, $number: Int!) "
                "{ viewer { login } repository(owner: $owner, name: $repo) "
                "{ pullRequest(number: $number) { body bodyHTML headRepository { url } "
                "commits(last:1) { nodes { commit { oid messageHeadline url "
                "statusCheckRollup { contexts { nodes { __typename } pageInfo { hasNextPage endCursor } } } } } "
                "pageInfo { hasPreviousPage } } "
                "comments(first:100) { nodes { author { login } body bodyHTML url } } "
                "reviewThreads(first:100) { nodes { id diffHunk diffSide path comments { nodes { author { login } body bodyHTML url diffHunk } } } } } }"
            )
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api", "graphql", "-f", f"query={query}",
                "-f", "owner=acme", "-f", "repo=widget", "-F", "number=4",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            pr = json.loads(completed.stdout)["data"]["repository"]["pullRequest"]
            self.assertEqual(pr["bodyHTML"], '<h1 dir="auto">Ready</h1>\n<p dir="auto">Changes</p>')
            self.assertEqual(pr["headRepository"]["url"], "https://dev.azure.com/acme/Tools/_git/widget")
            self.assertEqual(pr["commits"]["nodes"][0]["commit"]["oid"], "new-sha")
            self.assertIsNone(pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"])
            self.assertFalse(pr["commits"]["pageInfo"]["hasPreviousPage"])
            self.assertEqual([comment["body"] for comment in pr["comments"]["nodes"]], [
                "General conversation comment",
            ])
            self.assertEqual(pr["comments"]["nodes"][0]["author"]["login"], "alice@example.com")
            self.assertEqual(pr["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["author"]["login"], "Alice")
            self.assertEqual(pr["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["url"], "https://dev.azure.com/acme/Tools/_git/widget/pullrequest/4?discussionId=7")
            self.assertEqual(pr["comments"]["nodes"][0]["bodyHTML"], '<p dir="auto">General conversation comment</p>')
            self.assertEqual(pr["commits"]["nodes"][0]["commit"]["url"], "https://dev.azure.com/acme/Tools/_git/widget/commit/new-sha")
            self.assertEqual([thread["id"] for thread in pr["reviewThreads"]["nodes"]], ["7", "11"])
            self.assertEqual(pr["reviewThreads"]["nodes"][1]["diffSide"], "LEFT")
            self.assertEqual(pr["reviewThreads"]["nodes"][1]["path"], "old.txt")
            self.assertIn("@@", pr["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["diffHunk"])

    def test_graphql_detail_honors_include_directives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                "query($owner: String!, $repo: String!, $number: Int!, $includeComments: Boolean!) "
                "{ repository(owner: $owner, name: $repo) "
                "{ pullRequest(number: $number) { number comments(first:100) "
                "@include(if:$includeComments) { nodes { body } } } } }"
            )
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api", "graphql", "-f", f"query={query}",
                "-f", "owner=acme", "-f", "repo=widget", "-F", "number=4",
                "-F", "includeComments=false",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout)["data"]["repository"]["pullRequest"],
                {"number": 4},
            )

    def test_recorded_rich_graphql_pr_query_completes_with_content(self) -> None:
        rich_command = next(
            command
            for command in load_transcript(OFFICIAL_TRACE)
            if any(
                isinstance(argument, str) and "includeComments" in argument
                for argument in command.get("argv", [])
            )
        )
        query = next(
            argument.split("=", 1)[1]
            for argument in rich_command["argv"]
            if isinstance(argument, str) and argument.startswith("query=")
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api", "graphql", "-f", f"query={query}",
                "-F", "owner=acme", "-F", "repo=widget", "-F", "number=4",
                "-F", "includeComments=true", "-F", "includeReviews=true",
                "-F", "includeThreads=true", "-F", "includeSummary=true",
                "--hostname", "github.com",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            pr = json.loads(completed.stdout)["data"]["repository"]["pullRequest"]
            self.assertEqual(pr["number"], 4)
            self.assertEqual(pr["commits"]["nodes"][0]["commit"]["oid"], "new-sha")
            self.assertEqual([comment["body"] for comment in pr["comments"]["nodes"]], [
                "General conversation comment",
            ])
            self.assertEqual([thread["id"] for thread in pr["reviewThreads"]["nodes"]], ["7", "11"])

    def test_pr_view_repository_fields_match_gh_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "pr", "view", "4",
                "--json", "baseRefOid,headRefOid,number,headRepositoryOwner,headRepository,url",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["number"], 4)
            self.assertEqual(result["headRefOid"], "new-sha")
            self.assertEqual(result["baseRefOid"], "old-sha")
            self.assertEqual(result["headRepositoryOwner"]["login"], "acme")
            self.assertEqual(result["headRepository"]["name"], "widget")
            self.assertEqual(result["headRepository"]["owner"]["login"], "acme")
            self.assertEqual(result["headRepository"]["url"], "https://dev.azure.com/acme/Tools/_git/widget")

            jq = self.run_cli(
                fake,
                directory / "commands-jq.jsonl",
                "pr", "view", "4", "--json", "headRefOid", "--jq", ".headRefOid",
                "--repo", "github.com/availchet/Weather",
            )
            self.assertEqual(jq.returncode, 0, jq.stderr)
            self.assertEqual(jq.stdout, b"new-sha\n")

    def test_pr_view_content_fields_are_available_in_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "pr", "view", "4", "--json", "body,bodyHTML",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {
                "body": "# Ready\n\nChanges",
                "bodyHTML": '<h1 dir="auto">Ready</h1>\n<p dir="auto">Changes</p>',
            })

    def test_graphql_literal_repository_alias_uses_azure_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                'query { viewer { login } p0: repository(owner:"acme",name:"widget") '
                '{ pullRequest(number:4) { number title } } }'
            )
            completed = self.run_cli(
                fake, directory / "commands.jsonl",
                "api", "graphql", "-f", f"query={query}",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            pr = json.loads(completed.stdout)["data"]["p0"]["pullRequest"]
            self.assertEqual(pr["number"], 4)
            self.assertEqual(pr["title"], "Ready")

    def test_graphql_pr_summary_has_the_official_gh_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                'query { viewer { login } p0: repository(owner:"acme",name:"widget") '
                '{ pullRequest(number:4) { number url title baseRefName headRefName state '
                'isDraft mergeable mergeStateStatus headRefOid headRepository { url } '
                'commits(last:1) { nodes { commit { oid statusCheckRollup { '
                'contexts(first:100,after:null) { nodes { __typename ... on CheckRun { '
                'name status conclusion startedAt completedAt detailsUrl checkSuite { '
                'workflowRun { event workflow { name } } } } ... on StatusContext { '
                'context state createdAt description targetUrl } } pageInfo { hasNextPage '
                'endCursor } } } } } } }'
            )
            completed = self.run_cli(
                fake, directory / "commands.jsonl", "api", "graphql", "-f", f"query={query}"
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            azure = json.loads(completed.stdout)
            official = next(
                value
                for _, value in json_outputs(OFFICIAL_TRACE)
                if isinstance(value, dict)
                and isinstance(value.get("data"), dict)
                if isinstance(value.get("data", {}).get("p0"), dict)
                and isinstance(value["data"]["p0"].get("pullRequest"), dict)
            )
            self.assertEqual(shape(azure), shape(official))

    def test_graphql_summary_selects_azure_source_commit_for_last_one(self) -> None:
        from azgh.graphql import _commit_nodes

        commits = {
            "value": [
                {"commitId": "older-sha", "author": {}, "comment": "Older"},
                {"commitId": "head-sha", "author": {}, "comment": "Head"},
            ]
        }
        nodes = _commit_nodes(commits, 1, {}, head_oid="head-sha")
        self.assertEqual(["head-sha"], [nodes[0]["commit"]["oid"]])
        nodes = _commit_nodes({"value": []}, 1, {}, head_oid="head-sha")
        self.assertEqual(["head-sha"], [nodes[0]["commit"]["oid"]])

    def test_graphql_github_host_uses_a_parseable_pull_request_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                'query { viewer { login } p0: repository(owner:"acme",name:"widget") '
                '{ pullRequest(number:4) { number url } } }'
            )
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api", "graphql", "--hostname", "github.com", "-f", f"query={query}",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout)["data"]["p0"]["pullRequest"]["url"],
                "https://github.com/acme/widget/pull/4",
            )
            self.assertEqual(json.loads(completed.stdout)["data"]["viewer"]["login"], "alice")

    def test_graphql_github_host_projects_nested_resource_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                "query($owner:String!,$repo:String!,$number:Int!){viewer{login} "
                "repository(owner:$owner,name:$repo){pullRequest(number:$number){"
                "url headRepository{url} commits(last:1){nodes{commit{url}}} "
                "comments(first:100){nodes{url}} reviewThreads(first:100){nodes{"
                "comments(first:100){nodes{url}}}}}}}"
            )
            completed = self.run_cli(
                fake, directory / "commands.jsonl", "api", "graphql",
                "--hostname", "github.com", "-f", f"query={query}",
                "-f", "owner=acme", "-f", "repo=widget", "-F", "number=4",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            pr = json.loads(completed.stdout)["data"]["repository"]["pullRequest"]
            self.assertEqual(pr["headRepository"]["url"], "https://github.com/acme/widget")
            self.assertEqual(
                pr["commits"]["nodes"][0]["commit"]["url"],
                "https://github.com/acme/widget/commit/new-sha",
            )
            self.assertEqual(
                pr["comments"]["nodes"][0]["url"],
                "https://github.com/acme/widget/pull/4?discussionId=10",
            )
            self.assertEqual(
                pr["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["url"],
                "https://github.com/acme/widget/pull/4?discussionId=7",
            )

    def test_graphql_detail_keeps_repository_capabilities_at_repository_level(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            query = (
                "query($owner:String!,$repo:String!,$number:Int!){viewer{login} "
                "repository(owner:$owner,name:$repo){mergeCommitAllowed "
                "squashMergeAllowed pullRequest(number:$number){additions deletions "
                "headRefOid author{login avatarUrl} createdAt autoMergeRequest{enabledAt}}}}"
            )
            completed = self.run_cli(
                fake, directory / "commands.jsonl", "api", "graphql",
                "-f", f"query={query}", "-f", "owner=acme", "-f", "repo=widget", "-F", "number=4",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            repository = json.loads(completed.stdout)["data"]["repository"]
            self.assertFalse(repository["mergeCommitAllowed"])
            self.assertFalse(repository["squashMergeAllowed"])
            self.assertEqual(repository["pullRequest"]["additions"], 1)
            self.assertEqual(repository["pullRequest"]["deletions"], 1)

    def test_diff_stats_builds_valid_payloads_for_added_and_edited_files(self) -> None:
        from azgh.prs import pr_diff_stats

        class FakeAzure:
            def __init__(self) -> None:
                self.payload = None

            def json(self, args: list[str], **kwargs: object) -> object:
                if "commitDiffs" in args:
                    return {"changes": [
                        {"changeType": "add", "item": {"path": "/new.cs"}},
                        {"changeType": "edit", "item": {"path": "/changed.cs"}},
                    ]}
                self.payload = kwargs["payload"]
                return {"value": [
                    {"lineDiffBlocks": [{
                        "changeType": "add", "originalLinesCount": 0, "modifiedLinesCount": 2,
                    }]},
                    {"lineDiffBlocks": [{
                        "changeType": "edit", "originalLinesCount": 1, "modifiedLinesCount": 1,
                    }]},
                ]}

        fake = FakeAzure()
        stats = pr_diff_stats(fake, resolve("Tools/repo-1"), {
            "repository": {"id": "repo-1", "project": {"name": "Tools"}},
            "lastMergeSourceCommit": {"commitId": "new-sha"},
            "lastMergeTargetCommit": {"commitId": "old-sha"},
        })
        self.assertEqual(stats, (3, 1))
        self.assertEqual(fake.payload["fileDiffParams"], [
            {"path": "/new.cs"},
            {"path": "/changed.cs", "originalPath": "/changed.cs"},
        ])

    def test_normalize_pr_falls_back_to_creation_date_for_updated_at(self) -> None:
        from azgh.prs import normalize_pr

        normalized = normalize_pr({"creationDate": "2026-01-01T00:00:00Z", "updatedDate": None})
        self.assertEqual(normalized["updatedAt"], "2026-01-01T00:00:00Z")

    def test_normalize_pr_uses_empty_strings_for_missing_body(self) -> None:
        from azgh.prs import normalize_pr

        normalized = normalize_pr({"description": None})
        self.assertEqual(normalized["body"], "")
        self.assertEqual(normalized["bodyHTML"], "")

    def test_graphql_identities_use_azure_unique_name_as_login(self) -> None:
        from azgh.prs import identity_login

        self.assertEqual(
            identity_login({"displayName": "Trisan Deschamps", "uniqueName": "tris790@gmail.com"}),
            "tris790@gmail.com",
        )
        self.assertEqual(identity_login({"displayName": "Alice"}), "Alice")

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

    def test_api_user_includes_azure_identity_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            completed = self.run_cli(fake, directory / "commands.jsonl", "api", "user")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["login"], "alice@example.com")
            self.assertIsInstance(result["id"], int)
            self.assertLessEqual(result["id"], 2**53 - 1)
            self.assertEqual(result["node_id"], "identity-1")
            self.assertEqual(result["name"], "Alice Example")
            self.assertEqual(result["url"], "https://dev.azure.com/acme/_apis/Graph/Users/identity-1")
            self.assertEqual(set(result), {
                "login", "id", "node_id", "avatar_url", "gravatar_id", "url", "html_url",
                "followers_url", "following_url", "gists_url", "starred_url", "subscriptions_url",
                "organizations_url", "repos_url", "events_url", "received_events_url", "type",
                "user_view_type", "site_admin", "name", "company", "blog", "location", "email",
                "hireable", "bio", "twitter_username", "notification_email", "public_repos",
                "public_gists", "followers", "following", "created_at", "updated_at",
            })

    def test_api_user_uses_github_urls_for_github_compatibility_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            completed = self.run_cli(
                self.make_fake_az(directory),
                directory / "commands.jsonl",
                "api", "user", "--hostname", "github.com",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["login"], "alice")
            self.assertEqual(result["url"], "https://api.github.com/users/alice")
            self.assertEqual(result["html_url"], "https://github.com/alice")
            self.assertEqual(result["repos_url"], "https://api.github.com/users/alice/repos")

    def test_api_user_does_not_leak_optional_identity_lookup_errors(self) -> None:
        from azgh.identity import profile

        az = Mock()
        az.json.side_effect = CliError("bad organization")
        self.assertEqual(profile(az, "alice@example.com", "https://dev.azure.com/acme"), {})
        az.json.assert_called_once_with(
            [
                "devops", "user", "show", "--user", "alice@example.com",
                "--organization", "https://dev.azure.com/acme",
            ],
            emit_stderr=False,
        )

    def test_github_repository_commands_are_delegated(self) -> None:
        from azgh.github import should_delegate

        with patch.dict(os.environ, {"AZ_GH_PROVIDER": "github"}, clear=False):
            self.assertTrue(should_delegate(["pr", "list", "--repo", "https://github.com/tris790/az-gh"]))
            self.assertTrue(should_delegate(["pr", "list", "--repo", "tris790/az-gh"]))
            self.assertTrue(should_delegate(["pr", "list", "-R", "tris790/az-gh"]))
            self.assertTrue(should_delegate(["api", "graphql", "--hostname", "github.com"]))
            self.assertFalse(should_delegate(["pr", "list", "--repo", "https://tris790.visualstudio.com/ClaudeOps"]))

    def test_azure_is_default_even_for_github_remotes_and_flags(self) -> None:
        from azgh.github import should_delegate

        with patch.dict(os.environ, {"AZ_GH_PROVIDER": "azure"}, clear=False):
            self.assertFalse(should_delegate(["pr", "list"]))
            self.assertFalse(should_delegate(["api", "graphql", "--hostname", "github.com"]))

    def test_passthrough_executes_the_official_github_cli(self) -> None:
        from azgh.commands import main

        args = ["pr", "list", "--repo", "tris790/az-gh"]
        result = subprocess.CompletedProcess(
            ["/usr/bin/gh", *args], 23, b"official stdout\n", b"official stderr\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "commands.jsonl"
            environment = {"AZ_GH_PASSTHROUGH": "1", "AZ_GH_LOG_FILE": str(log)}
            with patch.dict(os.environ, environment, clear=False):
                with patch("azgh.github.subprocess.run", return_value=result) as run:
                    with patch("azgh.recording.write_bytes"):
                        self.assertEqual(main(args), 23)

            run.assert_called_once_with(
                ["/usr/bin/gh", *args],
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                [record["event"] for record in self.read_records(log)],
                ["start", "output", "output", "result"],
            )

    def test_pr_list_text_matches_gh_tabular_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            completed = self.run_cli(fake, directory / "commands.jsonl", "pr", "list")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                completed.stdout,
                b"42\tImprove parser\tmain\tOPEN\t\n"
                b"43\tUnrelated branch\tfeature/unrelated\tOPEN\t\n",
            )

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

    def test_pr_comment_posts_an_azure_pull_request_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            completed = self.run_cli(
                fake, directory / "commands.jsonl", "pr", "comment", "4", "--body", "hi chat"
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                completed.stdout,
                b"https://dev.azure.com/acme/Tools/_git/widget/pullrequest/4?discussionId=17\n",
            )

    def test_pr_comment_reads_body_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            body_file = directory / "comment.md"
            body_file.write_text("comment from a file\n", encoding="utf-8")
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "pr", "comment", "4", "--body-file", str(body_file),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(b"?discussionId=17\n", completed.stdout)

    def test_api_pull_request_comment_maps_code_location_to_azure_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            completed = self.run_cli(
                fake,
                directory / "commands.jsonl",
                "api", "repos/{owner}/{repo}/pulls/4/comments", "--method", "POST",
                "-f", "body=fix this", "-f", "commit_id=new-sha",
                "-f", "path=TestMod/TestMod.cs", "-F", "line=45", "-f", "side=RIGHT",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["body"], "fix this")
            self.assertEqual(result["path"], "TestMod/TestMod.cs")
            self.assertEqual(result["line"], 45)
            self.assertEqual(result["side"], "RIGHT")
            self.assertEqual(result["html_url"], "https://dev.azure.com/acme/Tools/_git/widget/pullrequest/4?discussionId=17")


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

    def test_pr_diff_derives_project_for_github_compat_repo_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = self.make_fake_az(directory)
            log = directory / "commands.jsonl"
            completed = self.run_cli(
                fake, log, "pr", "diff", "42", "--repo", "github.com/tris790/ClaudeOps"
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(b"diff --git a/README.md b/README.md", completed.stdout)

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

    def test_log_file_keeps_complete_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "commands.jsonl"
            logger = JsonlLogger(log)
            try:
                for number in range(51):
                    logger.write({"number": number})
            finally:
                logger.close()

            records = self.read_records(log)
            self.assertEqual(len(records), 51)
            self.assertEqual(records[0]["number"], 0)
            self.assertEqual(records[-1]["number"], 50)


if __name__ == "__main__":
    unittest.main()
