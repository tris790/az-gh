from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable

from .repository import git_remote, is_github_remote, is_github_url


OFFICIAL_GH_PATH = "/usr/bin/gh"


def run_official_github(argv: list[str], emit: Callable[[str, bytes], None]) -> int:
    """Run the system GitHub CLI while recording its output."""
    try:
        result = subprocess.run(
            [OFFICIAL_GH_PATH, *argv],
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        message = f"az-gh: failed to execute official GitHub CLI at {OFFICIAL_GH_PATH}: {exc}\n"
        emit("stderr", message.encode("utf-8"))
        error_code = 126 if exc.errno in {errno.EACCES, errno.ENOEXEC} else 127
        return error_code
    if result.stdout:
        emit("stdout", result.stdout)
    if result.stderr:
        emit("stderr", result.stderr)
    return result.returncode


def provider() -> str:
    """Return the backend selected for this compatibility command.

    The executable is named ``gh`` so callers do not need to change, but this
    project is an Azure DevOps adapter. Repository remotes and GitHub-shaped
    flags therefore cannot safely select GitHub implicitly: an Azure-backed
    checkout may still live in a GitHub repository, and existing callers often
    pass ``--hostname github.com`` unconditionally.
    """
    value = os.environ.get("AZ_GH_PROVIDER", "azure").strip().lower()
    if value in {"github", "gh"}:
        return "github"
    return "azure"


def _github_cli() -> str | None:
    wrapper = Path(__file__).resolve().parents[1] / "gh"
    candidates = [os.environ.get("AZ_GH_GITHUB_CLI"), OFFICIAL_GH_PATH, "/usr/local/bin/gh", shutil.which("gh")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if Path(candidate).resolve() != wrapper.resolve():
                return candidate
        except OSError:
            continue
    return None


def _option_value(argv: list[str], option: str, *aliases: str) -> str | None:
    for index, argument in enumerate(argv):
        if argument in {option, *aliases}:
            return argv[index + 1] if index + 1 < len(argv) else None
    return None


def should_delegate(argv: list[str]) -> bool:
    # Azure is the default backend. GitHub forwarding is opt-in because the
    # compatibility command is commonly run from a GitHub checkout and callers
    # may pass --hostname github.com even when they want Azure data.
    if os.environ.get("AZ_GH_AZ") or provider() != "github":
        return False
    if not _github_cli():
        return False
    if "--hostname" in argv:
        hostname = _option_value(argv, "--hostname")
        if hostname == "github.com" or (hostname and hostname.endswith(".github.com")):
            return True

    command = argv[0] if argv else ""
    if command == "pr":
        repo = _option_value(argv, "--repo", "-R")
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
