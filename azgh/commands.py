from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any, Callable
from urllib.parse import quote

from . import __version__
from .azcli import AzCli
from .errors import CliError, CliExit
from .github import run_github, run_official_github, should_delegate
from .graphql import graphql_api
from .identity import account, configured_username, profile
from .prs import (
    api_pull_request_comment,
    comment_pr,
    diff_pr,
    is_pull_request_comment_endpoint,
    list_prs,
    show_pr,
)
from .repository import resolve


BARE_HELP = """Work seamlessly with GitHub from the command line.

USAGE
  gh <command> <subcommand> [flags]

CORE COMMANDS
  auth:          Authenticate gh and git with GitHub
  browse:        Open repositories, issues, pull requests, and more in the browser
  codespace:     Connect to and manage codespaces
  discussion:    Work with GitHub Discussions (preview)
  gist:          Manage gists
  issue:         Manage issues
  org:           Manage organizations
  pr:            Manage pull requests
  project:       Work with GitHub Projects.
  release:       Manage releases
  repo:          Manage repositories
  skill:         Install and manage agent skills (preview)

GITHUB ACTIONS COMMANDS
  cache:         Manage GitHub Actions caches
  run:           View details about workflow runs
  workflow:      View details about GitHub Actions workflows

ALIAS COMMANDS
  co:            Alias for "pr checkout"

ADDITIONAL COMMANDS
  agent-task:    Work with agent tasks (preview)
  alias:         Create command shortcuts
  api:           Make an authenticated GitHub API request
  attestation:   Work with artifact attestations
  completion:    Generate shell completion scripts
  config:        Manage configuration for gh
  copilot:       Run the GitHub Copilot CLI (preview)
  extension:     Manage gh extensions
  gpg-key:       Manage GPG keys
  label:         Manage labels
  licenses:      View third-party license information
  preview:       Execute previews for gh features
  ruleset:       View info about repo rulesets
  search:        Search for repositories, issues, and pull requests
  secret:        Manage GitHub secrets
  ssh-key:       Manage SSH keys
  status:        Print information about relevant issues, pull requests, and notifications across repositories
  variable:      Manage GitHub Actions variables

HELP TOPICS
  accessibility: Learn about GitHub CLI's accessibility experiences
  actions:       Learn about working with GitHub Actions
  environment:   Environment variables that can be used with gh
  exit-codes:    Exit codes used by gh
  formatting:    Formatting options for JSON data exported from gh
  mintty:        Information about using gh with MinTTY
  reference:     A comprehensive reference of all gh commands
  telemetry:     Information about telemetry in gh

FLAGS
  --help      Show help for command
  --version   Show gh version

EXAMPLES
  $ gh issue create
  $ gh repo clone cli/cli
  $ gh pr checkout 321

LEARN MORE
  Use `gh <command> <subcommand> --help` for more information about a command.
  Read the manual at https://cli.github.com/manual
  Learn about exit codes using `gh help exit-codes`
  Learn about accessibility experiences using `gh help accessibility`

"""

AUTH_HELP = """Authenticate gh and git with GitHub

USAGE
  gh auth <command> [flags]

AVAILABLE COMMANDS
  login:         Log in to a GitHub account
  logout:        Log out of a GitHub account
  refresh:       Refresh stored authentication credentials
  setup-git:     Setup git with GitHub CLI
  status:        Display active account and authentication state on each known GitHub host
  switch:        Switch active GitHub account
  token:         Print the authentication token gh uses for a hostname and account

INHERITED FLAGS
  --help   Show help for command

LEARN MORE
  Use `gh <command> <subcommand> --help` for more information about a command.
  Read the manual at https://cli.github.com/manual
  Learn about exit codes using `gh help exit-codes`
  Learn about accessibility experiences using `gh help accessibility`

"""

