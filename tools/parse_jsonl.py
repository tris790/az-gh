#!/usr/bin/env python3
"""Inspect Codex/CLI JSONL recordings without loading raw output by hand.

Examples:
    python tools/parse_jsonl.py official_commands.jsonl --summary
    python tools/parse_jsonl.py az-gh-commands.jsonl --json-output 7
    python tools/parse_jsonl.py official_commands.jsonl --shapes

The module functions are also used by contract tests. Records are read one
line at a time and output payloads are decoded only when requested, which is
important for recordings containing large diffs.
"""

from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
import json
from pathlib import Path
from typing import Any, Iterable, Iterator


def iter_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield valid JSONL events from *path* in recording order."""
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"{path}:{line_number}: event must be an object")
            event["_line"] = line_number
            yield event


def load_transcript(path: str | Path) -> list[dict[str, Any]]:
    """Group a recording's events by command id while preserving order."""
    commands: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for event in iter_events(path):
        command_id = event.get("id")
        if not isinstance(command_id, str):
            continue
        command = commands.setdefault(command_id, {"id": command_id, "events": []})
        command["events"].append(event)
        if event.get("event") == "start":
            command["argv"] = event.get("argv", [])
            command["cwd"] = event.get("cwd")
    return list(commands.values())


def output_events(command: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return (event for event in command.get("events", []) if event.get("event") == "output")


def decode_output(event: dict[str, Any], parse_json: bool = True) -> Any:
    """Decode one output event, optionally parsing its UTF-8 payload as JSON."""
    try:
        payload = base64.b64decode(event["data_b64"], validate=True)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"output event at line {event.get('_line')} has invalid base64") from exc
    text = payload.decode("utf-8", "replace")
    if not parse_json:
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def json_outputs(path: str | Path) -> list[tuple[dict[str, Any], Any]]:
    """Return parsed JSON output payloads and their command metadata."""
    result: list[tuple[dict[str, Any], Any]] = []
    for command in load_transcript(path):
        for event in output_events(command):
            value = decode_output(event)
            if isinstance(value, (dict, list)):
                result.append((command, value))
    return result


def shape(value: Any) -> Any:
    """Return a value-independent JSON shape suitable for contract tests."""
    if isinstance(value, dict):
        return {key: shape(value[key]) for key in value}
    if isinstance(value, list):
        if not value:
            return {"[]": None}
        item_shapes: list[Any] = []
        for item in value:
            item_shape = shape(item)
            if item_shape not in item_shapes:
                item_shapes.append(item_shape)
        return {"[]": item_shapes[0] if len(item_shapes) == 1 else item_shapes}
    if value is None:
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _command_label(command: dict[str, Any]) -> str:
    argv = command.get("argv") or []
    return " ".join(str(value) for value in argv[:2]) or command["id"][:8]


def print_summary(path: str | Path) -> None:
    commands = load_transcript(path)
    print(f"{path}: {len(commands)} command(s)")
    for command in commands:
        outputs = list(output_events(command))
        json_count = sum(isinstance(decode_output(event), (dict, list)) for event in outputs)
        result = next((event for event in command["events"] if event.get("event") == "result"), {})
        print(
            f"  line {command['events'][0].get('_line')}: {_command_label(command)} "
            f"outputs={len(outputs)} json={json_count} exit={result.get('exit_code')}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--summary", action="store_true", help="summarize commands and output types")
    parser.add_argument("--shapes", action="store_true", help="print value-independent shapes for JSON outputs")
    parser.add_argument("--json-output", type=int, metavar="N", help="print the Nth JSON output, one-based")
    options = parser.parse_args(argv)

    if options.summary or not (options.shapes or options.json_output):
        print_summary(options.path)
    if options.shapes:
        for index, (command, value) in enumerate(json_outputs(options.path), 1):
            print(f"\nJSON output {index} ({_command_label(command)}):")
            print(json.dumps(shape(value), indent=2, ensure_ascii=False))
    if options.json_output is not None:
        outputs = json_outputs(options.path)
        if options.json_output < 1 or options.json_output > len(outputs):
            parser.error(f"JSON output must be between 1 and {len(outputs)}")
        print(json.dumps(outputs[options.json_output - 1][1], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
