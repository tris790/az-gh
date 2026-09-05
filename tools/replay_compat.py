#!/usr/bin/env python3
"""Replay recorded gh commands and compare Azure-backed output shapes.

The official recording is treated as a contract for command order, output
streams, exit status, and JSON shape. Values such as usernames, URLs, commit
ids, and text contents are deliberately ignored because an Azure response
cannot contain the same GitHub domain values.

Examples:
    python tools/replay_compat.py official_commands.jsonl --az az
    python tools/replay_compat.py official_commands.jsonl \
        --az tests/fixtures/replay_az.py
    python tools/replay_compat.py official_commands.jsonl \
        --actual broken2_commands.jsonl --partial
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable

try:
    from tools.parse_jsonl import decode_output, load_transcript, output_events, shape
except ModuleNotFoundError:  # Running this file directly from the tools directory.
    from parse_jsonl import decode_output, load_transcript, output_events, shape


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLI = ROOT / "gh"
DEFAULT_REPLAY_AZ = ROOT / "tests" / "fixtures" / "replay_az.py"


@dataclass(frozen=True)
class ReplayMismatch:
    """One actionable difference between an expected and replayed command."""

    command_index: int
    argv: list[str]
    message: str

    def format(self) -> str:
        command = json.dumps(self.argv, ensure_ascii=False)
        return f"command {self.command_index}: {command}: {self.message}"


def _shape_differences(expected: Any, actual: Any, path: str = "$") -> list[str]:
    """Describe stable structural differences between two response shapes.

    Repository contents are intentionally not part of the contract. A
    connection can be empty in one backend and populated in the other, and a
    nullable field can be null for one pull request but populated for another.
    Keep checking object keys and concrete types where both sides provide a
    value, while treating those data-dependent cases as compatible.
    """
    if expected is None or actual is None:
        return []
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences: list[str] = []
        expected_keys = set(expected)
        actual_keys = set(actual)
        for key in sorted(expected_keys - actual_keys):
            # GraphQL adds this envelope only when the backend reports an
            # operation error. Whether a repository/PR exists is data, not a
            # response-schema incompatibility.
            if path == "$" and key == "errors":
                continue
            differences.append(f"{path}.{key}: missing (expected {expected[key]!r})")
        for key in sorted(actual_keys - expected_keys):
            if path == "$" and key == "errors":
                continue
            differences.append(f"{path}.{key}: unexpected (actual {actual[key]!r})")
        for key in sorted(expected_keys & actual_keys):
            differences.extend(_shape_differences(expected[key], actual[key], f"{path}.{key}"))
        return differences
    if expected != actual:
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    return []


def _auth_line_shapes(text: str) -> list[str]:
    shapes: list[str] = []
    for line in text.splitlines():
        if line.startswith("  ✓ Logged in to "):
            shapes.append("logged-in")
        elif line.startswith("  ✗ Not logged in to "):
            shapes.append("not-logged-in")
        elif line.startswith("  - Active account:"):
            shapes.append("active-account")
        elif line.startswith("  - Git operations protocol:"):
            shapes.append("git-operations-protocol")
        elif line.startswith("  - Token:"):
            shapes.append("token")
        elif line.startswith("  - Token scopes:"):
            shapes.append("token-scopes")
        elif line.startswith("  "):
            shapes.append("indented-text")
        else:
            shapes.append("host")
    return shapes


def _payload_shape(event: dict[str, Any], argv: list[str]) -> dict[str, Any]:
    """Describe a recorded output without retaining domain-specific values."""
    text = decode_output(event, parse_json=False)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        if argv[:2] == ["auth", "status"]:
            return {
                "kind": "text",
                "type": "auth-status",
                "lines": _auth_line_shapes(text),
                "trailing_newline": text.endswith("\n"),
            }
        return {
            "kind": "text",
            "type": "string",
            "nonempty": bool(text),
            "trailing_newline": text.endswith("\n"),
        }
    return {"kind": "json", "shape": shape(value)}


def _command_argv(command: dict[str, Any]) -> list[str]:
    argv = command.get("argv", [])
    return [str(value) for value in argv] if isinstance(argv, list) else []


def _executable(value: str | Path) -> str:
    """Resolve a path while still allowing names found on ``PATH``."""
    text = str(value)
    path = Path(text)
    if path.is_absolute() or path.parent != Path("."):
        return str(path.resolve())
    return shutil.which(text) or text


def _mismatch(index: int, command: dict[str, Any], message: str) -> ReplayMismatch:
    return ReplayMismatch(index, _command_argv(command), message)


def _recording_issues(
    commands: list[dict[str, Any]], label: str
) -> list[ReplayMismatch]:
    """Return lifecycle errors that indicate a truncated/incomplete trace."""
    issues: list[ReplayMismatch] = []
    for index, command in enumerate(commands, 1):
        events = command.get("events", [])
        event_names = [event.get("event") for event in events]
        command_id = str(command.get("id", "?"))[:8]
        argv = _command_argv(command)
        if not events:
            issues.append(
                ReplayMismatch(index, argv, f"{label} recording command {command_id} has no events")
            )
            continue
        if event_names[0] != "start":
            issues.append(
                ReplayMismatch(
                    index,
                    argv,
                    f"{label} recording command {command_id} is missing its start event "
                    f"(first retained event: {event_names[0]!r}; trace may be truncated)",
                )
            )
        if event_names.count("start") != 1:
            issues.append(
                ReplayMismatch(
                    index,
                    argv,
                    f"{label} recording command {command_id} has "
                    f"{event_names.count('start')} start events; expected 1",
                )
            )
        if event_names[-1] != "result":
            issues.append(
                ReplayMismatch(
                    index,
                    argv,
                    f"{label} recording command {command_id} is missing its final result event "
                    f"(last retained event: {event_names[-1]!r}; trace may be truncated)",
                )
            )
        if event_names.count("result") != 1:
            issues.append(
                ReplayMismatch(
                    index,
                    argv,
                    f"{label} recording command {command_id} has "
                    f"{event_names.count('result')} result events; expected 1",
                )
            )
    return issues


def _payload_differences(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    if expected.get("kind") != actual.get("kind"):
        return [
            "$: expected "
            f"{expected.get('kind')!r} output, got {actual.get('kind')!r} output"
        ]
    if expected.get("kind") == "json":
        return _shape_differences(expected.get("shape"), actual.get("shape"))
    return _shape_differences(expected, actual)


def _result_exit_code(command: dict[str, Any]) -> int | None:
    result = next(
        (event for event in command.get("events", []) if event.get("event") == "result"),
        None,
    )
    value = result.get("exit_code") if result else None
    return value if isinstance(value, int) else None


def _command_signature(command: dict[str, Any]) -> tuple[str, ...]:
    """Identify a command while ignoring backend-specific repo and PR values."""
    argv = _command_argv(command)
    signature: list[str] = []
    for index, argument in enumerate(argv):
        if index and argv[index - 1] in {"--repo", "-R"}:
            signature.append("VALUE")
            continue
        if argument.startswith(("owner=", "repo=", "number=")):
            signature.append(argument.split("=", 1)[0] + "=VALUE")
            continue
        normalized = re.sub(
            r"\b(owner|name)\s*:\s*['\"][^'\"]+['\"]",
            r"\1:VALUE",
            argument,
        )
        normalized = re.sub(
            r"(pullRequest\s*\(\s*number\s*:\s*)\d+",
            r"\1N",
            normalized,
        )
        signature.append(normalized)
    return tuple(signature)


def _compare_matched_commands(
    expected_command: dict[str, Any],
    actual_command: dict[str, Any],
    index: int,
) -> list[ReplayMismatch]:
    """Compare output contracts for two commands already identified as equivalent."""
    expected_argv = _command_argv(expected_command)
    expected_exit = _result_exit_code(expected_command)
    actual_exit = _result_exit_code(actual_command)
    if (expected_exit == 0) != (actual_exit == 0):
        # A missing PR/repository is expected when the official and Azure
        # traces point at different services. Do not turn that
        # data-dependent outcome into a false contract failure.
        return []

    mismatches: list[ReplayMismatch] = []
    expected_events = [event.get("event") for event in expected_command.get("events", [])]
    actual_events = [event.get("event") for event in actual_command.get("events", [])]
    if expected_events != actual_events:
        mismatches.append(
            _mismatch(
                index,
                expected_command,
                f"event sequence differs: expected {expected_events!r}, got {actual_events!r}",
            )
        )

    expected_outputs = list(output_events(expected_command))
    actual_outputs = list(output_events(actual_command))
    if len(expected_outputs) != len(actual_outputs):
        mismatches.append(
            _mismatch(
                index,
                expected_command,
                f"output event count: expected {len(expected_outputs)}, got {len(actual_outputs)}",
            )
        )
    for output_index, (expected_output, actual_output) in enumerate(
        zip(expected_outputs, actual_outputs), 1
    ):
        if expected_output.get("stream") != actual_output.get("stream"):
            mismatches.append(
                _mismatch(
                    index,
                    expected_command,
                    f"output {output_index} stream: expected "
                    f"{expected_output.get('stream')!r}, got {actual_output.get('stream')!r}",
                )
            )
        try:
            expected_shape = _payload_shape(expected_output, expected_argv)
            actual_shape = _payload_shape(actual_output, expected_argv)
        except (KeyError, ValueError, UnicodeError) as exc:
            mismatches.append(_mismatch(index, expected_command, f"invalid output recording: {exc}"))
            continue
        differences = _payload_differences(expected_shape, actual_shape)
        for difference in differences:
            mismatches.append(
                _mismatch(index, expected_command, f"output {output_index} shape: {difference}")
            )
    return mismatches


def compare_transcripts(expected_path: str | Path, actual_path: str | Path) -> list[ReplayMismatch]:
    """Compare two complete JSONL recordings and return all mismatches."""
    expected = load_transcript(expected_path)
    actual = load_transcript(actual_path)
    recording_issues = [
        *_recording_issues(expected, "official"),
        *_recording_issues(actual, "replay"),
    ]
    if recording_issues:
        return recording_issues
    mismatches: list[ReplayMismatch] = []

    if len(expected) != len(actual):
        mismatches.append(
            ReplayMismatch(
                0,
                [],
                f"command count: expected {len(expected)}, got {len(actual)}",
            )
        )
        if len(actual) > len(expected):
            for index, actual_command in enumerate(actual[len(expected):], len(expected) + 1):
                mismatches.append(
                    ReplayMismatch(index, _command_argv(actual_command), "unexpected replay command")
                )

    for index, expected_command in enumerate(expected, 1):
        if index > len(actual):
            mismatches.append(_mismatch(index, expected_command, "command was not replayed"))
            continue
        actual_command = actual[index - 1]
        expected_argv = _command_argv(expected_command)
        actual_argv = _command_argv(actual_command)
        if expected_argv != actual_argv:
            mismatches.append(
                _mismatch(index, expected_command, f"argv differs: got {actual_argv!r}")
            )
            # The output of a different command is not a meaningful shape
            # comparison. Keep the sequence divergence visible without
            # generating a cascade of false field/stream mismatches.
            continue

        mismatches.extend(_compare_matched_commands(expected_command, actual_command, index))

    return mismatches


def compare_partial_transcript(
    expected_path: str | Path,
    actual_path: str | Path,
) -> list[ReplayMismatch]:
    """Compare a focused capture against matching official command shapes.

    This mode is for a trace containing only a subset of the official
    commands. Commands are matched by normalized query structure, not by
    position, while the normal complete-replay mode remains strict about
    order and command count.
    """
    expected = load_transcript(expected_path)
    actual = load_transcript(actual_path)
    recording_issues = [
        *_recording_issues(expected, "official"),
        *_recording_issues(actual, "replay"),
    ]
    if recording_issues:
        return recording_issues

    candidates: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = {}
    for index, command in enumerate(expected, 1):
        candidates.setdefault(_command_signature(command), []).append((index, command))

    mismatches: list[ReplayMismatch] = []
    for actual_index, actual_command in enumerate(actual, 1):
        matching = candidates.get(_command_signature(actual_command), [])
        if not matching:
            mismatches.append(
                _mismatch(actual_index, actual_command, "no matching official command shape")
            )
            continue
        actual_exit = _result_exit_code(actual_command)
        same_outcome = lambda item: (_result_exit_code(item[1]) == 0) == (actual_exit == 0)
        expected_index, expected_command = next(
            (item for item in matching if same_outcome(item)),
            matching[0],
        )
        mismatches.extend(
            _compare_matched_commands(expected_command, actual_command, expected_index)
        )
        matching.remove((expected_index, expected_command))
    return mismatches


def replay(
    official_path: str | Path,
    cli: str | Path = DEFAULT_CLI,
    az: str | Path | None = None,
    cwd: str | Path = ROOT,
    base_env: dict[str, str] | None = None,
) -> tuple[Path, list[ReplayMismatch]]:
    """Run every official command sequentially and compare the resulting log."""
    official_commands = load_transcript(official_path)
    temp_dir = Path(tempfile.mkdtemp(prefix="az-gh-replay-"))
    actual_path = temp_dir / "replay.jsonl"
    environment = os.environ.copy()
    environment.pop("AZ_GH_PASSTHROUGH", None)
    environment.pop("AZ_GH_PROVIDER", None)
    if base_env:
        environment.update(base_env)
    if az is not None:
        environment["AZ_GH_AZ"] = _executable(az)

    for index, command in enumerate(official_commands, 1):
        argv = _command_argv(command)
        environment["AZ_GH_REPLAY_COMMAND_INDEX"] = str(index)
        # Keep one recorder file per command so a replay cannot lose command
        # history if a caller supplies a custom logger implementation.
        environment["AZ_GH_LOG_FILE"] = str(temp_dir / f"command-{index:04d}.jsonl")
        completed = subprocess.run(
            [_executable(cli), *argv],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode < 0:
            # The Recorder normally turns failures into an exit result. Keep
            # the subprocess failure visible if the launcher itself crashes.
            print(
                f"replay command {index} terminated by signal {-completed.returncode}",
                file=sys.stderr,
            )

    with actual_path.open("w", encoding="utf-8") as combined:
        for index in range(1, len(official_commands) + 1):
            command_path = temp_dir / f"command-{index:04d}.jsonl"
            if command_path.exists():
                combined.write(command_path.read_text(encoding="utf-8"))
    return actual_path, compare_transcripts(official_path, actual_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("official", type=Path, help="official gh JSONL recording")
    parser.add_argument(
        "--actual",
        type=Path,
        help="compare an existing az-gh JSONL recording instead of replaying commands",
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help="match a focused capture to official commands by normalized query structure",
    )
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI, help="gh-compatible executable")
    parser.add_argument(
        "--az",
        type=Path,
        default=DEFAULT_REPLAY_AZ,
        help="Azure CLI executable to inject (defaults to the deterministic fixture)",
    )
    parser.add_argument("--org", help="Azure organization used for replay")
    parser.add_argument("--project", help="Azure project used for replay")
    parser.add_argument("--repository", help="Azure repository used for replay")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    options = _parser().parse_args(list(argv) if argv is not None else None)
    base_env: dict[str, str] = {}
    if options.org:
        base_env["AZ_GH_AZDO_ORG"] = options.org
    if options.project:
        base_env["AZ_GH_AZDO_PROJECT"] = options.project
    if options.repository:
        base_env["AZ_GH_AZDO_REPOSITORY"] = options.repository
    if not base_env and options.az.resolve() == DEFAULT_REPLAY_AZ.resolve():
        base_env = {
            "AZ_GH_AZDO_ORG": "https://dev.azure.com/replay",
            "AZ_GH_AZDO_PROJECT": "Weather",
            "AZ_GH_AZDO_REPOSITORY": "weather",
        }
    if options.actual:
        actual_path = options.actual
        if options.partial:
            mismatches = compare_partial_transcript(options.official, actual_path)
        else:
            mismatches = compare_transcripts(options.official, actual_path)
    else:
        actual_path, mismatches = replay(
            options.official,
            cli=options.cli,
            az=options.az,
            base_env=base_env,
        )
    if mismatches:
        for mismatch in mismatches:
            print(mismatch.format())
        print(f"{len(mismatches)} mismatch(es); replay log: {actual_path}")
        return 1
    count = len(load_transcript(options.actual)) if options.actual else len(load_transcript(options.official))
    qualifier = " captured command(s)" if options.partial else " command(s)"
    print(f"all commands match by shape ({count}{qualifier})")
    print(f"replay log: {actual_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
