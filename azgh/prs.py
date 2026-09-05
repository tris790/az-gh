from __future__ import annotations

import base64
import difflib
import json
import os
import re
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

from markdown_it import MarkdownIt

from .azcli import AzCli
from .errors import CliError
from .identity import account, configured_username
from .repository import RepoContext, parse_remote


def strip_ref(value: Any) -> Any:
    if isinstance(value, str):
        return value.removeprefix("refs/heads/")
    return value


def state(value: Any) -> str:
    normalized = str(value or "").lower()
    return {"active": "OPEN", "completed": "MERGED", "abandoned": "CLOSED"}.get(normalized, normalized.upper())


def first_value(data: dict[str, Any], *keys: str) -> Any:
    """Return the first non-null value, preserving meaningful falsey values."""
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def identity_login(identity: Any) -> str | None:
    """Return the stable Azure identity value used as a GitHub ``login``."""
    if isinstance(identity, dict):
        value = (
            identity.get("login")
            or identity.get("uniqueName")
            or identity.get("email")
            or identity.get("mail")
            or identity.get("displayName")
            or identity.get("id")
        )
        return str(value) if value is not None else None
    return str(identity) if identity else None


_BARE_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _replace_plain_urls(line: str) -> str:
    """Turn bare URLs into Markdown autolinks without touching Markdown syntax."""
    def replace(match: re.Match[str]) -> str:
        start = match.start()
        prefix = line[:start]
        if (
            (line.rfind("<", 0, start) > line.rfind(">", 0, start))
            or prefix.rstrip().endswith("](")
            or any(span.start() <= start < span.end() for span in re.finditer(r"`+[^`]*`+", line))
        ):
            return match.group(0)

        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,;:!?)]}":
            trailing = url[-1] + trailing
            url = url[:-1]
        if not url:
            return match.group(0)
        return f"<{url}>{trailing}"

    return _BARE_URL.sub(replace, line)


def _linkify_plain_urls(text: str) -> str:
    lines: list[str] = []
    fenced = False
    for line in text.split("\n"):
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            lines.append(line)
            fenced = not fenced
        elif fenced:
            lines.append(line)
        else:
            lines.append(_replace_plain_urls(line))
    return "\n".join(lines)


def _link_open(tokens: list[Any], index: int, options: Any, env: Any, renderer: MarkdownIt) -> str:
    token = tokens[index]
    token.attrSet("rel", "nofollow")
    return renderer.renderer.renderToken(tokens, index, options, env)


def _block_open(tokens: list[Any], index: int, options: Any, env: Any, renderer: MarkdownIt) -> str:
    token = tokens[index]
    token.attrSet("dir", "auto")
    return renderer.renderer.renderToken(tokens, index, options, env)


def _line_break(tokens: list[Any], index: int, options: Any, env: Any) -> str:
    return "<br>\n"


def _markdown_renderer() -> MarkdownIt:
    renderer = MarkdownIt(
        "default",
        {
            "breaks": True,
            "html": False,
            "linkify": False,
        },
    )

    def link_open(tokens: list[Any], index: int, options: Any, env: Any) -> str:
        return _link_open(tokens, index, options, env, renderer)

    def block_open(tokens: list[Any], index: int, options: Any, env: Any) -> str:
        return _block_open(tokens, index, options, env, renderer)

    for rule in ("paragraph_open", "heading_open", "bullet_list_open", "ordered_list_open"):
        renderer.renderer.rules[rule] = block_open
    renderer.renderer.rules["link_open"] = link_open
    renderer.renderer.rules["softbreak"] = _line_break
    renderer.renderer.rules["hardbreak"] = _line_break
    return renderer


