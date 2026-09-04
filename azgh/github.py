from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable

from .repository import git_remote, is_github_remote, is_github_url


def _github_cli() -> str | None:
    wrapper = Path(__file__).resolve().parents[1] / "gh"
    candidates = [os.environ.get("AZ_GH_GITHUB_CLI"), "/usr/bin/gh", "/usr/local/bin/gh", shutil.which("gh")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if Path(candidate).resolve() != wrapper.resolve():
                return candidate
        except OSError:
            continue
    return None


def _option_value(argv: list[str], option: str) -> str | None:
    try:
        index = argv.index(option)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def should_delegate(argv: list[str]) -> bool:
    # A custom Azure executable is primarily used to force Azure behavior in
    # tests and integrations, even when the current checkout has a GitHub remote.
    if os.environ.get("AZ_GH_AZ"):
        return False
    if not _github_cli():
        return False
    if "--hostname" in argv:
        hostname = _option_value(argv, "--hostname")
        if hostname == "github.com" or (hostname and hostname.endswith(".github.com")):
            return True

    command = argv[0] if argv else ""
    if command == "pr":
        repo = _option_value(argv, "--repo")
        if repo:
            if repo.startswith(("http://", "https://")):
                return is_github_url(repo)
            return is_github_remote()
        return is_github_remote()
    if command == "api":
        return is_github_remote()
    if command == "auth":
        return _option_value(argv, "--hostname") in {None, "github.com"}
    return False


def run_github(argv: list[str], emit: Callable[[str, bytes], None]) -> int:
    executable = _github_cli()
    if not executable:
        return 127
    try:
        result = subprocess.run(
            [executable, *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        emit("stderr", f"az-gh: failed to execute GitHub CLI: {exc}\n".encode("utf-8"))
        return 126
    if result.stdout:
        emit("stdout", result.stdout)
    if result.stderr:
        emit("stderr", result.stderr)
    return result.returncode
