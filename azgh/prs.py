from __future__ import annotations

import base64
import difflib
import json
import os
from typing import Any, Callable
from urllib.parse import quote, urlparse

from .azcli import AzCli
from .errors import CliError
from .identity import account, configured_username
from .repository import RepoContext


def strip_ref(value: Any) -> Any:
    if isinstance(value, str):
        return value.removeprefix("refs/heads/")
    return value


def state(value: Any) -> str:
    normalized = str(value or "").lower()
    return {"active": "OPEN", "completed": "MERGED", "abandoned": "CLOSED"}.get(normalized, normalized.upper())


def pr_url(data: dict[str, Any]) -> str | None:
    api_url = data.get("url")
    links = data.get("_links")
    if isinstance(links, dict) and isinstance(links.get("web"), dict):
        href = links["web"].get("href")
        if href and "/_apis/" not in href:
            return href
        api_url = href or api_url
    repository = data.get("repository") or {}
    base = repository.get("webUrl") if isinstance(repository, dict) else None
    number = data.get("pullRequestId") or data.get("number")
    if base and number:
        return f"{base}/pullrequest/{number}"
    project = repository.get("project") if isinstance(repository, dict) else None
    project_name = project.get("name") if isinstance(project, dict) else None
    repository_name = repository.get("name") if isinstance(repository, dict) else None
    if api_url and number and project_name and repository_name:
        parsed = urlparse(api_url)
        host = parsed.hostname or ""
        if host.endswith(".visualstudio.com"):
            organization = f"{parsed.scheme}://{host}"
        else:
            path = [part for part in parsed.path.split("/") if part]
            organization = f"{parsed.scheme}://{host}/{path[0]}" if host == "dev.azure.com" and path else None
        if organization:
            return (
                f"{organization}/{quote(str(project_name), safe='')}/_git/"
                f"{quote(str(repository_name), safe='')}/pullrequest/{number}"
            )
    return api_url


def normalize_pr(data: dict[str, Any]) -> dict[str, Any]:
    creator = data.get("createdBy") or data.get("author") or {}
    creator_name = creator.get("displayName") or creator.get("uniqueName") if isinstance(creator, dict) else creator
    source = data.get("sourceRefName") or data.get("headRefName")
    target = data.get("targetRefName") or data.get("baseRefName")
    return {
        "number": data.get("pullRequestId", data.get("number")),
        "url": pr_url(data),
        "state": state(data.get("status", data.get("state"))),
        "headRefName": strip_ref(source),
        "baseRefName": strip_ref(target),
        "title": data.get("title"),
        "body": data.get("description", data.get("body")),
        "author": {"login": creator_name} if creator_name else None,
        "isDraft": data.get("isDraft", False),
        "createdAt": data.get("creationDate", data.get("createdAt")),
        "updatedAt": data.get("updatedDate", data.get("updatedAt", data.get("closedDate", data.get("creationDate")))),
        "closedAt": data.get("closedDate"),
        "mergedAt": data.get("closedDate") if str(data.get("status", "")).lower() == "completed" else None,
        "repository": data.get("repository"),
        "raw": data,
    }