def markdown_to_html(value: Any) -> str:
    """Render Azure Markdown in the HTML shape returned by GitHub GraphQL."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return ""
    text = _linkify_plain_urls(text)
    return _markdown_renderer().render(text).removesuffix("\n")


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
        return f"{str(base).rstrip('/')}/pullrequest/{number}"
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


def repository_owner(data: dict[str, Any]) -> str | None:
    repository = data.get("repository") or {}
    if not isinstance(repository, dict):
        return None
    web_url = repository.get("webUrl") or repository.get("remoteUrl") or repository.get("url")
    if isinstance(web_url, str):
        parsed = urlparse(web_url)
        host = parsed.hostname or ""
        if host.endswith(".visualstudio.com"):
            return host.split(".", 1)[0]
        path = [part for part in parsed.path.split("/") if part]
        if host == "dev.azure.com" and path:
            return path[0]
    project = repository.get("project")
    if isinstance(project, dict) and project.get("organization"):
        return str(project["organization"])
    return None


def head_repository(data: dict[str, Any]) -> dict[str, Any] | None:
    repository = data.get("repository") or {}
    if not isinstance(repository, dict):
        return None
    name = repository.get("name")
    web_url = repository.get("webUrl") or repository.get("remoteUrl")
    owner = repository_owner(data)
    if not name and not web_url and not owner:
        return None
    result: dict[str, Any] = {
        "name": name,
        "nameWithOwner": f"{owner}/{name}" if owner and name else name,
        "owner": {"login": owner} if owner else None,
        "url": web_url,
    }
    if repository.get("id") is not None:
        result["id"] = repository["id"]
    return result


def head_repository_owner(data: dict[str, Any]) -> dict[str, Any] | None:
    owner = repository_owner(data)
    if not owner:
        return None
    repository = data.get("repository") or {}
    web_url = repository.get("webUrl") or repository.get("remoteUrl") if isinstance(repository, dict) else None
    url = None
    if isinstance(web_url, str):
        parsed = urlparse(web_url)
        host = parsed.hostname or ""
        if host.endswith(".visualstudio.com"):
            url = f"{parsed.scheme}://{host}"
        elif host == "dev.azure.com":
            url = f"{parsed.scheme}://{host}/{owner}"
    return {"login": owner, "name": owner, "url": url}


def normalize_pr(data: dict[str, Any]) -> dict[str, Any]:
    creator = data.get("createdBy") or data.get("author") or {}
    creator_name = identity_login(creator)
    source = data.get("sourceRefName") or data.get("headRefName")
    target = data.get("targetRefName") or data.get("baseRefName")
    body = first_value(data, "description", "body") or ""
    source_commit = data.get("lastMergeSourceCommit") or {}
    target_commit = data.get("lastMergeTargetCommit") or {}
    source_oid = source_commit.get("commitId") if isinstance(source_commit, dict) else None
    target_oid = target_commit.get("commitId") if isinstance(target_commit, dict) else None
    return {
        "number": data.get("pullRequestId", data.get("number")),
        "url": pr_url(data),
        "state": state(data.get("status", data.get("state"))),
        "headRefName": strip_ref(source),
        "baseRefName": strip_ref(target),
        "headRefOid": data["headRefOid"] if data.get("headRefOid") is not None else source_oid,
        "baseRefOid": data["baseRefOid"] if data.get("baseRefOid") is not None else target_oid,
        "title": data.get("title"),
        "body": body,
        "bodyHTML": markdown_to_html(body),
        "additions": data.get("additions"),
        "deletions": data.get("deletions"),
        "author": {"login": creator_name} if creator_name else None,
        "isDraft": data.get("isDraft", False),
        "createdAt": first_value(data, "creationDate", "createdAt"),
        "updatedAt": first_value(data, "updatedDate", "updatedAt", "closedDate", "creationDate"),
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
        elif field == "headRepository":
            output[field] = head_repository(item["raw"])
        elif field == "headRepositoryOwner":
            output[field] = head_repository_owner(item["raw"])
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
    head: str | None = None,
    base: str | None = None,
) -> list[dict[str, Any]]:
    args = ["repos", "pr", "list"] + ctx.az_args()
    if head:
        args += ["--source-branch", branch_for_azdo(head)]
    if base:
        args += ["--target-branch", branch_for_azdo(base)]
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
    # Keep the compatibility behavior correct even when an Azure CLI version
    # ignores one of the branch filters.  GitHub's --head/--base are exact
    # filters on the normalized branch names.
    if head:
        normalized = [item for item in normalized if item.get("headRefName") == strip_ref(head)]
    if base:
        normalized = [item for item in normalized if item.get("baseRefName") == strip_ref(base)]
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
        head=options.head,
        base=options.base,
    )

    if options.json_fields:
        fields = [field.strip() for field in options.json_fields.split(",") if field.strip()]
        if "additions" in fields or "deletions" in fields:
            for item, stats in zip(normalized, load_pr_diff_stats(az, ctx, normalized)):
                if stats is None:
                    continue
                item["additions"], item["deletions"] = stats
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


def fetch_pr(
    az: AzCli,
    ctx: RepoContext,
    number: str,
    *,
    emit_errors: bool = True,
) -> dict[str, Any]:
    if ctx.repository:
        args = [
            "devops", "invoke", "--area", "git", "--resource", "pullRequests",
            "--route-parameters",
            *(["project=" + ctx.project] if ctx.project else []),
            f"repositoryId={ctx.repository}", f"pullRequestId={number}",
            "--api-version", "7.1",
        ]
        if ctx.organization:
            args += ["--organization", ctx.organization]
    else:
        args = ["repos", "pr", "show", "--id", number]
        if ctx.organization:
            args += ["--organization", ctx.organization]
    data = az.json(args, emit_stderr=emit_errors)
    return data if isinstance(data, dict) else {}


def _comment_request_context(
    ctx: RepoContext,
    details: dict[str, Any],
) -> tuple[RepoContext, str]:
    """Resolve the project and repository identifiers required by the threads API."""
    repository = details.get("repository") or {}
    if not isinstance(repository, dict):
        repository = {}

    repository_id = repository.get("id") or ctx.repository or repository.get("name")
    project = ctx.project
    organization = ctx.organization
    repository_url = repository.get("webUrl") or repository.get("remoteUrl")
    if isinstance(repository_url, str):
        url_org, url_project, url_repository = parse_remote(repository_url)
        organization = organization or url_org
        project = project or url_project
        repository_id = repository_id or url_repository

    project_info = repository.get("project")
    if isinstance(project_info, dict):
        project = project or project_info.get("id") or project_info.get("name")

    if not repository_id:
        raise CliError(
            "az-gh: pull request is missing repository information; "
            "set AZDO_REPOSITORY or use --repo"
        )
    if not project:
        raise CliError(
            "az-gh: Azure project is required for pr comment; "
            "set AZDO_PROJECT or use --repo PROJECT/REPOSITORY"
        )
    return RepoContext(organization, str(project), str(repository_id)), str(repository_id)


def _comment_request_args(
    ctx: RepoContext,
    number: str,
    resource: str,
    method: str,
    repository_id: str,
    thread_id: Any = None,
    comment_id: Any = None,
    iteration_id: Any = None,
) -> list[str]:
    route_parameters = [
        f"project={ctx.project}", f"repositoryId={repository_id}",
        f"pullRequestId={number}",
    ]
    if thread_id is not None:
        route_parameters.append(f"threadId={thread_id}")
    if comment_id is not None:
        route_parameters.append(f"commentId={comment_id}")
    if iteration_id is not None:
        route_parameters.append(f"iterationId={iteration_id}")
    args = [
        "devops", "invoke", "--area", "git", "--resource", resource,
        "--route-parameters", *route_parameters, "--http-method", method,
        "--api-version", "7.1",
    ]
    if ctx.organization:
        args += ["--organization", ctx.organization]
    return args


def _comment_body(options: Any) -> str:
    body = getattr(options, "body", None)
    body_file = getattr(options, "body_file", None)
    if body is not None and body_file is not None:
        raise CliError("gh pr comment: specify only one of --body or --body-file")
    if body is not None:
        return body
    if body_file is not None:
        if body_file == "-":
            return sys.stdin.read()
        try:
            with open(body_file, encoding="utf-8") as source:
                return source.read()
        except OSError as exc:
            raise CliError(f"gh pr comment: could not read body file: {exc}") from exc
    if getattr(options, "editor", False):
        raise CliError("az-gh: --editor is not supported for Azure DevOps comments")
    raise CliError("gh pr comment: flags required when not running interactively")


def _comment_url(
    details: dict[str, Any],
    thread: dict[str, Any] | None,
    comment: dict[str, Any] | None = None,
) -> str | None:
    for value in (comment, thread):
        if not isinstance(value, dict):
            continue
        direct_url = value.get("url")
        if isinstance(direct_url, str) and "/_apis/" not in direct_url:
            return direct_url
        links = value.get("_links")
        if isinstance(links, dict):
            for link_name in ("web", "html"):
                link = links.get(link_name)
                if isinstance(link, dict):
                    href = link.get("href")
                    if isinstance(href, str) and "/_apis/" not in href:
                        return href

    pull_request_url = pr_url(details)
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if pull_request_url and thread_id is not None:
        return f"{pull_request_url}?discussionId={quote(str(thread_id), safe='')}"
    return pull_request_url


def _list_pr_threads(
    az: AzCli,
    ctx: RepoContext,
    number: str,
    repository_id: str,
) -> list[dict[str, Any]]:
    data = az.json(
        _comment_request_args(ctx, number, "pullRequestThreads", "GET", repository_id)
    )
    values = data.get("value") if isinstance(data, dict) else None
    return [thread for thread in values if isinstance(thread, dict)] if isinstance(values, list) else []


def _last_user_comment(
    az: AzCli,
    threads: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    user = configured_username(account(az))
    if not user:
        return None
    for thread in reversed(threads):
        comments = thread.get("comments")
        if not isinstance(comments, list):
            continue
        for comment in reversed(comments):
            if not isinstance(comment, dict):
                continue
            if str(comment.get("commentType", "text")).lower() == "system":
                continue
            author = identity_login(comment.get("author"))
            if author == user:
                return thread, comment
    return None


def _resolve_comment_number(
    az: AzCli,
    ctx: RepoContext,
    value: str,
) -> str:
    if value.isdigit():
        return value
    match = re.search(r"/(?:pull|pullrequest)/(?P<number>\d+)(?:[/?#]|$)", value)
    if match:
        return match.group("number")
    matches = fetch_prs(az, ctx, state_filter="all", head=value, limit=100)
    if len(matches) == 1 and matches[0].get("number") is not None:
        return str(matches[0]["number"])
    raise CliError(f"az-gh: could not resolve pull request {value!r}")


def comment_pr(
    az: AzCli,
    ctx: RepoContext,
    options: Any,
    emit: Callable[[str, bytes], None],
) -> int:
    number = getattr(options, "number", None)
    if not number:
        raise CliError("gh pr comment: pull request number is required")
    if getattr(options, "attach", []):
        raise CliError("az-gh: --attach is not supported for Azure DevOps comments")
    if getattr(options, "edit_last", False) and getattr(options, "delete_last", False):
        raise CliError("gh pr comment: --edit-last and --delete-last cannot be used together")
    if getattr(options, "create_if_none", False) and not getattr(options, "edit_last", False):
        raise CliError("gh pr comment: --create-if-none can only be used with --edit-last")

    number = _resolve_comment_number(az, ctx, str(number))
    details = fetch_pr(az, ctx, number)
    if not details:
        raise CliError("az-gh: Azure CLI returned no pull request details")
    comment_ctx, repository_id = _comment_request_context(ctx, details)

    if getattr(options, "web", False):
        if any(getattr(options, name, None) is not None for name in ("body", "body_file")):
            raise CliError("gh pr comment: specify only one of --body, --body-file, or --web")
        target_url = _comment_url(details, None) or ""
        if getattr(options, "edit_last", False):
            existing = _last_user_comment(az, _list_pr_threads(az, comment_ctx, number, repository_id))
            if existing is None:
                raise CliError("gh pr comment: no comments found for current user")
            target_url = _comment_url(details, *existing) or target_url
        if target_url:
            webbrowser.open(target_url)
        return 0

    if getattr(options, "delete_last", False):
        if not getattr(options, "yes", False):
            raise CliError("gh pr comment: --yes is required with --delete-last")
        existing = _last_user_comment(az, _list_pr_threads(az, comment_ctx, number, repository_id))
        if existing is None:
            raise CliError("gh pr comment: no comments found for current user")
        thread, comment = existing
        thread_id = thread.get("id")
        comment_id = comment.get("id")
        if thread_id is None or comment_id is None:
            raise CliError("az-gh: Azure comment is missing thread or comment information")
        az.json(
            _comment_request_args(
                comment_ctx, number, "pullRequestThreadComments", "DELETE", repository_id,
                thread_id, comment_id,
            )
        )
        return 0

    body = _comment_body(options)
    if getattr(options, "edit_last", False):
        existing = _last_user_comment(az, _list_pr_threads(az, comment_ctx, number, repository_id))
        if existing is None:
            if not getattr(options, "create_if_none", False):
                raise CliError("gh pr comment: no comments found for current user")
        else:
            thread, comment = existing
            thread_id = thread.get("id")
            comment_id = comment.get("id")
            if thread_id is None or comment_id is None:
                raise CliError("az-gh: Azure comment is missing thread or comment information")
            updated = az.json(
                _comment_request_args(
                    comment_ctx, number, "pullRequestThreadComments", "PATCH", repository_id,
                    thread_id, comment_id,
                ),
                payload={"content": body},
            )
            url = _comment_url(details, thread, updated if isinstance(updated, dict) else comment)
            if url:
                emit("stdout", (url + "\n").encode("utf-8"))
            return 0

    created = az.json(
        _comment_request_args(comment_ctx, number, "pullRequestThreads", "POST", repository_id),
        payload={
            "comments": [{"parentCommentId": 0, "content": body, "commentType": 1}],
            "status": 1,
        },
    )
    created_thread = created if isinstance(created, dict) else {}
    created_comments = created_thread.get("comments")
    created_comment = created_comments[0] if isinstance(created_comments, list) and created_comments else None
    url = _comment_url(details, created_thread, created_comment if isinstance(created_comment, dict) else None)
    if url:
        emit("stdout", (url + "\n").encode("utf-8"))
    return 0


def is_pull_request_comment_endpoint(endpoint: str) -> bool:
    path = urlparse(endpoint).path if "://" in endpoint else endpoint.split("?", 1)[0]
    return bool(re.search(r"(?:^|/)repos/[^/]+/[^/]+/pulls/[^/]+/comments/?$", path.strip("/")))


def _api_form_fields(raw_fields: list[str], typed_fields: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field, typed in [(field, False) for field in raw_fields] + [(field, True) for field in typed_fields]:
        if "=" not in field:
            raise CliError(f"az-gh: field must be in KEY=VALUE form: {field}")
        key, value = field.split("=", 1)
        if not key:
            raise CliError("az-gh: field name cannot be empty")
        if value.startswith("@"):
            try:
                with open(value[1:], encoding="utf-8") as source:
                    value = source.read()
            except OSError as exc:
                raise CliError(f"az-gh: could not read field file: {exc}") from exc
        if typed:
            lowered = value.lower()
            if lowered == "null":
                values[key] = None
                continue
            if lowered == "true":
                values[key] = True
                continue
            if lowered == "false":
                values[key] = False
                continue
            if re.fullmatch(r"-?\d+", value):
                values[key] = int(value)
                continue
            if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", value):
                values[key] = float(value)
                continue
        values[key] = value
    return values


def _api_comment_number(endpoint: str) -> str:
    path = urlparse(endpoint).path if "://" in endpoint else endpoint.split("?", 1)[0]
    match = re.search(r"(?:^|/)pulls/(?P<number>[^/]+)/comments/?$", path.strip("/"))
    if not match:
        raise CliError(f"az-gh: unsupported API endpoint: {endpoint}")
    number = unquote(match.group("number"))
    if not number.isdigit():
        raise CliError(f"az-gh: pull request number must be numeric: {number}")
    return number


def _pull_request_iterations(
    az: AzCli,
    ctx: RepoContext,
    number: str,
    repository_id: str,
) -> list[dict[str, Any]]:
    data = az.json(
        _comment_request_args(ctx, number, "pullRequestIterations", "GET", repository_id)
    )
    values = data.get("value") if isinstance(data, dict) else None
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _commit_id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("commitId") or value.get("objectId")
    return str(value) if value else None


def _inline_thread_context(
    az: AzCli,
    ctx: RepoContext,
    number: str,
    repository_id: str,
    path: str,
    commit_id: str | None,
) -> dict[str, Any] | None:
    """Find Azure's iteration/change IDs for a GitHub-shaped line comment."""
    iterations = _pull_request_iterations(az, ctx, number, repository_id)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for iteration in iterations:
        try:
            iteration_id = int(iteration.get("id"))
        except (TypeError, ValueError):
            continue
        candidates.append((iteration_id, iteration))
    if not candidates:
        # Legacy Azure pull requests do not expose iteration support and do
        # not require pullRequestThreadContext.
        return None

    latest_id, _ = max(candidates, key=lambda item: item[0])
    second_iteration = latest_id
    if commit_id:
        matching = [
            iteration_id
            for iteration_id, iteration in candidates
            if _commit_id(iteration.get("sourceRefCommit")) == commit_id
        ]
        if matching:
            second_iteration = max(matching)
    first_iteration = max(1, second_iteration - 1)

    changes_args = _comment_request_args(
        ctx,
        number,
        "pullRequestIterationChanges",
        "GET",
        repository_id,
        iteration_id=second_iteration,
    )
    if first_iteration > 1:
        changes_args += ["--query-parameters", f"$top=2000&$compareTo={first_iteration}"]
    else:
        changes_args += ["--query-parameters", "$top=2000"]
    changes = az.json(changes_args)
    entries = changes.get("changeEntries") if isinstance(changes, dict) else None
    wanted = path.lstrip("/")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item") or {}
            entry_path = item.get("path") if isinstance(item, dict) else None
            original_path = entry.get("originalPath")
            if not any(
                isinstance(candidate, str) and candidate.lstrip("/") == wanted
                for candidate in (entry_path, original_path)
            ):
                continue
            tracking_id = entry.get("changeTrackingId")
            if tracking_id is None:
                continue
            return {
                "changeTrackingId": tracking_id,
                "iterationContext": {
                    "firstComparingIteration": first_iteration,
                    "secondComparingIteration": second_iteration,
                },
            }
    raise CliError(f"az-gh: file is not part of pull request {number}: {path}")


