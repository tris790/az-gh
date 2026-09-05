from __future__ import annotations

from pathlib import Path
import unittest

from azgh.graphql import project_graphql_response
from azgh.prs import markdown_to_html
from tools.parse_jsonl import json_outputs, load_transcript, shape


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_TRACE = next(
    (ROOT / name for name in ("official_commands.jsonl", "official-commands.jsonl") if (ROOT / name).exists()),
    ROOT / "official_commands.jsonl",
)
AZURE_TRANSCRIPTS = (
    ROOT / "gh-az-commands.jsonl",
    ROOT / "az-gh-commands.jsonl",
)


class TranscriptToolTests(unittest.TestCase):
    @staticmethod
    def query_for(command: dict[str, object]) -> str:
        for event in command.get("events", []):
            if not isinstance(event, dict) or event.get("event") != "start":
                continue
            for argument in event.get("argv", []):
                if isinstance(argument, str) and argument.startswith("query="):
                    return argument.split("=", 1)[1]
        return ""

    def test_loads_recordings_by_command_and_decodes_json_outputs(self) -> None:
        commands = load_transcript(OFFICIAL_TRACE)
        outputs = json_outputs(OFFICIAL_TRACE)
        self.assertGreater(len(commands), 0)
        self.assertGreater(len(outputs), 0)
        summary = next(
            value
            for _, value in outputs
            if isinstance(value, dict)
            and isinstance(value.get("data"), dict)
            if isinstance(value.get("data", {}).get("p0"), dict)
            and isinstance(value["data"]["p0"].get("pullRequest"), dict)
        )
        self.assertIsInstance(summary["data"]["p0"]["pullRequest"]["number"], int)

    def test_shape_ignores_domain_values(self) -> None:
        left = {"data": {"pullRequest": {"number": 2, "title": "GitHub"}}}
        right = {"data": {"pullRequest": {"number": 42, "title": "Azure"}}}
        self.assertEqual(shape(left), shape(right))

    def test_shape_keeps_distinct_shapes_in_nonempty_arrays(self) -> None:
        left = [{"number": 2}, {"title": "GitHub"}]
        right = [{"number": 42}, {"body": "Azure"}]
        self.assertNotEqual(shape(left), shape(right))

    def test_graphql_projection_respects_inline_fragment_runtime_type(self) -> None:
        query = """
        query {
          repository {
            pullRequest {
              reviewRequests(first: 10) {
                nodes {
                  requestedReviewer {
                    __typename
                    ... on User { login avatarUrl }
                    ... on Team { name slug }
                  }
                }
              }
            }
          }
        }
        """
        response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewRequests": {
                            "nodes": [{
                                "requestedReviewer": {
                                    "__typename": "User",
                                    "login": "alice@example.com",
                                    "avatarUrl": "https://example.test/alice",
                                    "name": None,
                                    "slug": None,
                                }
                            }]
                        }
                    }
                }
            }
        }
        projected = project_graphql_response(response, query)
        reviewer = projected["data"]["repository"]["pullRequest"]["reviewRequests"]["nodes"][0]["requestedReviewer"]
        self.assertEqual(
            reviewer,
            {
                "__typename": "User",
                "login": "alice@example.com",
                "avatarUrl": "https://example.test/alice",
            },
        )

    def test_graphql_projection_respects_conditional_directives(self) -> None:
        query = """
        query($includeDetails: Boolean!, $skipExtra: Boolean!) {
          repository {
            pullRequest {
              number
              details @include(if: $includeDetails)
              extra @skip(if: $skipExtra)
            }
          }
        }
        """
        response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "details": "should be omitted",
                        "extra": "should be omitted",
                    }
                }
            }
        }
        projected = project_graphql_response(
            response,
            query,
            {"includeDetails": "false", "skipExtra": "true"},
        )
        self.assertEqual(
            projected["data"]["repository"]["pullRequest"],
            {"number": 42},
        )

    def test_recorded_pr_content_queries_preserve_their_selected_fields(self) -> None:
        official = next(
            value
            for _, value in json_outputs(OFFICIAL_TRACE)
            if isinstance(value, dict)
            and isinstance(value.get("data"), dict)
            if isinstance(value.get("data", {}).get("repository"), dict)
            and set(value["data"]["repository"].get("pullRequest", {}))
            == {"body", "bodyHTML"}
        )
        for azure_transcript in AZURE_TRANSCRIPTS:
            if not azure_transcript.exists():
                continue
            azure_bodies = [
                value
                for _, value in json_outputs(azure_transcript)
                if isinstance(value, dict)
                and isinstance(value.get("data"), dict)
                and isinstance(value["data"].get("repository"), dict)
                and set(value["data"]["repository"].get("pullRequest", {}))
                == {"body", "bodyHTML"}
            ]
            if azure_bodies:
                self.assertEqual(shape(azure_bodies[0]), shape(official))
            break

        # The checked-in official recording provides a stable rich query. The
        # live Azure recording is intentionally allowed to contain a
        # different command sequence.
        rich_command, rich_output = next(
            (command, value)
            for command, value in json_outputs(OFFICIAL_TRACE)
            if "includeComments" in self.query_for(command)
            and isinstance(value, dict)
            and isinstance(value.get("data"), dict)
            and isinstance(value.get("data", {}).get("repository"), dict)
            and isinstance(value["data"]["repository"].get("pullRequest"), dict)
            and "commits" in value["data"]["repository"]["pullRequest"]
        )
        rich_query = self.query_for(rich_command)
        projected = project_graphql_response(rich_output, rich_query)
        self.assertEqual(shape(projected), shape(rich_output))
        self.assertEqual(
            set(official["data"]["repository"]["pullRequest"]),
            {"body", "bodyHTML"},
        )

        body_html = markdown_to_html(
            "# add a file explorerasdf\n\n- a\n- b\n- c\n\n"
            "http://localhost:3000/prs/1?repoId=repo&tab=overview"
        )
        self.assertIn('<h1 dir="auto">add a file explorerasdf</h1>', body_html)
        self.assertIn('<ul dir="auto">', body_html)
        self.assertIn('<a href="http://localhost:3000/prs/1?', body_html)

        self.assertEqual(
            markdown_to_html("line one\r\nhttps://example.test/path"),
            '<p dir="auto">line one<br>\n'
            '<a href="https://example.test/path" rel="nofollow">'
            'https://example.test/path</a></p>',
        )


if __name__ == "__main__":
    unittest.main()
