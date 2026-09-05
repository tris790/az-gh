#!/usr/bin/env python3
"""Deterministic Azure CLI fixture used by the official-command replay test."""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _argument_after(arguments: list[str], option: str) -> str | None:
    try:
        return arguments[arguments.index(option) + 1]
    except (ValueError, IndexError):
        return None


def _pull_request(number: int, include_repository: bool) -> dict[str, Any]:
    value: dict[str, Any] = {
        "pullRequestId": number,
        "status": "active",
        "title": "Replay pull request",
        "sourceRefName": "refs/heads/feature/replay",
        "targetRefName": "refs/heads/main",
        "description": "Replay body",
        "creationDate": "2026-01-01T00:00:00Z",
        "createdBy": {
            "displayName": "Replay User",
            "uniqueName": "replay@example.com",
            "imageUrl": "https://example.test/replay.png",
        },
        "lastMergeSourceCommit": {"commitId": f"source-{number}"},
        "lastMergeTargetCommit": {"commitId": f"target-{number}"},
        "_links": {
            "web": {
                "href": f"https://dev.azure.com/replay/Weather/_git/weather/pullrequest/{number}"
            }
        },
        "additions": 12,
        "deletions": 3,
        "reviewers": [],
    }
    value["repository"] = {
        "id": "replay-repository",
        "name": "weather",
        "project": {"name": "Weather"},
        "webUrl": "https://dev.azure.com/replay/Weather/_git/weather",
    }
    value["sourceRepository"] = value["repository"] if include_repository else None
    return value


def main() -> int:
    arguments = sys.argv[1:]
    index = int(os.environ.get("AZ_GH_REPLAY_COMMAND_INDEX", "0"))
    resource = _argument_after(arguments, "--resource")
    if arguments[:2] == ["account", "show"]:
        result: Any = {"user": {"name": "replay@example.com", "id": "replay-user"}}
    elif arguments[:4] == ["devops", "user", "show", "--user"]:
        result = {
            "id": "replay-user",
            "user": {
                "displayName": "Replay User",
                "url": "https://dev.azure.com/replay/_apis/Graph/Users/replay-user",
            },
        }
    elif arguments[:3] == ["repos", "pr", "list"]:
        if index in {11, 12, 13}:
            result = []
        elif index == 14:
            # The official recording returned eleven author-matched PRs. The
            # replay only needs their structural cardinality and pagination
            # metadata, so deterministic synthetic records are sufficient.
            result = [_pull_request(number, True) for number in range(1, 12)]
        else:
            result = [_pull_request(3, True)]
    elif resource == "pullRequests":
        number_text = next(
            (argument.split("=", 1)[1] for argument in arguments if argument.startswith("pullRequestId=")),
            "3",
        )
        number = int(number_text)
        if index in {6, 7, 8, 9, 10}:
            print("ERROR: TF401180: The requested pull request was not found.", file=sys.stderr)
            return 1
        result = _pull_request(number, include_repository=True)
    elif resource == "commits":
        number_text = next(
            (argument.split("=", 1)[1] for argument in arguments if argument.startswith("pullRequestId=")),
            "3",
        )
        number = int(number_text)
        result = {
            "value": [{
                "commitId": f"source-{number}",
                "comment": "Replay commit",
                "author": {
                    "name": "Replay User",
                    "email": "replay@example.com",
                    "date": "2026-01-01T00:00:00Z",
                },
            }]
        }
    elif resource == "pullRequestThreads":
        result = {"value": []}
    elif resource == "commitDiffs":
        result = {"changes": [{"item": {"path": "/README.md"}, "changeType": "edit"}]}
    elif resource == "items":
        is_source = any("source-" in argument for argument in arguments)
        result = {"content": "old line\n" if not is_source else "new line\n", "contentType": "rawText"}
    else:
        result = {}
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