def _github_review_comment_response(
    details: dict[str, Any],
    thread: dict[str, Any],
    comment: dict[str, Any],
    fields: dict[str, Any],
    commit_id: str | None,
) -> dict[str, Any]:
    repository = details.get("repository") or {}
    author = comment.get("author") or {}
    thread_id = thread.get("id")
    comment_id = comment.get("id")
    path = str(fields.get("path") or "").lstrip("/")
    side = str(fields.get("side") or "RIGHT").upper()
    line = fields.get("line")
    start_line = fields.get("start_line")
    if start_line is None:
        start_line = line
    html_url = _comment_url(details, thread, comment)
    api_url = None
    links = comment.get("_links") if isinstance(comment, dict) else None
    if isinstance(links, dict) and isinstance(links.get("self"), dict):
        api_url = links["self"].get("href")
    if not api_url:
        api_url = thread.get("url") or comment.get("url")
    login = identity_login(author)
    return {
        "url": api_url or html_url,
        "pull_request_review_id": None,
        "id": comment_id if comment_id is not None else thread_id,
        "node_id": f"AZ_GH_THREAD_{thread_id}_COMMENT_{comment_id}",
        "diff_hunk": None,
        "path": path,
        "position": None,
        "original_position": None,
        "commit_id": commit_id,
        "original_commit_id": fields.get("original_commit_id") or commit_id,
        "user": {
            "login": login,
            "id": author.get("id"),
            "node_id": author.get("id"),
            "avatar_url": author.get("imageUrl"),
            "url": author.get("url"),
            "html_url": author.get("url"),
            "type": "User",
            "site_admin": False,
        },
        "body": comment.get("content") or "",
        "created_at": comment.get("publishedDate") or thread.get("publishedDate"),
        "updated_at": comment.get("lastUpdatedDate") or thread.get("lastUpdatedDate"),
        "html_url": html_url,
        "pull_request_url": pr_url(details),
        "author_association": "NONE",
        "line": line,
        "side": side,
        "start_line": start_line,
        "start_side": str(fields.get("start_side") or side).upper(),
        "original_line": fields.get("original_line") or line,
        "original_start_line": fields.get("original_start_line") or start_line,
        "original_start_side": str(fields.get("original_start_side") or fields.get("start_side") or side).upper(),
        "in_reply_to_id": None,
    }


