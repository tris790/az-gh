from __future__ import annotations

import difflib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

from .azcli import AzCli
from .errors import CliError
from .identity import account, configured_username
from .prs import (
    fetch_pr,
    fetch_prs,
    head_repository,
    identity_login,
    load_pr_diff_stats,
    markdown_to_html,
    normalize_pr,
    pr_diff_stats,
    pr_url,
    repository_owner,
    _item_content,
)
from .repository import RepoContext, normalize_org


# (response/output name, schema field name, children, inline-fragment type
# condition, directives). The conditions are retained because GraphQL only
# includes fields from an inline fragment or conditional directive when it
# applies to the current response.
GraphQLSelection = tuple[
    str,
    str,
    list["GraphQLSelection"],
    str | None,
    tuple[tuple[str, str], ...],
]


class _GraphQLSelectionParser:
    """Parse the small selection-set subset needed by gh's GraphQL requests.

    This is deliberately a selection parser rather than a complete GraphQL
    parser. Arguments are skipped because they affect the Azure lookup, not
    the response projection. Conditional directives are retained so the
    projected response has the same field-presence semantics as GraphQL.
    Keeping the parser here avoids adding a runtime dependency for a
    compatibility shim.
    """

    _name = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

    def __init__(self, query: str) -> None:
        self.tokens = self._tokenize(query)
        self.position = 0

    @classmethod
    def _tokenize(cls, query: str) -> list[str]:
        tokens: list[str] = []
        position = 0
        while position < len(query):
            character = query[position]
            if character.isspace() or character == ",":
                position += 1
                continue
            if character == "#":
                newline = query.find("\n", position)
                position = len(query) if newline < 0 else newline + 1
                continue
            if query.startswith("...", position):
                tokens.append("...")
                position += 3
                continue
            if character in "{}():!@$[]":
                tokens.append(character)
                position += 1
                continue
            if character in "\"'":
                quote = character
                end = position + 1
                escaped = False
                while end < len(query):
                    current = query[end]
                    if current == quote and not escaped:
                        end += 1
                        break
                    escaped = current == "\\" and not escaped
                    if current != "\\":
                        escaped = False
                    end += 1
                tokens.append("STRING")
                position = end
                continue
            match = cls._name.match(query, position)
            if match:
                tokens.append(match.group(0))
                position = match.end()
                continue
            # Numeric literals and other argument syntax are irrelevant to
            # the selection shape. Retain punctuation boundaries and move on.
            position += 1
        return tokens

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self) -> str | None:
        token = self._peek()
        if token is not None:
            self.position += 1
        return token

    def _skip_balanced(self, opening: str, closing: str) -> None:
        if self._peek() != opening:
            return
        depth = 0
        while (token := self._take()) is not None:
            if token == opening:
                depth += 1
            elif token == closing:
                depth -= 1
                if depth == 0:
                    return

    def _skip_directives(self) -> tuple[tuple[str, str], ...]:
        conditions: list[tuple[str, str]] = []
        while self._peek() == "@":
            self._take()
            directive = self._take()
            condition: str | None = None
            if self._peek() == "(":
                self._take()
                while self._peek() not in (None, ")"):
                    argument = self._take()
                    if argument == "if" and self._peek() == ":":
                        self._take()
                        if self._peek() == "$":
                            self._take()
                            variable = self._take()
                            condition = "$" + (variable or "")
                        else:
                            condition = self._take()
                if self._peek() == ")":
                    self._take()
            if directive in {"include", "skip"} and condition:
                conditions.append((directive, condition))
        return tuple(conditions)

    def selection_set(self) -> list[GraphQLSelection]:
        if self._take() != "{":
            return []
        selections: list[GraphQLSelection] = []
        while self._peek() not in (None, "}"):
            if self._peek() == "...":
                self._take()
                if self._peek() == "on":
                    self._take()
                    type_condition = self._take()
                    fragment_conditions = self._skip_directives()
                    selections.extend(
                        (output_name, field_name, children, type_condition, (*fragment_conditions, *conditions))
                        for output_name, field_name, children, _, conditions in self.selection_set()
                    )
                else:
                    self._take()  # fragment name
                    self._skip_directives()
                continue

            first = self._take()
            if first in (None, "}"):
                break
            output_name = first
            field_name = first
            if self._peek() == ":":
                self._take()
                field_name = self._take() or first
            self._skip_balanced("(", ")")
            conditions = self._skip_directives()
            children = self.selection_set() if self._peek() == "{" else []
            selections.append((output_name, field_name, children, None, conditions))
        if self._peek() == "}":
            self._take()
        return selections

    def parse(self) -> list[GraphQLSelection]:
        while self._peek() not in (None, "{"):
            self._take()
        return self.selection_set()


