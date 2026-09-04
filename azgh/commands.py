from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from . import __version__
from .azcli import AzCli
from .errors import CliError, CliExit
from .identity import account, configured_username
from .prs import diff_pr, list_prs, show_pr
from .repository import resolve


class Parser(argparse.ArgumentParser):
    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise CliExit(status, message or "", "stdout" if status == 0 else "stderr")

    def error(self, message: str) -> None:
        raise CliError(f"{self.prog}: error: {message}")


def parser() -> Parser:
    root = Parser(prog="gh", description="A gh-compatible Azure DevOps client")
    sub = root.add_subparsers(dest="command", parser_class=Parser)

    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", parser_class=Parser)
    status = auth_sub.add_parser("status")
    status.add_argument("--active", action="store_true")
    status.add_argument("--hostname")

    api = sub.add_parser("api")
    api.add_argument("endpoint")
    api.add_argument("--hostname")
    api.add_argument("--jq")

    pr = sub.add_parser("pr")
    pr_sub = pr.add_subparsers(dest="pr_command", parser_class=Parser)
    pr_list = pr_sub.add_parser("list")
    pr_list.add_argument("--head")
    pr_list.add_argument("--base")
    pr_list.add_argument("--author")
    pr_list.add_argument("--state", choices=["open", "closed", "all", "merged"])
    pr_list.add_argument("--json", dest="json_fields")
    pr_list.add_argument("--jq")
    pr_list.add_argument("--limit", type=int)
    pr_list.add_argument("--repo")
    pr_diff = pr_sub.add_parser("diff")
    pr_diff.add_argument("number")
    pr_diff.add_argument("--repo")
    pr_show = pr_sub.add_parser("view", aliases=["show"])
    pr_show.add_argument("number")
    pr_show.add_argument("--repo")
    pr_show.add_argument("--json", dest="json_fields")
    return root


def dispatch(argv: list[str], emit: Callable[[str, bytes], None]) -> int:
    root = parser()
    try:
        options = root.parse_args(argv)
    except CliExit as exc:
        if exc.message:
            emit(exc.stream, exc.message.encode("utf-8"))
        return exc.exit_code

    if options.command is None:
        raise CliError("gh: no command specified; try gh --help")
    az = AzCli(emit)
    if options.command == "auth" and options.auth_command == "status":
        data = account(az)
        user = configured_username(data)
        host = "dev.azure.com"
        if user:
            emit("stdout", f"{host}\n  ✓ Logged in to {host} account {user}\n  - Active account: true\n".encode("utf-8"))
        else:
            emit("stdout", f"{host}\n  ✗ Not logged in to {host}\n".encode("utf-8"))
            return 1
        return 0
    if options.command == "api":
        if options.endpoint.strip("/") != "user":
            raise CliError("az-gh: only the gh api user endpoint is supported")
        data = account(az)
        user = configured_username(data)
        result: Any = {
            "login": user,
            "id": (data.get("user") or {}).get("id") if isinstance(data.get("user"), dict) else None,
            "name": user,
            "url": "https://dev.azure.com/",
        }
        if options.jq:
            from .prs import apply_jq

            result = apply_jq(result, options.jq)
        emit("stdout", (json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
        return 0
    if options.command == "pr":
        if options.pr_command not in {"list", "diff", "view", "show"}:
            raise CliError("gh pr: a subcommand is required")
        ctx = resolve(getattr(options, "repo", None))
        if options.pr_command == "list":
            return list_prs(az, ctx, options, emit)
        if options.pr_command == "diff":
            return diff_pr(az, ctx, options.number, emit)
        return show_pr(az, ctx, options.number, options, emit)
    raise CliError(f"az-gh: unsupported command: {options.command}")


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"] or args == ["version"]:
        from .recording import Recorder

        return Recorder(args).run(lambda emit: (emit("stdout", f"gh version {__version__} (Azure DevOps)\n".encode("utf-8")) or 0))
    from .recording import Recorder

    return Recorder(args).run(lambda emit: dispatch(args, emit))