def api_pull_request_comment(
    az: AzCli,
    ctx: RepoContext,
    endpoint: str,
    method: str | None,
    raw_fields: list[str],
    typed_fields: list[str],
    jq: str | None,
    emit: Callable[[str, bytes], None],
) -> int:
    if (method or "GET").upper() != "POST":
        raise CliError("az-gh: pull request code comments require --method POST")
    fields = _api_form_fields(raw_fields, typed_fields)
    body = fields.get("body")
    path = fields.get("path")
    line = fields.get("line")
    if body is None:
        raise CliError("az-gh: pull request code comments require a body field")
    if not isinstance(path, str) or not path:
        raise CliError("az-gh: pull request code comments require a path field")
    try:
        line = int(line)
    except (TypeError, ValueError) as exc:
        raise CliError("az-gh: pull request code comments require an integer line field") from exc
    if line < 1:
        raise CliError("az-gh: line must be at least 1")

    number = _api_comment_number(endpoint)
    details = fetch_pr(az, ctx, number)
    if not details:
        raise CliError("az-gh: Azure CLI returned no pull request details")
    comment_ctx, repository_id = _comment_request_context(ctx, details)
    commit_id = _commit_id(fields.get("commit_id")) or _commit_id(details.get("lastMergeSourceCommit"))
    side = str(fields.get("side") or "RIGHT").upper()
    if side not in {"LEFT", "RIGHT"}:
        raise CliError("az-gh: side must be LEFT or RIGHT")
    start_line = fields.get("start_line")
    try:
        start_line = int(start_line) if start_line is not None else line
    except (TypeError, ValueError) as exc:
        raise CliError("az-gh: start_line must be an integer") from exc
    if start_line < 1:
        raise CliError("az-gh: start_line must be at least 1")
    start_side = str(fields.get("start_side") or side).upper()
    if start_side not in {"LEFT", "RIGHT"}:
        raise CliError("az-gh: start_side must be LEFT or RIGHT")

    start = {"line": start_line, "offset": 1}
    end = {"line": line, "offset": 1}
    thread_context: dict[str, Any] = {"filePath": "/" + path.lstrip("/")}
    if start_side == "LEFT":
        thread_context["leftFileStart"] = start
    else:
        thread_context["rightFileStart"] = start
    if side == "LEFT":
        thread_context["leftFileEnd"] = end
    else:
        thread_context["rightFileEnd"] = end

    payload: dict[str, Any] = {
        "comments": [{"parentCommentId": 0, "content": str(body), "commentType": 1}],
        "status": 1,
        "threadContext": thread_context,
    }
    iteration_context = _inline_thread_context(
        az, comment_ctx, number, repository_id, path, commit_id
    )
    if iteration_context is not None:
        payload["pullRequestThreadContext"] = iteration_context

    created = az.json(
        _comment_request_args(comment_ctx, number, "pullRequestThreads", "POST", repository_id),
        payload=payload,
    )
    thread = created if isinstance(created, dict) else {}
    comments = thread.get("comments")
    comment = comments[0] if isinstance(comments, list) and comments else {}
    if not isinstance(comment, dict):
        comment = {}
    result = _github_review_comment_response(details, thread, comment, fields, commit_id)
    output: Any = apply_jq(result, jq)
    if jq and isinstance(output, (str, int, float, bool)):
        rendered = str(output).lower() if isinstance(output, bool) else str(output)
    else:
        rendered = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    emit("stdout", (rendered + "\n").encode("utf-8"))
    return 0


