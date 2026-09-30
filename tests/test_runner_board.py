#!/usr/bin/env python3
"""Unit tests for the local GitHub Actions runner board."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import github_runner_board as MODULE

ROOT = Path(__file__).resolve().parents[1]


class RunnerBoardTests(unittest.TestCase):
    def test_api_list_paginates(self):
        client = MODULE.GitHubClient("owner/repository")
        calls = []

        def api(path):
            calls.append(path)
            return {"runners": [{"id": len(calls)}] * (100 if len(calls) == 1 else 2)}

        client.api = api
        runners = client.runners()
        self.assertEqual(len(runners), 102)
        self.assertEqual(calls, ["actions/runners?per_page=100&page=1", "actions/runners?per_page=100&page=2"])

    def test_active_runs_survive_one_status_query_failure(self):
        client = MODULE.GitHubClient("owner/repository")

        def api_list(path, key, limit=None):
            if "status=waiting" in path:
                raise RuntimeError("temporary GitHub API failure")
            return [{"id": 42, "created_at": "2026-09-28T00:00:00Z"}]

        client.api_list = api_list
        client.add_workflow_names = lambda runs: runs
        self.assertEqual([run["id"] for run in client.active_runs()], [42])

    def test_config_precedence_is_cli_then_environment_then_file(self):
        config = {"repo": "file/repository"}
        with patch.dict("os.environ", {"RUNNER_BOARD_REPO": "env/repository"}, clear=False):
            self.assertEqual(
                MODULE.configured_value(None, config, "repo", "RUNNER_BOARD_REPO", None),
                "env/repository",
            )
        self.assertEqual(
            MODULE.configured_value("cli/repository", config, "repo", "RUNNER_BOARD_REPO", None),
            "cli/repository",
        )

    def test_recent_history_is_limited_to_one_day_and_maps_to_runner_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MODULE.BoardStore(Path(directory))
            now = MODULE.utc_now()
            recent = MODULE.timestamp(now - MODULE.dt.timedelta(hours=1))
            old = MODULE.timestamp(now - MODULE.dt.timedelta(hours=25))
            store.add_executions([
                {
                    "jobId": "recent",
                    "runId": "run-recent",
                    "workflow": "CI",
                    "name": "test",
                    "runnerId": 7,
                    "runner": "runner-a",
                    "labels": ["self-hosted", "linux"],
                    "status": "completed",
                    "startedAt": recent,
                    "completedAt": MODULE.timestamp(now - MODULE.dt.timedelta(minutes=30)),
                    "durationSeconds": 1800,
                },
                {
                    "jobId": "old",
                    "runId": "run-old",
                    "workflow": "CI",
                    "name": "test",
                    "runnerId": 8,
                    "runner": "runner-old",
                    "labels": ["self-hosted", "linux"],
                    "status": "completed",
                    "startedAt": old,
                    "completedAt": MODULE.timestamp(now - MODULE.dt.timedelta(hours=24, minutes=30)),
                    "durationSeconds": 1800,
                },
            ])
            store.update_execution_context([{
                "runId": "run-recent",
                "headBranch": "feature/merged",
                "event": "pull_request",
                "pullRequestNumber": 624,
                "pullRequestUrl": "https://github.com/owner/repository/pull/624",
                "runDisplayTitle": "merged change",
            }])
            collector = MODULE.Collector.__new__(MODULE.Collector)
            collector.client = type("Client", (), {"repository": "owner/repository"})()
            collector.store = store
            state = collector._build_state(
                [{"id": 7, "name": "runner-a", "status": "online", "busy": False, "labels": []}],
                [],
                MODULE.timestamp(now),
                [],
            )
            self.assertEqual([job["id"] for job in state["history"]], ["recent"])
            self.assertIn({"id": "7", "label": "runner-a", "runnerId": 7}, state["lanes"])
            self.assertEqual(state["history"][0]["pullRequestNumber"], 624)

    def test_historical_runner_name_is_used_for_historical_only_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MODULE.BoardStore(Path(directory))
            now = MODULE.utc_now()
            store.add_executions([{
                "jobId": "recent",
                "runId": "run-recent",
                "workflow": "CI",
                "name": "test",
                "runnerId": 99,
                "runner": "retired-runner",
                "labels": [],
                "status": "completed",
                "startedAt": MODULE.timestamp(now - MODULE.dt.timedelta(hours=1)),
                "completedAt": MODULE.timestamp(now - MODULE.dt.timedelta(minutes=30)),
                "durationSeconds": 1800,
            }])
            collector = MODULE.Collector.__new__(MODULE.Collector)
            collector.client = type("Client", (), {"repository": "owner/repository"})()
            collector.store = store
            state = collector._build_state([], [], MODULE.timestamp(now), [])
            self.assertIn(
                {"id": "99", "label": "retired-runner", "runnerId": "99", "historicalOnly": True},
                state["lanes"],
            )

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
        self.assertEqual(MODULE.lane_from_labels(["self-hosted", "arm64", "gpu"]), "arm64")
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

    def test_workflow_name_is_preferred_to_run_title(self):
        collector = MODULE.Collector.__new__(MODULE.Collector)
        job = collector._normalise_job(
            {
                "id": 42,
                "workflow_name": "Dependabot Updates",
                "name": "npm_and_yarn update title",
                "status": "queued",
            },
            {"id": 7, "name": "Dependabot", "status": "queued"},
        )
        self.assertEqual(job["workflow"], "Dependabot Updates")
        self.assertEqual(job["name"], "Dependabot")

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

    def test_github_hosted_job_does_not_create_unassigned_self_hosted_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = MODULE.Collector.__new__(MODULE.Collector)
            collector.client = type("Client", (), {"repository": "rob179994/pantrify"})()
            collector.store = MODULE.BoardStore(Path(directory))
            state = collector._build_state(
                [{
                    "id": 1,
                    "name": "pantrify-mini-linux",
                    "os": "Linux",
                    "status": "online",
                    "busy": True,
                    "labels": [{"name": "self-hosted"}, {"name": "mini-linux"}],
                }],
                [{
                    "id": 2,
                    "workflow": "Dependabot",
                    "name": "Dependabot",
                    "status": "in_progress",
                    "runner": "GitHub Actions 1000004695",
                    "labels": ["ubuntu-latest"],
                    "startedAt": "2026-09-22T16:00:00Z",
                    "createdAt": "2026-09-22T16:00:00Z",
                }],
                "2026-09-22T16:05:00Z",
                [],
            )
            self.assertNotIn("unassigned", [lane["id"] for lane in state["lanes"]])
            self.assertIn("github-hosted", [lane["id"] for lane in state["lanes"]])
            self.assertEqual(state["jobs"][0]["assignedRunner"], "github-hosted")

    def test_custom_runner_label_stays_on_matching_self_hosted_runner(self):
        collector = MODULE.Collector.__new__(MODULE.Collector)
        job = collector._normalise_job(
            {"id": 42, "workflow_name": "CI", "status": "in_progress", "run_started_at": "2026-09-22T16:00:00Z"},
            {"id": 7, "name": "Preflight", "status": "queued", "labels": ["mini-linux"]},
        )
        job["estimate"] = {"p50Seconds": 60, "p90Seconds": 120, "sampleSize": 3}
        collector._schedule(
            [job],
            [{"id": 23, "name": "pantrify-mini-linux", "status": "online", "busy": False, "labels": ["self-hosted", "mini-linux"]}],
            MODULE.utc_now(),
        )
        self.assertEqual(job["assignedRunner"], "pantrify-mini-linux")
        self.assertNotEqual(job["assignedRunnerId"], "github-hosted")

    def test_github_hosted_history_uses_single_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MODULE.BoardStore(Path(directory))
            now = MODULE.utc_now()
            store.add_executions([{
                "jobId": "hosted-history",
                "runId": "run-hosted-history",
                "workflow": "CI",
                "name": "hosted test",
                "runnerId": 1000004696,
                "runner": "GitHub Actions 1000004696",
                "labels": ["ubuntu-latest"],
                "status": "completed",
                "startedAt": MODULE.timestamp(now - MODULE.dt.timedelta(minutes=20)),
                "completedAt": MODULE.timestamp(now - MODULE.dt.timedelta(minutes=10)),
                "durationSeconds": 600,
                "event": "pull_request",
                "pullRequestNumber": 624,
                "pullRequestUrl": "https://github.com/owner/repository/pull/624",
            }])
            collector = MODULE.Collector.__new__(MODULE.Collector)
            collector.client = type("Client", (), {"repository": "owner/repository"})()
            collector.store = store
            state = collector._build_state([], [], MODULE.timestamp(now), [])
            self.assertIn("github-hosted", [lane["id"] for lane in state["lanes"]])
            self.assertNotIn("1000004696", [lane["id"] for lane in state["lanes"]])
            self.assertEqual(state["history"][0]["runner"], "github-hosted")
            self.assertEqual(state["history"][0]["pullRequestNumber"], 624)
            self.assertEqual(state["history"][0]["pullRequestUrl"], "https://github.com/owner/repository/pull/624")
            self.assertTrue(MODULE.is_github_hosted_job({"historical": True, "runnerId": 0, "runner": "", "labels": ["mini-linux"]}))
            self.assertFalse(MODULE.is_github_hosted_job({"status": "queued", "runnerId": 0, "runner": None, "labels": ["mini-linux"]}))

    def test_html_contains_mobile_view_and_background_refresh_copy(self):
        html = (ROOT / "github_runner_board" / "static" / "index.html").read_text()
        self.assertIn('name="viewport"', html)
        self.assertIn("updates in background", html)
        self.assertIn("/api/state", html)
        self.assertIn("--track-width", html)
        self.assertIn('id="completed-runs"', html)
        self.assertIn('id="runner-time"', html)
        self.assertIn('id="average-duration"', html)
        self.assertIn("const totalRuntime = history.reduce", html)
        self.assertIn("const duration = (seconds)", html)
        self.assertIn("scrollLeft", html)
        self.assertIn("Now ${clock(now)}", html)
        self.assertIn("visibilitychange", html)
        self.assertIn("pageshow", html)
        self.assertIn("refreshInFlight", html)
        self.assertIn("package=com.github.android", html)
        self.assertIn("data-github-url", html)
        self.assertIn("data-job-id", html)
        self.assertIn("outcomeClass", html)
        self.assertIn("historical.passed", html)
        self.assertIn("historical.failed", html)
        self.assertIn("historical.cancelled", html)
        self.assertIn('class=\"legend passed\"', html)
        self.assertIn("bindJobHighlights", html)
        self.assertIn("mouseover", html)
        self.assertIn("focusin", html)
        self.assertIn('id="active-list"', html)
        self.assertIn('id="history-list"', html)
        self.assertIn("renderActive", html)
        self.assertIn("renderHistory", html)
        self.assertIn("jobSource", html)
        self.assertIn("merged PR", html)
        self.assertIn("/api/events", html)
        self.assertIn("EventSource", html)
        self.assertIn("last 24 hours", html)
        self.assertIn("historical", html)
        self.assertIn("TIMELINE_RANGES", html)
        self.assertIn("const TIMELINE_TRACK_WIDTH = 1200", html)
        self.assertIn("const TIMELINE_HISTORY_MS = 24 * 60 * 60 * 1000", html)
        self.assertIn("const TIMELINE_FUTURE_MS = 6 * 60 * 60 * 1000", html)
        self.assertIn("const trackWidth = Math.max(visibleTrackWidth", html)
        self.assertIn("const domainSpan = TIMELINE_DOMAIN_MS", html)
        self.assertIn("timelineView.centerMs = timelineView.startMs + centerOffset / timelineView.trackWidth * TIMELINE_DOMAIN_MS", html)
        self.assertIn("job.assignedRunnerId ??", html)
        self.assertIn('startsWith("GitHub Actions ")', html)
        self.assertIn("timelineView.centerMs = Date.now()", html)
        self.assertIn('label: "1 min"', html)
        self.assertIn('label: "6 hours"', html)
        self.assertIn("timelineGridLabel(gridStep)", html)
        self.assertIn("timelineView", html)
        self.assertIn("data-timeline-action=\"zoom-in\"", html)
        self.assertIn("pointerdown", html)
        self.assertIn("MIN_TIMELINE_RANGE_MS", html)


if __name__ == "__main__":
    unittest.main()
