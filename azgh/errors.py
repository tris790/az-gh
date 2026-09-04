from __future__ import annotations


class CliError(Exception):
    """An expected command-line failure with a process exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class CliExit(Exception):
    """An argparse-style successful or unsuccessful exit with captured text."""

    def __init__(self, exit_code: int, message: str, stream: str = "stdout") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
        self.stream = stream