def show_pr(az: AzCli, ctx: RepoContext, number: str, options: Any, emit: Callable[[str, bytes], None]) -> int:
    data = fetch_pr(az, ctx, number)
    normalized = normalize_pr(data)
    if options.json_fields:
        fields = [field.strip() for field in options.json_fields.split(",") if field.strip()]
        if "additions" in fields or "deletions" in fields:
            stats = load_pr_diff_stats(az, ctx, [normalized])[0]
            if stats is not None:
                normalized["additions"], normalized["deletions"] = stats
        output: Any = select_fields(normalized, fields)
        output = apply_jq(output, getattr(options, "jq", None))
    else:
        stats = load_pr_diff_stats(az, ctx, [normalized])[0]
        if stats is not None:
            normalized["additions"], normalized["deletions"] = stats
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
            f"additions:\t{normalized.get('additions') if normalized.get('additions') is not None else ''}",
            f"deletions:\t{normalized.get('deletions') if normalized.get('deletions') is not None else ''}",
            "auto-merge:\tdisabled",
            "--",
        ]
        if body:
            lines.append(str(body).rstrip("\n"))
        emit("stdout", ("\n".join(lines) + "\n").encode("utf-8"))
        return 0
    if options.jq and isinstance(output, (str, int, float, bool)):
        rendered = str(output).lower() if isinstance(output, bool) else str(output)
    else:
        rendered = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    emit("stdout", (rendered + "\n").encode("utf-8"))
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


