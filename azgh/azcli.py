from __future__ import annotations

import json
import os
from subprocess import CompletedProcess
import subprocess
from typing import Any, Callable

from .errors import CliError


class AzCli:
    """Small subprocess adapter; Azure CLI remains the auth/data boundary."""

    def __init__(self, emit: Callable[[str, bytes], None]) -> None:
        self.emit = emit
        self.executable = os.environ.get("AZ_GH_AZ", "az")

    def run(self, args: list[str]) -> CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.executable, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CliError(
                "az-gh: Azure CLI executable not found; install az and the azure-devops extension",
                127,
            ) from exc
        except OSError as exc:
            raise CliError(f"az-gh: failed to execute {self.executable}: {exc}", 126) from exc

    def json(self, args: list[str]) -> Any:
        result = self.run([*args, "--output", "json"])
        if result.returncode != 0:
            if result.stderr:
                self.emit("stderr", result.stderr)
            message = result.stderr.decode("utf-8", "replace").strip() or (
                f"Azure CLI exited with status {result.returncode}"
            )
            raise CliError(message, result.returncode)
        if result.stderr:
            self.emit("stderr", result.stderr)
        try:
            return json.loads(result.stdout.decode("utf-8")) if result.stdout.strip() else None
        except json.JSONDecodeError as exc:
            raise CliError(f"az-gh: Azure CLI returned invalid JSON: {exc}") from exc