def select_fields(item: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for field in sorted(fields):
        if field in item:
            output[field] = item[field]
        elif field == "headRepository" or field == "headRepositoryOwner":
            output[field] = None
        elif field in item.get("raw", {}):
            output[field] = item["raw"][field]
        else:
            output[field] = None
    return output


def apply_jq(value: Any, expression: str | None) -> Any:
    if not expression:
        return value
    expression = expression.strip()
    if expression.startswith(".[] | "):
        expression = expression[6:].strip()
        return [apply_jq(item, expression) for item in value]
    if expression.startswith("[].") and isinstance(value, list):
        return [apply_jq(item, "." + expression[3:]) for item in value]
    if expression.startswith(".[].") and isinstance(value, list):
        return [apply_jq(item, "." + expression[4:]) for item in value]
    if expression.startswith(".") and isinstance(value, dict):
        current: Any = value
        for part in expression[1:].split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current
    raise CliError(f"az-gh: unsupported --jq expression: {expression}")


def branch_for_azdo(value: str) -> str:
    return value if value.startswith("refs/") else f"refs/heads/{value}"


def fetch_prs(
    az: AzCli,
    ctx: RepoContext,
    state_filter: str = "open",
    limit: int | None = None,
    author: str | None = None,
    reviewer: str | None = None,
) -> list[dict[str, Any]]:
    args = ["repos", "pr", "list"] + ctx.az_args()
    if author and author != "@me":
        args += ["--creator", author]
    elif author == "@me":
        user_data = account(az)
        user = configured_username(user_data)
        if user:
            args += ["--creator", user]
    if reviewer:
        args += ["--reviewer", reviewer]
    if state_filter == "open":
        args += ["--status", "active"]
    elif state_filter == "closed":
        args += ["--status", "all"]
    elif state_filter in {"merged", "completed"}:
        args += ["--status", "completed"]
    else:
        args += ["--status", "all"]
    if limit:
        args += ["--top", str(limit)]

    raw = az.json(args)
    records = raw if isinstance(raw, list) else []
    normalized = [normalize_pr(item) for item in records if isinstance(item, dict)]
    if state_filter == "closed":
        normalized = [item for item in normalized if item["state"] in {"CLOSED", "MERGED"}]
    elif state_filter == "merged":
        normalized = [item for item in normalized if item["state"] == "MERGED"]
    return normalized


def list_prs(az: AzCli, ctx: RepoContext, options: Any, emit: Callable[[str, bytes], None]) -> int:
    state_filter = options.state or "open"
    normalized = fetch_prs(
        az,
        ctx,
        state_filter=state_filter,
        limit=options.limit,
        author=options.author,
    )

    if options.json_fields:
        fields = [field.strip() for field in options.json_fields.split(",") if field.strip()]
        result: Any = [select_fields(item, fields) for item in normalized]
        result = apply_jq(result, options.jq)
        if options.jq and isinstance(result, list) and all(
            isinstance(item, (str, int, float, bool)) or item is None for item in result
        ):
            output = "\n".join(
                "null" if item is None else str(item).lower() if isinstance(item, bool) else str(item)
                for item in result
            )
            if output:
                output += "\n"
        elif options.jq and isinstance(result, (str, int, float, bool)):
            output = str(result).lower() if isinstance(result, bool) else str(result)
            output += "\n"
        else:
            output = json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
        emit("stdout", output.encode("utf-8"))
    else:
        lines = [
            "\t".join(
                [
                    str(item.get("number") or ""),
                    item.get("title") or "",
                    item.get("headRefName") or "",
                    "DRAFT" if item.get("isDraft") else item["state"],
                    item.get("createdAt") or "",
                ]
            )
            for item in normalized
        ]
        emit("stdout", ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))
    return 0


def fetch_pr(az: AzCli, ctx: RepoContext, number: str) -> dict[str, Any]:
    args = ["repos", "pr", "show", "--id", number]
    if ctx.organization:
        args += ["--organization", ctx.organization]
    data = az.json(args)
    return data if isinstance(data, dict) else {}