def _item_content(
    az: AzCli,
    ctx: RepoContext,
    repository_id: str,
    path: str,
    commit: str,
    *,
    emit_errors: bool = True,
) -> str:
    args = [
        "devops", "invoke", "--area", "git", "--resource", "items",
        "--route-parameters", f"project={ctx.project or ''}", f"repositoryId={repository_id}",
        "--query-parameters",
        f"path={quote(path)}&versionDescriptor.version={commit}&versionDescriptor.versionType=commit&includeContent=true",
        "--api-version", "7.1",
    ]
    if ctx.organization:
        args += ["--organization", ctx.organization]
    data = az.json(args, emit_stderr=emit_errors)
    return _content(data)


def _diff_request_context(
    ctx: RepoContext,
    details: dict[str, Any],
) -> tuple[RepoContext, str, str, str] | None:
    repository = details.get("repository") or {}
    if not isinstance(repository, dict):
        repository = {}
    repository_id = str(repository.get("id") or ctx.repository or repository.get("name") or "")
    source_commit = (details.get("lastMergeSourceCommit") or {}).get("commitId")
    target_commit = (details.get("lastMergeTargetCommit") or {}).get("commitId")
    repository_url = repository.get("webUrl") or repository.get("remoteUrl")
    repository_org = repository_project = None
    if isinstance(repository_url, str):
        repository_org, repository_project, _ = parse_remote(repository_url)
    project_info = repository.get("project")
    if isinstance(project_info, dict):
        repository_project = repository_project or project_info.get("name") or project_info.get("id")
    project = ctx.project or repository_project
    if not project or not repository_id or not source_commit or not target_commit:
        return None
    diff_ctx = RepoContext(ctx.organization or repository_org, str(project), repository_id)
    return diff_ctx, repository_id, str(source_commit), str(target_commit)