def _merge_selections(selections: list[GraphQLSelection]) -> list[GraphQLSelection]:
    merged: dict[str, GraphQLSelection] = {}
    order: list[str] = []
    for output_name, field_name, children, type_condition, conditions in selections:
        if output_name not in merged:
            merged[output_name] = (output_name, field_name, list(children), type_condition, conditions)
            order.append(output_name)
            continue
        existing_output, existing_field, existing_children, existing_condition, existing_conditions = merged[output_name]
        merged[output_name] = (
            existing_output,
            existing_field,
            _merge_selections([*existing_children, *children]),
            existing_condition or type_condition,
            existing_conditions if existing_conditions == conditions else (),
        )
    return [merged[name] for name in order]


def _directive_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").lower() in {"1", "true", "yes"}


def _conditions_match(conditions: tuple[tuple[str, str], ...], variables: dict[str, str] | None) -> bool:
    for directive, operand in conditions:
        if operand.startswith("$"):
            if variables is None or operand[1:] not in variables:
                # A missing variable is invalid for a real GraphQL request.
                # Keep projection permissive for callers processing a saved
                # response without its original form fields.
                value = True
            else:
                value = variables[operand[1:]]
        else:
            value = operand
        truthy = _directive_truthy(value)
        if directive == "include" and not truthy:
            return False
        if directive == "skip" and truthy:
            return False
    return True


