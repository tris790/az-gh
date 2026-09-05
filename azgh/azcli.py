from __future__ import annotations

import json
import os
from subprocess import CompletedProcess
import subprocess
import tempfile
from typing import Any, Callable

from .errors import CliError


AZURE_CLI_TIMEOUT_SECONDS = 30


class AzCli:
    """Small subprocess adapter; Azure CLI remains the auth/data boundary."""

    def __init__(self, emit: Callable[[str, bytes], None]) -> None:
        self.emit = emit
        self.executable = os.environ.get("AZ_GH_AZ", "az")

    def run(self, args: list[str]) -> CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.executable, *args, "--only-show-errors"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=AZURE_CLI_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise CliError(
                "az-gh: Azure CLI executable not found; install az and the azure-devops extension",
                127,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CliError("az-gh: Azure CLI request timed out", 124) from exc
        except OSError as exc:
            raise CliError(f"az-gh: failed to execute {self.executable}: {exc}", 126) from exc

    def json(
        self,
        args: list[str],
        *,
        emit_stderr: bool = True,
        payload: Any = None,
    ) -> Any:
        input_path: str | None = None
        if payload is not None:
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".json", delete=False
                ) as input_file:
                    json.dump(payload, input_file, ensure_ascii=False, separators=(",", ":"))
                    input_path = input_file.name
            except OSError as exc:
                raise CliError(f"az-gh: could not prepare Azure CLI request body: {exc}") from exc
            args = [*args, "--in-file", input_path]

        try:
            result = self.run([*args, "--output", "json"])
            if result.returncode != 0:
                if emit_stderr and result.stderr:
                    self.emit("stderr", result.stderr)
                message = result.stderr.decode("utf-8", "replace").strip() or (
                    f"Azure CLI exited with status {result.returncode}"
                )
                raise CliError(message, result.returncode)
            if emit_stderr and result.stderr:
                self.emit("stderr", result.stderr)
            try:
                return json.loads(result.stdout.decode("utf-8")) if result.stdout.strip() else None
            except json.JSONDecodeError as exc:
                raise CliError(f"az-gh: Azure CLI returned invalid JSON: {exc}") from exc
        finally:
            if input_path is not None:
                try:
                    os.unlink(input_path)
                except OSError:
                    pass
