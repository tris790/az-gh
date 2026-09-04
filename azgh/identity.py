from __future__ import annotations

from typing import Any

from .azcli import AzCli


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