def show_pr(az: AzCli, ctx: RepoContext, number: str, options: Any, emit: Callable[[str, bytes], None]) -> int:
    data = fetch_pr(az, ctx, number)
    normalized = normalize_pr(data)
    if options.json_fields:
        fields = [field.strip() for field in options.json_fields.split(",") if field.strip()]
        output: Any = select_fields(normalized, fields)
        output = apply_jq(output, getattr(options, "jq", None))
    else:
        author = normalized.get("author") or {}
        author_name = author.get("login") if isinstance(author, dict) else author
        body = normalized.get("body") or ""
        lines = [
            f"title:\t{normalized.get('title') or ''}",
            f"state:\t{normalized.get('state') or ''}",
            f"author:\t{author_name or ''}",
            "labels:\t",
            "assignees:\t",
            "reviewers:\t",
            "projects:\t",
            "milestone:\t",
            f"number:\t{normalized.get('number') or ''}",
            f"url:\t{normalized.get('url') or ''}",
            f"additions:\t{normalized['raw'].get('additions', '')}",
            f"deletions:\t{normalized['raw'].get('deletions', '')}",
            "auto-merge:\tdisabled",
            "--",
        ]
        if body:
            lines.append(str(body).rstrip("\n"))
        emit("stdout", ("\n".join(lines) + "\n").encode("utf-8"))
        return 0
    emit("stdout", (json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return 0


def _content(value: Any) -> str:
    if isinstance(value, dict):
        content = value.get("content", "")
        if value.get("contentType") == "base64Encoded":
            try:
                return base64.b64decode(content).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return ""
        return str(content)
    return str(value or "")


def _item_content(az: AzCli, ctx: RepoContext, repository_id: str, path: str, commit: str) -> str:
    args = [
        "devops", "invoke", "--area", "git", "--resource", "items",
        "--route-parameters", f"project={ctx.project or ''}", f"repositoryId={repository_id}",
        "--query-parameters",
        f"path={quote(path)}&versionDescriptor.version={commit}&versionDescriptor.versionType=commit&includeContent=true",
        "--api-version", "7.1-preview.1",
    ]
    if ctx.organization:
        args += ["--organization", ctx.organization]
    data = az.json(args)
    return _content(data)


def diff_pr(az: AzCli, ctx: RepoContext, number: str, emit: Callable[[str, bytes], None]) -> int:
    if not ctx.project:
        raise CliError("az-gh: Azure project is required for pr diff; set AZDO_PROJECT or use --repo PROJECT/REPOSITORY")
    show_args = ["repos", "pr", "show", "--id", number]
    if ctx.organization:
        show_args += ["--organization", ctx.organization]
    details = az.json(show_args)
    if not isinstance(details, dict):
        raise CliError("az-gh: Azure CLI returned no pull request details")
    repository = details.get("repository") or {}
    repository_id = str(repository.get("id") or ctx.repository or "")
    source_commit = (details.get("lastMergeSourceCommit") or {}).get("commitId")
    target_commit = (details.get("lastMergeTargetCommit") or {}).get("commitId")
    if not repository_id or not source_commit or not target_commit:
        raise CliError("az-gh: pull request is missing repository or merge commit information")

    args = [
        "devops", "invoke", "--area", "git", "--resource", "diffs",
        "--route-parameters", f"project={ctx.project}", f"repositoryId={repository_id}",
        "--query-parameters", f"baseVersion={target_commit}&targetVersion={source_commit}",
        "--api-version", "7.1-preview.1",
    ]
    if ctx.organization:
        args += ["--organization", ctx.organization]
    diff_data = az.json(args)
    changes = diff_data.get("changes", []) if isinstance(diff_data, dict) else []
    chunks: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        item = change.get("item") or {}
        path = item.get("path") or change.get("path")
        if not path or item.get("isFolder"):
            continue
        original_path = change.get("originalPath") or path
        change_type = str(change.get("changeType", "edit")).lower()
        old_text = _content(change.get("originalContent")) if "originalContent" in change else ""
        new_text = _content(change.get("newContent")) if "newContent" in change else ""
        if "originalContent" not in change and change_type not in {"add", "new"}:
            old_text = _item_content(az, ctx, repository_id, original_path, target_commit)
        if "newContent" not in change and change_type not in {"delete", "remove"}:
            new_text = _item_content(az, ctx, repository_id, path, source_commit)
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        header = f"diff --git a{original_path} b{path}\n"
        patch = "".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"a{original_path}", tofile=f"b{path}", lineterm="\n"))
        if patch and not patch.endswith("\n"):
            patch += "\n"
        chunks.append(header + patch)
    emit("stdout", "".join(chunks).encode("utf-8"))
    return 0
