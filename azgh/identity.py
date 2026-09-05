from __future__ import annotations

from typing import Any

from .azcli import AzCli
from .errors import CliError


def account(az: AzCli) -> dict[str, Any]:
    value = az.json(["account", "show"])
    return value if isinstance(value, dict) else {}


def username(account_data: dict[str, Any]) -> str:
    user = account_data.get("user")
    if isinstance(user, dict):
        return str(user.get("name") or user.get("id") or "")
    return str(user or "")


def configured_username(account_data: dict[str, Any]) -> str:
    import os

    return os.environ.get("AZ_GH_AZDO_USER") or os.environ.get("AZDO_USER") or username(account_data)


def profile(az: AzCli, user: str, organization: str | None = None) -> dict[str, Any]:
    """Return the Azure DevOps identity used to fill the gh api user shape."""
    if not user:
        return {}
    args = ["devops", "user", "show", "--user", user]
    if organization:
        args += ["--organization", organization]
    try:
        # The identity is enrichment only.  A missing/invalid Azure identity
        # lookup must not turn a successful gh api user probe into a mixed
        # stdout/stderr response that callers interpret as unavailable.
        value = az.json(args, emit_stderr=False)
    except CliError:
        return {}
    return value if isinstance(value, dict) else {}
