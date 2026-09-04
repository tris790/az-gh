from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlparse

from .errors import CliError


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def normalize_org(value: str | None) -> str | None:
    if not value:
        return None
    value = value.rstrip("/")
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://dev.azure.com/{value}"


class RepoContext:
    def __init__(self, organization: str | None, project: str | None, repository: str | None) -> None:
        self.organization = normalize_org(organization)
        self.project = project
        self.repository = repository

    def az_args(self) -> list[str]:
        args: list[str] = []
        if self.organization:
            args += ["--organization", self.organization]
        if self.project:
            args += ["--project", self.project]
        if self.repository:
            args += ["--repository", self.repository]
        return args


def parse_repo_flag(value: str) -> tuple[str | None, str | None, str | None]:
    if value.startswith("http://") or value.startswith("https://"):
        return parse_remote(value)
    parts = [unquote(part) for part in value.strip("/").split("/") if part]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        # gh's owner/repository maps naturally to Azure's project/repository.
        return None, parts[0], parts[1]
    raise CliError("az-gh: --repo must be PROJECT/REPOSITORY or ORGANIZATION/PROJECT/REPOSITORY")


def parse_remote(remote: str) -> tuple[str | None, str | None, str | None]:
    remote = remote.strip()
    ssh = re.match(r"^git@ssh\.dev\.azure\.com:v3/([^/]+)/([^/]+)/(.+?)/?$", remote)
    if ssh:
        return ssh.group(1), ssh.group(2), ssh.group(3)

    parsed = urlparse(remote)
    host = parsed.hostname or ""
    path = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if host == "dev.azure.com" and "_git" in path:
        marker = path.index("_git")
        if marker >= 2 and marker + 1 < len(path):
            return path[0], "/".join(path[1:marker]), path[marker + 1]
    if host.endswith(".visualstudio.com") and "_git" in path:
        marker = path.index("_git")
        if marker >= 1 and marker + 1 < len(path):
            return host.split(".", 1)[0], "/".join(path[:marker]), path[marker + 1]
    return None, None, None


def git_remote(cwd: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def resolve(repo_flag: str | None = None, cwd: Path | None = None) -> RepoContext:
    organization = _first_env("AZ_GH_AZDO_ORG", "AZDO_ORG_URL", "AZURE_DEVOPS_ORG_URL")
    project = _first_env("AZ_GH_AZDO_PROJECT", "AZDO_PROJECT", "AZURE_DEVOPS_PROJECT")
    repository = _first_env("AZ_GH_AZDO_REPOSITORY", "AZDO_REPOSITORY", "AZURE_DEVOPS_REPOSITORY")

    remote = git_remote(cwd)
    if remote:
        remote_org, remote_project, remote_repository = parse_remote(remote)
        organization = organization or remote_org
        project = project or remote_project
        repository = repository or remote_repository
    if repo_flag:
        flag_org, flag_project, flag_repository = parse_repo_flag(repo_flag)
        organization = flag_org or organization
        project = flag_project or project
        repository = flag_repository or repository
    return RepoContext(organization, project, repository)
