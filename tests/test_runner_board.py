#!/usr/bin/env python3
"""Unit tests for the local GitHub Actions runner board."""

from pathlib import Path
import tempfile
import unittest

import github_runner_board as MODULE

ROOT = Path(__file__).resolve().parents[1]


class RunnerBoardTests(unittest.TestCase):
    def test_duration_stats_report_median_and_p90(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MODULE.BoardStore(Path(directory))
            store.add_samples([
                (str(index), "CI", "backend", "mini-linux", duration, "2026-09-22T00:00:00Z")
                for index, duration in enumerate((10, 20, 30, 40, 100))
            ])
            stats = store.duration_stats()[MODULE.BoardStore.key("CI", "backend", "mini-linux")]
            self.assertEqual(stats["p50Seconds"], 30)
            self.assertEqual(stats["p90Seconds"], 100)
            self.assertEqual(stats["sampleSize"], 5)

    def test_runner_matches_all_required_labels(self):
        runner = {"labels": [{"name": "self-hosted"}, {"name": "linux"}, {"name": "mini-linux"}]}
        self.assertTrue(MODULE.runner_matches(runner, ["self-hosted", "mini-linux"]))
        self.assertFalse(MODULE.runner_matches(runner, ["self-hosted", "macos"]))
        self.assertTrue(MODULE.runner_matches({"labels": ["self-hosted", "mini-linux"]}, ["mini-linux"]))

    def test_lane_prefers_the_explicit_mini_label(self):
        self.assertEqual(MODULE.lane_from_labels(["self-hosted", "linux", "mini-linux"]), "mini-linux")
        self.assertEqual(MODULE.lane_from_labels(["self-hosted", "macos"]), "macos")
        self.assertIsNone(MODULE.lane_from_labels(["ubuntu-latest"]))

    def test_queued_job_does_not_use_github_placeholder_started_at(self):
        collector = MODULE.Collector.__new__(MODULE.Collector)
        run = {
            "id": 42,
            "workflow_name": "CI",
            "status": "in_progress",
            "run_started_at": "2026-09-22T16:00:00Z",
        }
        queued = collector._normalise_job(
            run,
            {
                "id": 7,
                "name": "Preflight",
                "status": "queued",
                "created_at": "2026-09-22T16:05:00Z",
                "started_at": "2026-09-22T16:05:00Z",
            },
        )
        self.assertIsNone(queued["startedAt"])

    def test_in_progress_job_can_fall_back_to_run_start(self):
        collector = MODULE.Collector.__new__(MODULE.Collector)
        active = collector._normalise_job(
            {"id": 42, "workflow_name": "CI", "status": "in_progress", "run_started_at": "2026-09-22T16:00:00Z"},
            {"id": 7, "name": "Preflight", "status": "in_progress"},
        )
        self.assertEqual(active["startedAt"], "2026-09-22T16:00:00Z")

    def test_synthetic_run_waits_for_job_details_instead_of_being_blocked(self):
        collector = MODULE.Collector.__new__(MODULE.Collector)
        job = collector._normalise_job(
            {"id": 42, "workflow_name": "CI", "status": "pending"},
            {"id": "run-42", "name": "CI", "synthetic": True},
        )
        job["estimate"] = {"p50Seconds": None, "p90Seconds": None, "sampleSize": 0}
        jobs = [job]
        collector._schedule(jobs, [], MODULE.utc_now())
        self.assertEqual(jobs[0]["predictionConfidence"], "unknown")
        self.assertEqual(jobs[0]["queueReason"], "Waiting for GitHub to publish job details.")

    def test_html_contains_mobile_view_and_background_refresh_copy(self):
        html = (ROOT / "github_runner_board" / "static" / "index.html").read_text()
        self.assertIn('name="viewport"', html)
        self.assertIn("updates in background", html)
        self.assertIn("/api/state", html)
        self.assertIn("--track-width", html)
        self.assertIn("scrollLeft", html)
        self.assertIn("Now ${clock(now)}", html)
        self.assertIn("visibilitychange", html)
        self.assertIn("pageshow", html)
        self.assertIn("refreshInFlight", html)
        self.assertIn("package=com.github.android", html)
        self.assertIn("data-github-url", html)


if __name__ == "__main__":
    unittest.main()