PR_HELP = """Work with GitHub pull requests.

USAGE
  gh pr <command> [flags]

GENERAL COMMANDS
  create:        Create a pull request
  list:          List pull requests in a repository
  status:        Show status of relevant pull requests

TARGETED COMMANDS
  checkout:      Check out a pull request in git
  checks:        Show CI status for a single pull request
  close:         Close a pull request
  comment:       Add a comment to a pull request
  diff:          View changes in a pull request
  edit:          Edit a pull request
  lock:          Lock pull request conversation
  merge:         Merge a pull request
  ready:         Mark a pull request as ready for review
  reopen:        Reopen a pull request
  revert:        Revert a pull request
  review:        Add a review to a pull request
  unlock:        Unlock pull request conversation
  update-branch: Update a pull request branch
  view:          View a pull request

FLAGS
  -R, --repo [HOST/]OWNER/REPO   Select another repository using the [HOST/]OWNER/REPO format

INHERITED FLAGS
  --help   Show help for command

ARGUMENTS
  A pull request can be supplied as argument in any of the following formats:
  - by number, e.g. "123";
  - by URL, e.g. "https://github.com/OWNER/REPO/pull/123"; or
  - by the name of its head branch, e.g. "patch-1" or "OWNER:patch-1".

EXAMPLES
  $ gh pr checkout 353
  $ gh pr create --fill
  $ gh pr view --web

LEARN MORE
  Use `gh <command> <subcommand> --help` for more information about a command.
  Read the manual at https://cli.github.com/manual
  Learn about exit codes using `gh help exit-codes`
  Learn about accessibility experiences using `gh help accessibility`

"""


class Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.help_text: str | None = None
        self._printed_help = ""

    def _print_message(self, message: str, file: Any = None) -> None:
        # Keep argparse help inside Recorder's output stream instead of writing
        # directly to sys.stdout.
        self._printed_help += message

    def format_help(self) -> str:
        return self.help_text if self.help_text is not None else super().format_help()

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0 and not message:
            message = self._printed_help or self.format_help()
        raise CliExit(status, message or "", "stdout" if status == 0 else "stderr")

    def error(self, message: str) -> None:
        raise CliError(f"{self.prog}: error: {message}")


def parser() -> Parser:
    root = Parser(prog="gh", description="A gh-compatible Azure DevOps client")
    root.help_text = BARE_HELP
    sub = root.add_subparsers(dest="command", parser_class=Parser)

    auth = sub.add_parser("auth")
    auth.help_text = AUTH_HELP
    auth_sub = auth.add_subparsers(dest="auth_command", parser_class=Parser)
    status = auth_sub.add_parser("status")
    status.add_argument("--active", action="store_true")
    status.add_argument("--hostname")

    api = sub.add_parser("api")
    api.add_argument("endpoint", nargs="?")
    api.add_argument("--hostname")
    api.add_argument("-X", "--method")
    api.add_argument("--jq")
    api.add_argument("-f", "--raw-field", action="append", dest="raw_fields", default=[])
    api.add_argument("-F", "--field", action="append", dest="typed_fields", default=[])

    pr = sub.add_parser("pr")
    pr.help_text = PR_HELP
    pr_sub = pr.add_subparsers(dest="pr_command", parser_class=Parser)
    pr_list = pr_sub.add_parser("list")
    pr_list.add_argument("--head")
    pr_list.add_argument("--base")
    pr_list.add_argument("--author")
    pr_list.add_argument("--state", choices=["open", "closed", "all", "merged"])
    pr_list.add_argument("--json", dest="json_fields")
    pr_list.add_argument("--jq")
    pr_list.add_argument("--limit", type=int)
    pr_list.add_argument("-R", "--repo")
    pr_diff = pr_sub.add_parser("diff")
    pr_diff.add_argument("number")
    pr_diff.add_argument("-R", "--repo")
    pr_comment = pr_sub.add_parser("comment")
    pr_comment.add_argument("number", nargs="?")
    pr_comment.add_argument("-R", "--repo")
    pr_comment.add_argument("-b", "--body")
    pr_comment.add_argument("-F", "--body-file")
    pr_comment.add_argument("--edit-last", action="store_true")
    pr_comment.add_argument("--delete-last", action="store_true")
    pr_comment.add_argument("--create-if-none", action="store_true")
    pr_comment.add_argument("--yes", action="store_true")
    pr_comment.add_argument("-e", "--editor", action="store_true")
    pr_comment.add_argument("-w", "--web", action="store_true")
    pr_comment.add_argument("--attach", action="append", default=[])
    pr_show = pr_sub.add_parser("view", aliases=["show"])
    pr_show.add_argument("number")
    pr_show.add_argument("-R", "--repo")
    pr_show.add_argument("--json", dest="json_fields")
    pr_show.add_argument("--jq")
    return root


