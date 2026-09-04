from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import urlparse

from .azcli import AzCli
from .errors import CliError
from .identity import account, configured_username
from .prs import fetch_pr, fetch_prs, normalize_pr
from .repository import RepoContext


def parse_form_fields(raw_fields: list[str], typed_fields: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in [*raw_fields, *typed_fields]:
        if "=" not in field:
            raise CliError(f"az-gh: field must be in KEY=VALUE form: {field}")
        key, value = field.split("=", 1)
        if not key:
            raise CliError("az-gh: field name cannot be empty")
        values[key] = value
    return values


def _search_state(search_query: str) -> str:
    if re.search(r"(?:^|\s)is:open(?:\s|$)", search_query):
        return "open"
    if re.search(r"(?:^|\s)is:merged(?:\s|$)", search_query):
        return "merged"
    if re.search(r"(?:^|\s)is:closed(?:\s|$)", search_query):
        return "closed"
    return "all"


def _repository_name(item: dict[str, Any], ctx: RepoContext) -> str:
    repository = item.get("repository")
    if isinstance(repository, dict) and repository.get("name"):
        return str(repository["name"])
    return str(ctx.repository or "")


def _owner_name(item: dict[str, Any], ctx: RepoContext) -> str:
    if ctx.organization:
        return ctx.organization.rstrip("/").rsplit("/", 1)[-1].split(".", 1)[0]
    raw = item.get("raw") or {}
    repository = raw.get("repository") or {}
    source_url = raw.get("url") or repository.get("url")
    if isinstance(source_url, str):
        host = urlparse(source_url).hostname or ""
        if host.endswith(".visualstudio.com"):
            return host.split(".", 1)[0]
        path = [part for part in urlparse(source_url).path.split("/") if part]
        if host == "dev.azure.com" and path:
            return path[0]
    return ""


def _repository(item: dict[str, Any], ctx: RepoContext) -> dict[str, Any]:
    raw = item.get("raw") or item
    repository = raw.get("repository") or {}
    return {
        "name": repository.get("name") or ctx.repository or "",
        "owner": {"login": _owner_name(item, ctx)},
    }


def _graphql_node(item: dict[str, Any], ctx: RepoContext) -> dict[str, Any]:
    raw = item.get("raw") or {}
    author = item.get("author") or {}
    raw_author = raw.get("createdBy") or {}
    number = item.get("number")
    return {
        "__typename": "PullRequest",
        "additions": raw.get("additions", 0),
        "author": {
            "avatarUrl": raw_author.get("imageUrl") if isinstance(raw_author, dict) else None,
            "login": author.get("login") if isinstance(author, dict) else None,
        },
        "baseRefName": item.get("baseRefName"),
        "createdAt": item.get("createdAt"),
        "deletions": raw.get("deletions", 0),
        "headRefName": item.get("headRefName"),
        "id": f"AZDO_PR_{number}",
        "isDraft": item.get("isDraft", False),
        "mergeStateStatus": raw.get("mergeStateStatus"),
        "mergeable": raw.get("mergeable"),
        "number": number,
        "repository": _repository(item, ctx),
        "state": item.get("state"),
        "statusCheckRollup": {"contexts": {"totalCount": 0, "nodes": []}},
        "title": item.get("title"),
        "updatedAt": item.get("updatedAt"),
        "url": item.get("url"),
    }


def _truthy(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes"}


def _number(values: dict[str, str], query: str) -> str:
    if values.get("number"):
        return values["number"]
    match = re.search(r"pullRequest\s*\(\s*number\s*:\s*(\d+)", query)
    if match:
        return match.group(1)
    raise CliError("az-gh: graphql pullRequest requires number=")


def _effective_context(ctx: RepoContext, item: dict[str, Any]) -> RepoContext:
    raw = item.get("raw") or item
    repository = raw.get("repository") or {}
    project = repository.get("project") or {}
    project_name = project.get("name") if isinstance(project, dict) else None
    source_url = repository.get("webUrl") or repository.get("remoteUrl") or raw.get("url")
    organization = ctx.organization
    if not organization and isinstance(source_url, str):
        parsed = urlparse(source_url)
        if parsed.hostname and parsed.hostname.endswith(".visualstudio.com"):
            organization = f"{parsed.scheme}://{parsed.hostname}"
        elif parsed.hostname == "dev.azure.com":
            path = [part for part in parsed.path.split("/") if part]
            if path:
                organization = f"{parsed.scheme}://{parsed.hostname}/{path[0]}"
    return RepoContext(
        organization,
        ctx.project or project_name,
        ctx.repository or repository.get("name"),
    )


def _reviewer_state(reviewer: dict[str, Any]) -> str:
    vote = reviewer.get("vote", 0)
    if vote is None:
        vote = 0
    if vote < 0:
        return "CHANGES_REQUESTED"
    if vote > 0:
        return "APPROVED"
    return "PENDING"


def _reviewer_nodes(details: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for reviewer in details.get("reviewers") or []:
        if not isinstance(reviewer, dict):
            continue
        author = {
            "login": reviewer.get("displayName") or reviewer.get("uniqueName"),
            "avatarUrl": reviewer.get("imageUrl"),
        }
        state = _reviewer_state(reviewer)
        nodes.append({"author": author, "state": state, "comments": {"totalCount": 0}})
    return nodes


def _review_request_nodes(details: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for reviewer in details.get("reviewers") or []:
        if not isinstance(reviewer, dict):
            continue
        nodes.append({
            "requestedReviewer": {
                "__typename": "User",
                "login": reviewer.get("displayName") or reviewer.get("uniqueName"),
                "avatarUrl": reviewer.get("imageUrl"),
            }
        })
    return nodes


def _review_decision(details: dict[str, Any]) -> str | None:
    states = [_reviewer_state(r) for r in details.get("reviewers") or [] if isinstance(r, dict)]
    if "CHANGES_REQUESTED" in states:
        return "CHANGES_REQUESTED"
    if states and all(state == "APPROVED" for state in states):
        return "APPROVED"
    return None


def _page_info() -> dict[str, Any]:
    return {"hasNextPage": False, "endCursor": None}


def _invoke_optional(az: AzCli, ctx: RepoContext, resource: str, number: str, repository_id: str) -> dict[str, Any]:
    if not ctx.project or not repository_id:
        return {}
    args = [
        "devops", "invoke", "--area", "git", "--resource", resource,
        "--route-parameters", f"project={ctx.project}", f"repositoryId={repository_id}",
        f"pullRequestId={number}", "--api-version", "7.1-preview.1",
    ]
    if ctx.organization:
        args += ["--organization", ctx.organization]
    try:
        result = az.run([*args, "--output", "json"])
    except CliError:
        return {}
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout.decode("utf-8")) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _commit_nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for commit in data.get("value") or []:
        if not isinstance(commit, dict):
            continue
        author = commit.get("author") or {}
        nodes.append({
            "commit": {
                "oid": commit.get("commitId"),
                "committedDate": author.get("date"),
                "messageHeadline": commit.get("comment"),
                "url": commit.get("url"),
                "authors": {"nodes": [{
                    "name": author.get("name"),
                    "user": {"login": author.get("email")},
                }]},
            }
        })
    return nodes


def _comment(comment: dict[str, Any]) -> dict[str, Any]:
    author = comment.get("author") or {}
    body = comment.get("content") or comment.get("body") or ""
    return {
        "author": {
            "__typename": "User",
            "login": author.get("displayName") or author.get("uniqueName"),
            "avatarUrl": author.get("imageUrl"),
        },
        "body": body,
        "bodyHTML": body,
        "createdAt": comment.get("publishedDate") or comment.get("lastUpdatedDate"),
        "id": str(comment.get("id", "")),
        "url": None,
        "viewerCanDelete": False,
        "viewerCanUpdate": False,
    }


def _thread_nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for thread in data.get("value") or []:
        if not isinstance(thread, dict):
            continue
        context = thread.get("threadContext") or {}
        comments = [_comment(c) for c in thread.get("comments") or [] if isinstance(c, dict)]
        nodes.append({
            "id": str(thread.get("id", "")),
            "comments": {"nodes": comments, "pageInfo": _page_info()},
            "diffSide": "RIGHT",
            "isResolved": str(thread.get("status", "")).lower() in {"fixed", "closed"},
            "line": context.get("rightFileStart", {}).get("line") if isinstance(context.get("rightFileStart"), dict) else None,
            "originalLine": context.get("leftFileStart", {}).get("line") if isinstance(context.get("leftFileStart"), dict) else None,
            "originalStartLine": None,
            "path": context.get("filePath"),
            "startDiffSide": "RIGHT",
            "startLine": None,
            "viewerCanResolve": False,
            "viewerCanUnresolve": False,
        })
    return nodes


def _single_pr_response(az: AzCli, ctx: RepoContext, values: dict[str, str], query: str) -> dict[str, Any]:
    number = _number(values, query)
    details = fetch_pr(az, ctx, number)
    normalized = normalize_pr(details)
    effective_ctx = _effective_context(ctx, normalized)
    pr = _graphql_node(normalized, effective_ctx)
    raw = normalized.get("raw") or {}
    source_commit = (raw.get("lastMergeSourceCommit") or {}).get("commitId")
    pr.update({
        "body": normalized.get("body") or "",
        "bodyHTML": normalized.get("body") or "",
        "headRefOid": source_commit,
        "mergedAt": normalized.get("mergedAt"),
        "mergedBy": {"login": (raw.get("closedBy") or {}).get("displayName")} if raw.get("closedBy") else None,
        "reviewDecision": _review_decision(raw),
        "latestReviews": {"nodes": _reviewer_nodes(raw), "pageInfo": _page_info()},
        "reviewRequests": {"nodes": _review_request_nodes(raw), "pageInfo": _page_info()},
        "reviews": {"nodes": _reviewer_nodes(raw), "pageInfo": _page_info()},
        "autoMergeRequest": {"enabledAt": raw.get("completionQueueTime")} if raw.get("completionQueueTime") else None,
    })
    repository_id = str((raw.get("repository") or {}).get("id") or "")
    if "commits" in query:
        commits = _invoke_optional(az, effective_ctx, "commits", number, repository_id)
        pr["commits"] = {"nodes": _commit_nodes(commits), "pageInfo": {"hasPreviousPage": False}}
    if "comments" in query or "reviewThreads" in query:
        threads = _invoke_optional(az, effective_ctx, "threads", number, repository_id)
        thread_nodes = _thread_nodes(threads)
        if "comments" in query:
            pr["comments"] = {"nodes": [comment for thread in thread_nodes for comment in thread["comments"]["nodes"]], "pageInfo": _page_info()}
        if "reviewThreads" in query:
            pr["reviewThreads"] = {"nodes": thread_nodes, "pageInfo": _page_info()}

    repository_key_match = re.search(r"(?:(\w+)\s*:\s*)?repository\s*\(", query)
    repository_key = repository_key_match.group(1) if repository_key_match and repository_key_match.group(1) else "repository"
    result: dict[str, Any] = {repository_key: {"pullRequest": pr}}
    if "viewer" in query:
        result["viewer"] = {"login": configured_username(account(az))}
    return result


def graphql_api(
    az: AzCli,
    ctx: RepoContext,
    raw_fields: list[str],
    typed_fields: list[str],
    emit: Callable[[str, bytes], None],
) -> int:
    values = parse_form_fields(raw_fields, typed_fields)
    query = values.get("query", "")
    search_query = values.get("searchQuery")
    if not query:
        raise CliError("az-gh: graphql requires query= form field")
    if "search(" not in query and "pullRequest(" in query:
        result = {"data": _single_pr_response(az, ctx, values, query)}
        emit("stdout", (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        return 0
    if "search(" not in query or "PullRequest" not in query or not search_query:
        raise CliError("az-gh: only GraphQL pull-request search and detail queries are supported")

    first: int | None = None
    if values.get("first"):
        try:
            first = int(values["first"])
        except ValueError as exc:
            raise CliError("az-gh: graphql first= must be an integer") from exc
        if first < 1:
            raise CliError("az-gh: graphql first= must be positive")
    if values.get("after") not in (None, "", "null"):
        raise CliError("az-gh: GraphQL pagination with after= is not supported")

    reviewer = None
    if re.search(r"(?:user-)?review-requested:@me|reviewed-by:@me", search_query):
        reviewer = configured_username(account(az))
    author = "@me" if re.search(r"(?:^|\s)author:@me(?:\s|$)", search_query) else None
    normalized = fetch_prs(
        az,
        ctx,
        state_filter=_search_state(search_query),
        limit=first,
        author=author,
        reviewer=reviewer,
    )
    if "sort:updated-desc" in search_query:
        normalized.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    nodes = [_graphql_node(item, ctx) for item in normalized]
    result = {
        "data": {
            "search": {
                "issueCount": len(nodes),
                "nodes": nodes,
                "pageInfo": {"endCursor": None, "hasNextPage": False},
            }
        }
    }
    emit("stdout", (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return 0