def _diff_request_args(
    ctx: RepoContext,
    repository_id: str,
    source_commit: str,
    target_commit: str,
) -> list[str]:
    args = [
        "devops", "invoke", "--area", "git", "--resource", "commitDiffs",
        "--route-parameters", f"project={ctx.project}", f"repositoryId={repository_id}",
        "--query-parameters",
        f"baseVersion={target_commit}&baseVersionType=commit&targetVersion={source_commit}&targetVersionType=commit",
        "--api-version", "7.1",
    ]
    if ctx.organization:
        args += ["--organization", ctx.organization]
    return args


def _file_diff_request_args(ctx: RepoContext, repository_id: str) -> list[str]:
    args = [
        "devops", "invoke", "--area", "git", "--resource", "fileDiffs",
        "--route-parameters", f"project={ctx.project}", f"repositoryId={repository_id}",
        "--http-method", "POST", "--api-version", "7.1",
    ]
    if ctx.organization:
        args += ["--organization", ctx.organization]
    return args


def _line_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _count_line_diff_blocks(file_diffs: Any) -> tuple[int, int]:
    additions = deletions = 0
    if not isinstance(file_diffs, list):
        return additions, deletions
    for file_diff in file_diffs:
        if not isinstance(file_diff, dict):
            continue
        blocks = file_diff.get("lineDiffBlocks") or []
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            change_type = block.get("changeType")
            if isinstance(change_type, bool):
                change_type = int(change_type)
            if isinstance(change_type, (int, float)):
                change_type = int(change_type)
                if change_type == 0:  # LineDiffBlockChangeType.None
                    continue
                if change_type == 1:  # Add
                    additions += _line_count(block.get("modifiedLinesCount"))
                elif change_type == 2:  # Delete
                    deletions += _line_count(block.get("originalLinesCount"))
                else:  # Edit (3), or a future two-sided change type.
                    additions += _line_count(block.get("modifiedLinesCount"))
                    deletions += _line_count(block.get("originalLinesCount"))
                continue

            normalized_type = str(change_type or "").lower()
            if normalized_type in {"", "none", "context", "unchanged"}:
                continue
            if normalized_type in {"add", "added", "insert", "inserted"}:
                additions += _line_count(block.get("modifiedLinesCount"))
            elif normalized_type in {"delete", "deleted", "remove", "removed"}:
                deletions += _line_count(block.get("originalLinesCount"))
            else:
                additions += _line_count(block.get("modifiedLinesCount"))
                deletions += _line_count(block.get("originalLinesCount"))
    return additions, deletions