def dispatch(argv: list[str], emit: Callable[[str, bytes], None]) -> int:
    root = parser()
    try:
        options = root.parse_args(argv)
    except CliExit as exc:
        if exc.message:
            emit(exc.stream, exc.message.encode("utf-8"))
        return exc.exit_code

    if should_delegate(argv):
        return run_github(argv, emit)

    if options.command is None:
        # Real gh treats a bare invocation as a help request and exits 0.
        emit("stdout", BARE_HELP.encode("utf-8"))
        return 0
    if options.command == "auth" and options.auth_command is None:
        emit("stdout", AUTH_HELP.encode("utf-8"))
        return 0
    if options.command == "pr" and options.pr_command is None:
        emit("stdout", PR_HELP.encode("utf-8"))
        return 0
    az = AzCli(emit)
    if options.command == "auth" and options.auth_command == "status":
        data = account(az)
        user = configured_username(data)
        # Callers using the gh-shaped contract commonly probe
        # ``auth status --hostname github.com`` before invoking PR commands.
        # Keep Azure as the default display host, but use the complete GitHub
        # status shape for the explicit compatibility host.
        github_host = (options.hostname or "").lower() == "github.com"
        host = options.hostname or "dev.azure.com"
        login = user.split("@", 1)[0] if github_host and "@" in user else user
        if user:
            if github_host:
                output = (
                    f"{host}\n"
                    f"  ✓ Logged in to {host} account {login} (keyring)\n"
                    "  - Active account: true\n"
                    "  - Git operations protocol: ssh\n"
                    "  - Token: gho_************************************\n"
                    "  - Token scopes: 'repo', 'read:org'\n"
                )
            else:
                output = (
                    f"{host}\n"
                    f"  ✓ Logged in to {host} account {user}\n"
                    "  - Active account: true\n"
                    "  - Git operations protocol: https\n"
                    "  - Token: Azure CLI credential\n"
                    "  - Token scopes: Azure DevOps permissions\n"
                )
            emit("stdout", output.encode("utf-8"))
        else:
            emit("stdout", f"{host}\n  ✗ Not logged in to {host}\n".encode("utf-8"))
            return 1
        return 0
    if options.command == "api":
        if options.endpoint is None:
            emit("stderr", b"accepts 1 arg(s), received 0\n")
            return 1
        if options.endpoint.strip("/") == "graphql":
            return graphql_api(
                az,
                resolve(),
                options.raw_fields,
                options.typed_fields,
                emit,
                hostname=options.hostname,
            )
        if is_pull_request_comment_endpoint(options.endpoint):
            return api_pull_request_comment(
                az,
                resolve(),
                options.endpoint,
                options.method,
                options.raw_fields,
                options.typed_fields,
                options.jq,
                emit,
            )
        if options.endpoint.strip("/") != "user":
            raise CliError("az-gh: only the gh api user and graphql endpoints are supported")
        data = account(az)
        user = configured_username(data)
        github_host = (options.hostname or "").lower() == "github.com"
        login = user.split("@", 1)[0] if github_host and "@" in user else user
        identity = profile(az, user, resolve().organization)
        identity_user = identity.get("user") if isinstance(identity.get("user"), dict) else {}
        identity_id = str(identity.get("id") or user or "azure-user")
        # GitHub's user endpoint exposes a numeric id. Azure identities use a
        # GUID, so retain the Azure id in node_id and derive a stable numeric
        # compatibility id for clients that deserialize the GitHub shape.
        # Keep the compatibility id within JavaScript's safe integer range.
        # The app consuming this GitHub-shaped record parses JSON numbers as
        # IEEE-754 doubles; an unrestricted 64-bit hash would silently lose
        # precision and can make the otherwise valid user payload fail its
        # schema/identity checks.
        numeric_id = int.from_bytes(hashlib.sha256(identity_id.encode("utf-8")).digest()[:8], "big") % (2**53 - 1)
        user_url = str(identity_user.get("url") or "https://dev.azure.com/")
        api_root = user_url.split("/_apis/", 1)[0].rstrip("/")
        azure_api_root = api_root
        if github_host:
            # The caller uses this endpoint as a GitHub account probe. Azure
            # identity URLs are valid for Azure CLI users but are not
            # parseable as GitHub source URLs, especially with modern
            # dev.azure.com identity hosts.
            api_root = "https://api.github.com"
            user_url = f"{api_root}/users/{quote(login, safe='')}"
            html_url = f"https://github.com/{quote(login, safe='')}"
            user_path = f"/users/{quote(login, safe='')}"
        else:
            html_url = user_url
            user_path = f"/users/{user}"
        result: Any = {
            "login": login,
            "id": numeric_id,
            "node_id": identity_id,
            "avatar_url": str(identity_user.get("imageUrl") or f"{azure_api_root}/_apis/Graph/Users/{identity_id}/image"),
            "gravatar_id": "",
            "url": user_url,
            "html_url": html_url,
            "followers_url": f"{api_root}{user_path}/followers",
            "following_url": f"{api_root}{user_path}/following{{/other_user}}",
            "gists_url": f"{api_root}{user_path}/gists{{/gist_id}}" if github_host else f"{api_root}/gists{{/gist_id}}",
            "starred_url": f"{api_root}{user_path}/starred{{/owner}}{{/repo}}" if github_host else f"{api_root}/starred{{/owner}}{{/repo}}",
            "subscriptions_url": f"{api_root}{user_path}/subscriptions" if github_host else f"{api_root}/subscriptions",
            "organizations_url": f"{api_root}{user_path}/orgs" if github_host else f"{api_root}/orgs",
            "repos_url": f"{api_root}{user_path}/repos" if github_host else f"{api_root}/repos",
            "events_url": f"{api_root}{user_path}/events{{/privacy}}" if github_host else f"{api_root}/events{{/privacy}}",
            "received_events_url": f"{api_root}{user_path}/received_events" if github_host else f"{api_root}/received_events",
            "type": "User",
            "user_view_type": "public",
            "site_admin": False,
            "name": identity_user.get("displayName") or user,
            "company": None,
            "blog": "",
            "location": None,
            "email": None,
            "hireable": None,
            "bio": "",
            "twitter_username": None,
            "notification_email": None,
            "public_repos": 0,
            "public_gists": 0,
            "followers": 0,
            "following": 0,
            "created_at": "",
            "updated_at": "",
        }
        if options.jq:
            from .prs import apply_jq

            result = apply_jq(result, options.jq)
        if options.jq and isinstance(result, (str, int, float, bool)):
            output = str(result).lower() if isinstance(result, bool) else str(result)
        else:
            output = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        emit("stdout", (output + "\n").encode("utf-8"))
        return 0
    if options.command == "pr":
        if options.pr_command not in {"list", "diff", "comment", "view", "show"}:
            raise CliError("gh pr: a subcommand is required")
        ctx = resolve(getattr(options, "repo", None))
        if options.pr_command == "list":
            return list_prs(az, ctx, options, emit)
        if options.pr_command == "diff":
            return diff_pr(az, ctx, options.number, emit)
        if options.pr_command == "comment":
            return comment_pr(az, ctx, options, emit)
        return show_pr(az, ctx, options.number, options, emit)
    raise CliError(f"az-gh: unsupported command: {options.command}")


def main(argv: list[str] | None = None) -> int:
    import os
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("AZ_GH_PASSTHROUGH"):
        from .recording import Recorder

        return Recorder(args).run(lambda emit: run_official_github(args, emit))
    if args == ["--version"] or args == ["version"]:
        from .recording import Recorder

        version = f"gh version {__version__} (Azure DevOps)\n"
        release = f"https://github.com/cli/cli/releases/tag/v{__version__}\n"
        return Recorder(args).run(lambda emit: (emit("stdout", (version + release).encode("utf-8")) or 0))
    from .recording import Recorder

    return Recorder(args).run(lambda emit: dispatch(args, emit))
