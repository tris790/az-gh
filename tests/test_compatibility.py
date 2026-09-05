from __future__ import annotations

from pathlib import Path
import unittest

from tools.replay_compat import compare_partial_transcript, compare_transcripts, replay


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = next(
    (ROOT / name for name in ("official_commands.jsonl", "official-commands.jsonl") if (ROOT / name).exists()),
    ROOT / "official_commands.jsonl",
)
REPLAY_AZ = ROOT / "tests" / "fixtures" / "replay_az.py"


class OfficialCommandCompatibilityTests(unittest.TestCase):
    def test_every_official_command_replays_with_the_same_shape(self) -> None:
        _, mismatches = replay(
            OFFICIAL,
            az=REPLAY_AZ,
            base_env={
                "AZ_GH_AZDO_ORG": "https://dev.azure.com/replay",
                "AZ_GH_AZDO_PROJECT": "Weather",
                "AZ_GH_AZDO_REPOSITORY": "weather",
            },
        )
        self.assertEqual([], [mismatch.format() for mismatch in mismatches])

    def test_comparator_reports_json_path_for_shape_mismatch(self) -> None:
        expected = ROOT / "tests" / "fixtures" / "expected_shape.jsonl"
        actual = ROOT / "tests" / "fixtures" / "actual_shape.jsonl"
        mismatches = compare_transcripts(expected, actual)
        self.assertEqual(1, len(mismatches))
        self.assertIn("output 1 shape", mismatches[0].format())
        self.assertIn("$.data.pullRequest.number", mismatches[0].format())

    def test_comparator_allows_repository_data_variance(self) -> None:
        expected = ROOT / "tests" / "fixtures" / "expected_data_variance.jsonl"
        actual = ROOT / "tests" / "fixtures" / "actual_data_variance.jsonl"
        mismatches = compare_transcripts(expected, actual)
        self.assertEqual([], mismatches)

    def test_partial_capture_matches_official_query_shapes(self) -> None:
        capture = ROOT / "broken2_commands.jsonl"
        if not capture.exists():
            self.skipTest("focused capture is not present")
        self.assertEqual([], compare_partial_transcript(OFFICIAL, capture))