def _project_graphql_value(
    value: Any,
    selections: list[GraphQLSelection],
    variables: dict[str, str] | None = None,
) -> Any:
    if value is None or not selections:
        return value
    if isinstance(value, list):
        return [_project_graphql_value(item, selections, variables) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    runtime_type = value.get("__typename")
    for output_name, field_name, children, type_condition, conditions in _merge_selections(selections):
        if type_condition and runtime_type != type_condition:
            continue
        if not _conditions_match(conditions, variables):
            continue
        # Internal responses use schema field names. Falling back to the
        # output name also makes this helper safe to apply to an already
        # projected recording that contains an alias.
        selected = value.get(field_name, value.get(output_name))
        result[output_name] = _project_graphql_value(selected, children, variables)
    return result


def project_graphql_response(
    response: dict[str, Any],
    query: str,
    variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return only the fields selected by *query*, as GitHub GraphQL does."""
    selections = _GraphQLSelectionParser(query).parse()
    data = response.get("data")
    return {"data": _project_graphql_value(data, selections, variables)}


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


def _search_repository(search_query: str) -> tuple[str, str] | None:
    match = re.search(r"(?:^|\s)repo:([^\s]+)", search_query, re.IGNORECASE)
    if not match:
        return None
    owner, separator, name = match.group(1).partition("/")
    if not separator or not owner or not name:
        return ("", "")
    return owner, name


def _matches_repository(item: dict[str, Any], repository_filter: tuple[str, str]) -> bool:
    owner_filter, name_filter = repository_filter
    raw = item.get("raw") or {}
    repository = raw.get("repository") or {}
    actual_name = repository.get("name") if isinstance(repository, dict) else None
    if not actual_name and isinstance(repository, dict):
        repository_url = repository.get("webUrl") or repository.get("remoteUrl") or repository.get("url")
        if isinstance(repository_url, str) and "/_git/" in repository_url:
            actual_name = unquote(repository_url.split("/_git/", 1)[1].split("/", 1)[0])
    if not actual_name and isinstance(raw.get("url"), str) and "/_git/" in raw["url"]:
        actual_name = unquote(raw["url"].split("/_git/", 1)[1].split("/", 1)[0])
    actual_owner = repository_owner(raw)
    return (
        isinstance(actual_name, str)
        and actual_name.casefold() == name_filter.casefold()
        and isinstance(actual_owner, str)
        and actual_owner.casefold() == owner_filter.casefold()
    )


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


def _repository_url(item: dict[str, Any]) -> str | None:
    raw = item.get("raw") or item
    repository = raw.get("repository") or {}
    for key in ("webUrl", "remoteUrl"):
        value = repository.get(key)
        if value:
            return str(value).rstrip("/")
    url = item.get("url")
    if isinstance(url, str) and "/pullrequest/" in url:
        return url.split("/pullrequest/", 1)[0]
    return None


def _head_repository(item: dict[str, Any], ctx: RepoContext) -> dict[str, Any] | None:
    raw = item.get("raw") or item
    if "sourceRepository" in raw:
        source_repository = raw.get("sourceRepository")
        if not isinstance(source_repository, dict) or not source_repository:
            return None
        repository = head_repository({"repository": source_repository}) or {}
    else:
        repository = head_repository(raw) or {}
    if not repository.get("url"):
        repository["url"] = _repository_url(item)
    if not repository.get("name"):
        repository["name"] = _repository_name(item, ctx) or None
    if not repository.get("owner"):
        repository["owner"] = {"login": _owner_name(item, ctx)}
    if not repository.get("nameWithOwner") and repository.get("owner", {}).get("login") and repository.get("name"):
        repository["nameWithOwner"] = f"{repository['owner']['login']}/{repository['name']}"
    return repository


def _github_repository_url(repository: dict[str, Any], fallback: dict[str, Any] | None = None) -> str | None:
    """Return the GitHub-shaped repository URL used by the compatibility host."""
    owner_data = repository.get("owner") if isinstance(repository, dict) else None
    owner = owner_data.get("login") if isinstance(owner_data, dict) else None
    name = repository.get("name") if isinstance(repository, dict) else None
    if fallback:
        fallback_owner = fallback.get("owner")
        owner = owner or (
            fallback_owner.get("login")
            if isinstance(fallback_owner, dict)
            else None
        )
        name = name or fallback.get("name")
    if not owner or not name:
        return None
    return f"https://github.com/{quote(str(owner), safe='')}/{quote(str(name), safe='')}"


def _detail_context(ctx: RepoContext, values: dict[str, str], query: str) -> RepoContext:
    owner = values.get("owner")
    repository = values.get("repo")
    match = re.search(
        r"repository\s*\(\s*owner\s*:\s*[\"']([^\"']+)[\"']\s*,\s*"
        r"name\s*:\s*[\"']([^\"']+)[\"']",
        query,
    )
    if match:
        owner = owner or match.group(1)
        repository = repository or match.group(2)
    if not owner and not repository:
        return ctx
    return RepoContext(
        ctx.organization or normalize_org(owner),
        ctx.project,
        ctx.repository or repository,
    )


def _graphql_node(
    item: dict[str, Any],
    ctx: RepoContext,
    *,
    github_url: bool = False,
    diff_stats: tuple[int, int] | None = None,
) -> dict[str, Any]:
    raw = item.get("raw") or {}
    author = item.get("author") or {}
    raw_author = raw.get("createdBy") or {}
    number = item.get("number")
    url = item.get("url")
    repository_data = _repository(item, ctx)
    head_repository_data = _head_repository(item, ctx)
    if github_url and number is not None:
        owner = repository_data.get("owner", {}).get("login")
        name = repository_data.get("name")
        if owner and name:
            # The desktop GitHub integration parses this field as a GitHub
            # pull-request reference before it asks gh for the detail. Keep
            # the Azure browser URL in normal CLI output, but provide the
            # GitHub-shaped URL that the compatibility caller can consume.
            url = f"https://github.com/{quote(str(owner), safe='')}/{quote(str(name), safe='')}/pull/{number}"
        if head_repository_data:
            github_head_url = _github_repository_url(head_repository_data, repository_data)
            if github_head_url:
                head_repository_data = {**head_repository_data, "url": github_head_url}
    return {
        "__typename": "PullRequest",
        "additions": raw.get("additions") if raw.get("additions") is not None else (
            diff_stats[0] if diff_stats is not None else 0
        ),
        "author": {
            "avatarUrl": raw_author.get("imageUrl") if isinstance(raw_author, dict) else None,
            "login": author.get("login") if isinstance(author, dict) else None,
        },
        "baseRefName": item.get("baseRefName"),
        "body": item.get("body") or "",
        "bodyHTML": item.get("bodyHTML") or markdown_to_html(item.get("body")),
        "createdAt": item.get("createdAt"),
        "deletions": raw.get("deletions") if raw.get("deletions") is not None else (
            diff_stats[1] if diff_stats is not None else 0
        ),
        "headRefName": item.get("headRefName"),
        "id": f"AZDO_PR_{number}",
        "isDraft": item.get("isDraft", False),
        # Azure does not expose GitHub's mergeability enums on the PR list
        # endpoint. UNKNOWN is a valid GitHub enum value and is preferable to
        # changing the field's nullable shape for consumers that deserialize
        # this object as a GitHub PullRequest.
        "mergeStateStatus": raw.get("mergeStateStatus") or "UNKNOWN",
        "mergeable": raw.get("mergeable") or "UNKNOWN",
        "number": number,
        "repository": repository_data,
        "state": item.get("state"),
        # Azure's PR list endpoint does not provide GitHub check-rollup data.
        # GitHub represents an absent rollup as null, not as an empty
        # CheckRollup object. Keeping that nullable shape is important to
        # clients that distinguish "no checks" from "checks loaded".
        "statusCheckRollup": None,
        "title": item.get("title"),
        "updatedAt": item.get("updatedAt"),
        "url": url,
        "headRepository": head_repository_data,
    }


def _truthy(value: str | None) -> bool:
    return _directive_truthy(value)


def _query_includes_field(query: str, field_name: str, values: dict[str, str]) -> bool:
    """Return whether an effective GraphQL selection requests *field_name*."""
    selections = _GraphQLSelectionParser(query).parse()

    def walk(current: list[GraphQLSelection]) -> bool:
        for _, selected_field, children, _, conditions in current:
            if _conditions_match(conditions, values) and selected_field == field_name:
                return True
            if walk(children):
                return True
        return False

    return walk(selections)


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
    """Return actual review records only.

    Azure's pull-request ``reviewers`` collection contains reviewer votes,
    not GitHub-style PullRequestReview records. Votes are exposed through
    ``reviewDecision`` and pending reviewers through ``reviewRequests``.
    Returning fabricated reviews would produce misleading null review bodies
    and timestamps.
    """
    return []


def _review_request_nodes(details: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for reviewer in details.get("reviewers") or []:
        if not isinstance(reviewer, dict):
            continue
        if reviewer.get("hasDeclined") or reviewer.get("vote") not in (None, 0):
            continue
        is_team = bool(reviewer.get("isContainer")) or str(reviewer.get("reviewerType", "")).lower() == "team"
        if is_team:
            requested = {
                "__typename": "Team",
                "name": reviewer.get("displayName"),
                "slug": reviewer.get("uniqueName") or reviewer.get("displayName"),
            }
        else:
            requested = {
                "__typename": "User",
                "login": identity_login(reviewer),
                "avatarUrl": reviewer.get("imageUrl"),
            }
        nodes.append({"requestedReviewer": requested})
    return nodes


def _review_decision(details: dict[str, Any]) -> str | None:
    states = [_reviewer_state(r) for r in details.get("reviewers") or [] if isinstance(r, dict)]
    if "CHANGES_REQUESTED" in states:
        return "CHANGES_REQUESTED"
    if states and all(state == "APPROVED" for state in states):
        return "APPROVED"
    return None


def _page_info() -> dict[str, Any]:
    return {
        "hasNextPage": False,
        "hasPreviousPage": False,
        "startCursor": None,
        "endCursor": None,
    }


def _invoke_optional(
    az: AzCli,
    ctx: RepoContext,
    resource: str,
    number: str,
    repository_id: str,
    query_parameters: str | None = None,
) -> dict[str, Any]:
    if not ctx.project or not repository_id:
        return {}
    args = [
        "devops", "invoke", "--area", "git", "--resource", resource,
        "--route-parameters", f"project={ctx.project}", f"repositoryId={repository_id}",
        f"pullRequestId={number}", "--api-version", "7.1",
    ]
    if query_parameters:
        args += ["--query-parameters", query_parameters]
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


def _browser_commit_url(commit: dict[str, Any], details: dict[str, Any] | None) -> str | None:
    raw_url = commit.get("url")
    repository = (details or {}).get("repository") or {}
    base_url = repository.get("webUrl") or repository.get("remoteUrl") if isinstance(repository, dict) else None
    commit_id = commit.get("commitId")
    if isinstance(base_url, str) and commit_id:
        return f"{base_url.rstrip('/')}/commit/{quote(str(commit_id), safe='')}"
    return raw_url


def _github_commit_url(commit: dict[str, Any], details: dict[str, Any] | None) -> str | None:
    commit_id = commit.get("commitId")
    repository = (details or {}).get("repository") or {}
    if not isinstance(repository, dict) or not commit_id:
        return None
    owner = repository_owner({"repository": repository})
    name = repository.get("name")
    if not owner or not name:
        return None
    return (
        f"https://github.com/{quote(str(owner), safe='')}/{quote(str(name), safe='')}"
        f"/commit/{quote(str(commit_id), safe='')}"
    )


def _browser_comment_url(
    comment: dict[str, Any],
    fallback_url: str | None,
    thread_id: Any = None,
) -> str | None:
    links = comment.get("_links")
    href = ((links.get("self") or {}).get("href")) if isinstance(links, dict) and isinstance(links.get("self"), dict) else None
    if isinstance(href, str) and "/_apis/" not in href:
        return href
    if fallback_url:
        if thread_id is not None:
            return f"{fallback_url}?discussionId={quote(str(thread_id), safe='')}"
        return fallback_url
    return href


def _diff_hunk_for_line(
    old_text: str,
    new_text: str,
    path: str,
    line: int | None,
    side: str,
) -> str | None:
    if line is None:
        return None
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
        lineterm="",
    ))
    hunks: list[list[str]] = []
    current: list[str] | None = None
    for diff_line in diff:
        if diff_line.startswith("@@ "):
            if current is not None:
                hunks.append(current)
            current = [diff_line]
        elif current is not None:
            current.append(diff_line)
    if current is not None:
        hunks.append(current)

    header_pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for hunk in hunks:
        match = header_pattern.match(hunk[0])
        if not match:
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        start, count = (new_start, new_count) if side == "RIGHT" else (old_start, old_count)
        end = start + max(count, 1) - 1
        if start <= line <= end:
            return "\n".join(hunk)
    return None


def _thread_diff_hunks(
    az: AzCli,
    ctx: RepoContext,
    details: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, str]:
    repository = details.get("repository") or {}
    repository_id = str(repository.get("id") or "") if isinstance(repository, dict) else ""
    source_commit = details.get("lastMergeSourceCommit") or {}
    target_commit = details.get("lastMergeTargetCommit") or {}
    source_oid = source_commit.get("commitId") if isinstance(source_commit, dict) else None
    target_oid = target_commit.get("commitId") if isinstance(target_commit, dict) else None
    if not repository_id or not source_oid or not target_oid:
        return {}

    thread_inputs: list[tuple[str, str, int, str]] = []
    for thread in data.get("value") or []:
        if not isinstance(thread, dict) or not thread.get("threadContext"):
            continue
        context = thread["threadContext"]
        path = str(context.get("filePath") or "").lstrip("/")
        if not path:
            continue
        right_start = context.get("rightFileStart")
        left_start = context.get("leftFileStart")
        has_right = isinstance(right_start, dict) and right_start.get("line") is not None
        has_left = isinstance(left_start, dict) and left_start.get("line") is not None
        if has_right:
            side, line = "RIGHT", int(right_start["line"])
        elif has_left:
            side, line = "LEFT", int(left_start["line"])
        else:
            continue
        thread_inputs.append((str(thread.get("id", "")), path, line, side))

    paths = sorted({path for _, path, _, _ in thread_inputs})

    def fetch_pair(path: str) -> tuple[str, str, str]:
        try:
            old_text = _item_content(
                az, ctx, repository_id, path, str(target_oid), emit_errors=False
            )
        except CliError:
            old_text = ""
        try:
            new_text = _item_content(
                az, ctx, repository_id, path, str(source_oid), emit_errors=False
            )
        except CliError:
            new_text = ""
        return path, old_text, new_text

    file_contents: dict[str, tuple[str, str]] = {}
    if paths:
        with ThreadPoolExecutor(max_workers=min(8, len(paths))) as executor:
            for path, old_text, new_text in executor.map(fetch_pair, paths):
                file_contents[path] = (old_text, new_text)

    result: dict[str, str] = {}
    for thread_id, path, line, side in thread_inputs:
        old_text, new_text = file_contents.get(path, ("", ""))
        hunk = _diff_hunk_for_line(old_text, new_text, path, line, side)
        if hunk:
            result[thread_id] = hunk
    return result


def _commit_nodes(
    data: dict[str, Any],
    limit: int | None = None,
    details: dict[str, Any] | None = None,
    github_url: bool = False,
    head_oid: str | None = None,
) -> list[dict[str, Any]]:
    nodes = []
    commits = data.get("value") or []
    if limit is not None:
        if limit == 1 and head_oid:
            # The GitHub CLI summary query uses commits(last: 1) as a
            # consistency check: its only node must be the PR head commit.
            # Azure's pull-request commits endpoint can return an older
            # commit first, so select the source commit explicitly.
            head_commit = next(
                (
                    commit
                    for commit in commits
                    if isinstance(commit, dict) and commit.get("commitId") == head_oid
                ),
                None,
            )
            # The PR detail already supplies the authoritative source
            # revision. Preserve the one-node GitHub shape even if Azure's
            # commit listing is truncated or temporarily omits that item.
            commits = [
                head_commit
                if head_commit is not None
                else {"commitId": head_oid, "author": {}, "comment": None}
            ]
        else:
            # Azure returns pull-request commits newest first for larger
            # windows, which is close enough to GitHub's last-N connection.
            commits = commits[:limit]
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        author = commit.get("author") or {}
        commit_url = _github_commit_url(commit, details) if github_url else None
        nodes.append({
            "commit": {
                "oid": commit.get("commitId"),
                "committedDate": author.get("date"),
                "messageHeadline": commit.get("comment"),
                "url": commit_url or _browser_commit_url(commit, details),
                "statusCheckRollup": None,
                "authors": {"nodes": [{
                    "name": author.get("name"),
                    "user": {"login": identity_login(author)},
                }]},
            }
        })
    return nodes


def _comment(
    comment: dict[str, Any],
    fallback_url: str | None = None,
    thread_id: Any = None,
    diff_hunk: str | None = None,
    github_url: bool = False,
) -> dict[str, Any]:
    author = comment.get("author") or {}
    body = comment.get("content") or comment.get("body") or ""
    url = _browser_comment_url(comment, fallback_url, thread_id)
    if github_url and fallback_url:
        url = f"{fallback_url}?discussionId={quote(str(thread_id), safe='')}" if thread_id is not None else fallback_url
    return {
        "author": {
            "__typename": "User",
            "login": identity_login(author),
            "avatarUrl": author.get("imageUrl"),
        },
        "body": body,
        "bodyHTML": markdown_to_html(body),
        "createdAt": comment.get("publishedDate") or comment.get("lastUpdatedDate"),
        "id": str(comment.get("id", "")),
        "url": url,
        "viewerCanDelete": False,
        "viewerCanUpdate": False,
        "diffHunk": comment.get("diffHunk") or diff_hunk,
    }


def _conversation_comments(
    data: dict[str, Any],
    fallback_url: str | None = None,
    github_url: bool = False,
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for thread in data.get("value") or []:
        if not isinstance(thread, dict) or thread.get("threadContext"):
            continue
        thread_id = thread.get("id")
        comments.extend(
            _comment(comment, fallback_url, thread_id, github_url=github_url)
            for comment in thread.get("comments") or []
            if isinstance(comment, dict)
            and str(comment.get("commentType", "text")).lower() != "system"
            and str(comment.get("content") or comment.get("body") or "").strip()
        )
    return comments


def _thread_nodes(
    data: dict[str, Any],
    fallback_url: str | None = None,
    diff_hunks: dict[str, str] | None = None,
    github_url: bool = False,
) -> list[dict[str, Any]]:
    nodes = []
    for thread in data.get("value") or []:
        if not isinstance(thread, dict):
            continue
        context = thread.get("threadContext") or {}
        if not context:
            continue
        thread_id = thread.get("id")
        diff_hunk = (diff_hunks or {}).get(str(thread_id))
        comments = [
            _comment(comment, fallback_url, thread_id, diff_hunk, github_url)
            for comment in thread.get("comments") or []
            if isinstance(comment, dict)
            and str(comment.get("commentType", "text")).lower() != "system"
            and str(comment.get("content") or comment.get("body") or "").strip()
        ]
        if not comments:
            continue
        right_start = context.get("rightFileStart")
        left_start = context.get("leftFileStart")
        has_right = isinstance(right_start, dict) and right_start.get("line") is not None
        has_left = isinstance(left_start, dict) and left_start.get("line") is not None
        diff_side = "RIGHT" if has_right or not has_left else "LEFT"
        nodes.append({
            "id": str(thread.get("id", "")),
            "comments": {"nodes": comments, "pageInfo": _page_info()},
            "diffSide": diff_side,
            "isResolved": str(thread.get("status", "")).lower() in {"fixed", "closed", "wontfix", "bydesign"},
            "line": right_start.get("line") if has_right else None,
            "originalLine": left_start.get("line") if has_left else None,
            "originalStartLine": left_start.get("line") if has_left else None,
            "path": str(context.get("filePath") or "").lstrip("/") or None,
            "startDiffSide": diff_side,
            "startLine": right_start.get("line") if has_right else (left_start.get("line") if has_left else None),
            "viewerCanResolve": False,
            "viewerCanUnresolve": False,
        })
    return nodes


def _single_pr_response(
    az: AzCli,
    ctx: RepoContext,
    values: dict[str, str],
    query: str,
    *,
    github_url: bool = False,
) -> dict[str, Any]:
    number = _number(values, query)
    detail_ctx = _detail_context(ctx, values, query)
    details = fetch_pr(az, detail_ctx, number, emit_errors=False)
    normalized = normalize_pr(details)
    effective_ctx = _effective_context(detail_ctx, normalized)
    needs_diff_stats = (
        _query_includes_field(query, "additions", values)
        or _query_includes_field(query, "deletions", values)
    )
    diff_stats = pr_diff_stats(az, effective_ctx, details) if needs_diff_stats else None
    pr = _graphql_node(
        normalized,
        effective_ctx,
        github_url=github_url,
        diff_stats=diff_stats,
    )
    raw = normalized.get("raw") or {}
    browser_pr_url = pr_url(raw)
    pr.update({
        "body": normalized.get("body") or "",
        "bodyHTML": markdown_to_html(normalized.get("body")),
        "headRefOid": normalized.get("headRefOid"),
        "mergedAt": normalized.get("mergedAt"),
        "mergedBy": {"login": identity_login(raw.get("closedBy"))} if raw.get("closedBy") else None,
        "reviewDecision": _review_decision(raw),
        "latestReviews": {"nodes": _reviewer_nodes(raw), "pageInfo": _page_info()},
        "reviewRequests": {"nodes": _review_request_nodes(raw), "pageInfo": _page_info()},
        "reviews": {"nodes": _reviewer_nodes(raw), "pageInfo": _page_info()},
        "autoMergeRequest": {"enabledAt": raw.get("completionQueueTime")} if raw.get("completionQueueTime") else None,
    })
    repository_id = str((raw.get("repository") or {}).get("id") or "")
    # These lookups are independent after the PR detail has established the
    # repository id and project. Running them concurrently keeps the full PR
    # view close to the latency of the official single GraphQL request.
    include_commits = _query_includes_field(query, "commits", values)
    include_comments = _query_includes_field(query, "comments", values)
    include_threads = _query_includes_field(query, "reviewThreads", values)
    include_viewer = _query_includes_field(query, "viewer", values)
    with ThreadPoolExecutor(max_workers=3) as executor:
        commits_future = None
        commit_limit = None
        if include_commits:
            last_match = re.search(r"commits\s*\([^)]*\blast\s*:\s*(\d+)", query)
            commit_limit = int(last_match.group(1)) if last_match else None
            # Fetch enough Azure commits to locate the actual PR head when
            # the GitHub query asks for commits(last: 1). Azure's first item
            # is not guaranteed to be the source commit, so asking Azure for
            # only one item can make the GitHub summary consistency check
            # reject an otherwise valid pull request.
            azure_commit_limit = 100 if commit_limit == 1 else commit_limit
            commits_future = executor.submit(
                _invoke_optional,
                az,
                effective_ctx,
                "commits",
                number,
                repository_id,
                f"$top={azure_commit_limit}" if azure_commit_limit is not None else None,
            )

        threads_future = None
        if include_comments or include_threads:
            threads_future = executor.submit(
                _invoke_optional,
                az,
                effective_ctx,
                "pullRequestThreads",
                number,
                repository_id,
            )

        viewer_future = executor.submit(account, az) if include_viewer else None

        if commits_future is not None:
            commits = commits_future.result()
            pr["commits"] = {
                "nodes": _commit_nodes(
                    commits,
                    commit_limit,
                    raw,
                    github_url,
                    normalized.get("headRefOid"),
                ),
                "pageInfo": _page_info(),
            }
        if threads_future is not None:
            threads = threads_future.result()
            diff_hunks = (
                _thread_diff_hunks(az, effective_ctx, raw, threads)
                if include_threads and _query_includes_field(query, "diffHunk", values)
                else {}
            )
            resource_pr_url = pr["url"] if github_url else browser_pr_url
            thread_nodes = _thread_nodes(threads, resource_pr_url, diff_hunks, github_url)
            if include_comments:
                pr["comments"] = {
                    "nodes": _conversation_comments(threads, resource_pr_url, github_url),
                    "pageInfo": _page_info(),
                }
            if include_threads:
                pr["reviewThreads"] = {"nodes": thread_nodes, "pageInfo": _page_info()}

    # Keep the internal response keyed by the schema field name. The
    # projection step applies any GraphQL alias (for example ``p0:``) to the
    # wire response.
    result: dict[str, Any] = {
        "repository": {
            # These are Repository fields in GitHub's schema, not
            # PullRequest fields.
            "mergeCommitAllowed": False,
            "squashMergeAllowed": False,
            "pullRequest": pr,
        }
    }
    if include_viewer:
        viewer_data = viewer_future.result() if viewer_future is not None else {}
        result["viewer"] = {"login": _viewer_login(viewer_data, github_url)}
    return result


def _is_not_found_error(error: CliError) -> bool:
    message = str(error).lower()
    return "tf401180" in message or "not found" in message or "could not resolve" in message


def _viewer_login(viewer_data: dict[str, Any], github_url: bool) -> str:
    login = configured_username(viewer_data)
    return login.split("@", 1)[0] if github_url and "@" in login else login


def _graphql_error_message(error: CliError) -> str:
    message = str(error).strip()
    message = re.sub(r"^ERROR:\s*TF\d+:\s*", "", message, flags=re.IGNORECASE)
    return message.removeprefix("az-gh: ")


def _graphql_not_found_response(
    az: AzCli, query: str, error: CliError, *, github_url: bool = False
) -> dict[str, Any]:
    try:
        viewer_data = account(az)
    except CliError:
        viewer_data = {}
    selections = _GraphQLSelectionParser(query).parse()
    data: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for output_name, field_name, _, _, conditions in selections:
        if not _conditions_match(conditions, None):
            continue
        if field_name == "viewer":
            data[output_name] = {"login": _viewer_login(viewer_data, github_url)}
        elif field_name == "repository":
            data[output_name] = None
            errors.append({
                "type": "NOT_FOUND",
                "path": [output_name],
                "locations": [{"line": 1, "column": 1}],
                "message": _graphql_error_message(error),
            })
    return {"data": data, "errors": errors}


def graphql_api(
    az: AzCli,
    ctx: RepoContext,
    raw_fields: list[str],
    typed_fields: list[str],
    emit: Callable[[str, bytes], None],
    *,
    hostname: str | None = None,
) -> int:
    values = parse_form_fields(raw_fields, typed_fields)
    query = values.get("query", "")
    search_query = values.get("searchQuery")
    if not query:
        raise CliError("az-gh: graphql requires query= form field")
    if "search(" not in query and "pullRequest(" in query:
        try:
            result = {
                "data": _single_pr_response(
                    az,
                    ctx,
                    values,
                    query,
                    github_url=(hostname or "").lower() == "github.com",
                )
            }
        except CliError as error:
            if not _is_not_found_error(error):
                raise
            result = _graphql_not_found_response(
                az,
                query,
                error,
                github_url=(hostname or "").lower() == "github.com",
            )
            emit(
                "stdout",
                (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
            )
            emit("stderr", f"gh: {_graphql_error_message(error)}\n".encode("utf-8"))
            return error.exit_code
        result = project_graphql_response(result, query, values)
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
    repository_filter = _search_repository(search_query)
    normalized = fetch_prs(
        az,
        ctx,
        state_filter=_search_state(search_query),
        # Apply repo filtering before the limit. Azure's --top applies to the
        # whole project, while GitHub's search limit applies after repo
        # filtering.
        limit=None if repository_filter else first,
        author=author,
        reviewer=reviewer,
    )
    if repository_filter is not None:
        normalized = [item for item in normalized if _matches_repository(item, repository_filter)]
    if "sort:updated-desc" in search_query:
        normalized.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    issue_count = len(normalized)
    visible = normalized[:first] if first is not None else normalized
    diff_stats = [None] * len(visible)
    if (
        _query_includes_field(query, "additions", values)
        or _query_includes_field(query, "deletions", values)
    ):
        diff_stats = load_pr_diff_stats(az, ctx, visible)
    nodes = [
        _graphql_node(
            item,
            ctx,
            github_url=(hostname or "").lower() == "github.com",
            diff_stats=diff_stats[index],
        )
        for index, item in enumerate(visible)
    ]
    result = {
        "data": {
            "search": {
                "issueCount": issue_count,
                "nodes": nodes,
                # GitHub supplies an opaque end cursor whenever the
                # connection contains nodes, including on its final page.
                "pageInfo": {
                    "endCursor": "AZ_GH_CURSOR" if nodes else None,
                    "hasNextPage": len(nodes) < issue_count,
                },
            }
        }
    }
    result = project_graphql_response(result, query, values)
    emit("stdout", (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return 0