def pr_diff_stats(
    az: AzCli,
    ctx: RepoContext,
    details: dict[str, Any],
) -> tuple[int, int] | None:
    """Return GitHub-style added/deleted line counts for an Azure PR."""
    raw_additions = details.get("additions")
    raw_deletions = details.get("deletions")
    if raw_additions is not None and raw_deletions is not None:
        try:
            return int(raw_additions), int(raw_deletions)
        except (TypeError, ValueError):
            pass

    request_context = _diff_request_context(ctx, details)
    if request_context is None:
        return None
    diff_ctx, repository_id, source_commit, target_commit = request_context
    try:
        diff_data = az.json(
            _diff_request_args(diff_ctx, repository_id, source_commit, target_commit),
            emit_stderr=False,
        )
    except CliError:
        return None
    changes = diff_data.get("changes", []) if isinstance(diff_data, dict) else []
    if not isinstance(changes, list):
        return None

    file_diff_params: list[dict[str, str]] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        raw_item = change.get("item") or {}
        item = raw_item if isinstance(raw_item, dict) else {}
        path = item.get("path") if item else raw_item if isinstance(raw_item, str) else None
        if not path or item.get("isFolder"):
            continue
        change_type = str(change.get("changeType", "edit")).lower()
        original_path = change.get("originalPath")
        if change_type in {"add", "new"}:
            # An added file has no path in the base commit. Supplying an
            # originalPath makes the Azure endpoint reject the whole batch.
            file_diff_params.append({"path": str(path)})
        elif change_type in {"delete", "remove"}:
            file_diff_params.append({"originalPath": str(original_path or path)})
        else:
            # For an edit, originalPath is required even when the path did
            # not change; without it Azure interprets the file as an add.
            file_diff_params.append({
                "path": str(path),
                "originalPath": str(original_path or path),
            })
    if not file_diff_params:
        return 0, 0

    payload = {
        "baseVersionCommit": target_commit,
        "targetVersionCommit": source_commit,
        "fileDiffParams": file_diff_params,
    }
    try:
        file_diff_data = az.json(
            _file_diff_request_args(diff_ctx, repository_id),
            emit_stderr=False,
            payload=payload,
        )
    except CliError:
        # Some older Azure DevOps installations do not expose fileDiffs to
        # ``az devops invoke``.  Stats are optional metadata; never make the
        # pull-request list fail because the optional endpoint is unavailable.
        return None
    if not isinstance(file_diff_data, dict):
        return None
    # ``az devops invoke`` returns the REST collection in ``value``.  Some
    # wrappers expose the same collection as ``fileDiffs``, so accept both.
    file_diffs = file_diff_data.get("fileDiffs")
    if file_diffs is None:
        file_diffs = file_diff_data.get("value")
    return _count_line_diff_blocks(file_diffs)


def load_pr_diff_stats(
    az: AzCli,
    ctx: RepoContext,
    items: list[dict[str, Any]],
) -> list[tuple[int, int] | None]:
    """Load diff stats for several normalized PRs while keeping list order."""
    if not items:
        return []

    def load(item: dict[str, Any]) -> tuple[int, int] | None:
        raw = item.get("raw")
        details = raw if isinstance(raw, dict) else item
        try:
            return pr_diff_stats(az, ctx, details)
        except Exception:
            # Diff statistics are optional enrichment for list/search output;
            # malformed metadata or an unexpected Azure response must not
            # prevent the pull-request records themselves from loading.
            return None

    with ThreadPoolExecutor(max_workers=min(4, len(items))) as executor:
        futures = [executor.submit(load, item) for item in items]
        return [future.result() for future in futures]


def diff_pr(az: AzCli, ctx: RepoContext, number: str, emit: Callable[[str, bytes], None]) -> int:
    details = fetch_pr(az, ctx, number)
    if not isinstance(details, dict):
        raise CliError("az-gh: Azure CLI returned no pull request details")
    repository = details.get("repository") or {}
    repository_id = str(repository.get("id") or ctx.repository or "")
    source_commit = (details.get("lastMergeSourceCommit") or {}).get("commitId")
    target_commit = (details.get("lastMergeTargetCommit") or {}).get("commitId")
    repository_url = repository.get("webUrl") or repository.get("remoteUrl")
    repository_org = repository_project = None
    if isinstance(repository_url, str):
        repository_org, repository_project, _ = parse_remote(repository_url)
    project_info = repository.get("project")
    if isinstance(project_info, dict):
        repository_project = repository_project or project_info.get("name") or project_info.get("id")
    project = ctx.project or repository_project
    if not project:
        raise CliError("az-gh: Azure project is required for pr diff; set AZDO_PROJECT or use --repo PROJECT/REPOSITORY")
    if not repository_id or not source_commit or not target_commit:
        raise CliError("az-gh: pull request is missing repository or merge commit information")

    # ``gh pr diff --repo HOST/OWNER/REPOSITORY`` supplies a GitHub-shaped
    # identifier.  In Azure mode that identifier cannot identify the REST
    # project, but ``az repos pr show --id`` (used by fetch_pr when no Azure
    # repository is selected) returns the authoritative project metadata.
    # Carry that metadata into the follow-up requests instead of requiring
    # AZDO_PROJECT from the caller.
    diff_ctx = RepoContext(ctx.organization or repository_org, str(project), repository_id)

    args = [
        "devops", "invoke", "--area", "git", "--resource", "commitDiffs",
        "--route-parameters", f"project={diff_ctx.project}", f"repositoryId={repository_id}",
        "--query-parameters",
        f"baseVersion={target_commit}&baseVersionType=commit&targetVersion={source_commit}&targetVersionType=commit",
        "--api-version", "7.1",
    ]
    if diff_ctx.organization:
        args += ["--organization", diff_ctx.organization]
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
            old_text = _item_content(az, diff_ctx, repository_id, original_path, target_commit)
        if "newContent" not in change and change_type not in {"delete", "remove"}:
            new_text = _item_content(az, diff_ctx, repository_id, path, source_commit)
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        header = f"diff --git a{original_path} b{path}\n"
        patch = "".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"a{original_path}", tofile=f"b{path}", lineterm="\n"))
        if patch and not patch.endswith("\n"):
            patch += "\n"
        chunks.append(header + patch)
    emit("stdout", "".join(chunks).encode("utf-8"))
    return 0
