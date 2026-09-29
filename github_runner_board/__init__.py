#!/usr/bin/env python3
"""Serve a private, read-only GitHub Actions runner board."""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import http.server
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse


ACTIVE_STATUSES = ("queued", "in_progress", "waiting", "requested", "pending")
DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
UNKNOWN_DURATION_SECONDS = 15 * 60
HISTORY_LOOKBACK_SECONDS = 24 * 60 * 60
GITHUB_HOSTED_LANE_ID = "github-hosted"
SCRIPT_DIR = Path(__file__).resolve().parent
HTML_PATH = SCRIPT_DIR / "static" / "index.html"
LAUNCH_AGENT_LABEL = "io.github.runner-board"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def timestamp(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def seconds_between(start: str | None, end: str | None = None) -> float | None:
    start_time = parse_timestamp(start)
    end_time = parse_timestamp(end) if end else utc_now()
    if not start_time or not end_time:
        return None
    return max(0.0, (end_time - start_time).total_seconds())


def absolute_time_error(expected: str | None, actual: str | None) -> float | None:
    expected_time = parse_timestamp(expected)
    actual_time = parse_timestamp(actual)
    if not expected_time or not actual_time:
        return None
    return abs((actual_time - expected_time).total_seconds())


def format_error(error: Exception) -> str:
    message = str(error).strip().splitlines()[-1] if str(error).strip() else error.__class__.__name__
    return message[:240]


def default_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "github-runner-board" / "config.json"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read config file {path}: {format_error(error)}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"Config file {path} must contain a JSON object.")
    return payload


def configured_value(cli_value: Any, config: dict[str, Any], key: str, env_name: str, default: Any) -> Any:
    if cli_value is not None:
        return cli_value
    if env_name in os.environ:
        return os.environ[env_name]
    return config.get(key, default)


def configured_int(cli_value: int | None, config: dict[str, Any], key: str, env_name: str, default: int) -> int:
    value = configured_value(cli_value, config, key, env_name, default)
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{key} must be an integer") from error


def detect_repository(cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=cwd or Path.cwd(),
        check=True,
        capture_output=True,
        text=True,
    )
    remote = result.stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    if not match:
        raise RuntimeError("The origin remote is not a GitHub repository; pass --repo owner/name.")
    return f"{match.group(1)}/{match.group(2)}"


def tailscale_address() -> str:
    executable = shutil.which("tailscale")
    if not executable:
        raise RuntimeError("tailscale was not found; pass --host 127.0.0.1 or an explicit bind address.")
    result = subprocess.run([executable, "ip", "-4"], check=True, capture_output=True, text=True)
    address = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", address):
        raise RuntimeError("tailscale did not return an IPv4 address.")
    return address


def lane_from_labels(labels: list[str] | None) -> str | None:
    labels = labels or []
    if "self-hosted" not in labels:
        return None
    ignored = {"self-hosted", "linux", "macos", "windows"}
    for label in labels:
        if label not in ignored and not label.startswith(("ubuntu-", "windows-", "macos-")):
            return label
    for label in labels:
        if label.startswith("mini-"):
            return label
    for label in labels:
        if label in ("linux", "macos", "windows"):
            return label
    return None


def is_self_hosted_labels(labels: list[str] | None) -> bool:
    return "self-hosted" in (labels or [])


def is_github_hosted_labels(labels: list[str] | None) -> bool:
    labels = labels or []
    return bool(labels) and not is_self_hosted_labels(labels) and any(
        label.startswith(("ubuntu-", "windows-", "macos-"))
        for label in labels
        if isinstance(label, str)
    )


def is_github_hosted_job(job: dict[str, Any]) -> bool:
    if job.get("historical") and job.get("runnerId") in (0, "0"):
        return True
    runner = job.get("runner")
    return is_github_hosted_labels(job.get("labels")) or (
        isinstance(runner, str) and runner.startswith("GitHub Actions ")
    )


def runner_matches(runner: dict[str, Any], labels: list[str]) -> bool:
    runner_labels = {
        label.get("name") if isinstance(label, dict) else label
        for label in runner.get("labels", [])
    }
    return bool(labels) and set(labels).issubset(runner_labels)


def quantile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


class GitHubClient:
    def __init__(self, repository: str):
        self.repository = repository
        self._workflow_names: dict[str, str] | None = None

    def api(self, path: str) -> dict[str, Any]:
        result = subprocess.run(
            ["gh", "api", f"repos/{self.repository}/{path}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def api_list(self, path: str, key: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Read all pages from a GitHub list endpoint, up to an optional limit."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            payload = self.api(f"{path}{separator}per_page=100&page={page}")
            page_items = payload.get(key, [])
            if not isinstance(page_items, list):
                break
            items.extend(item for item in page_items if isinstance(item, dict))
            if limit is not None and len(items) >= limit:
                return items[:limit]
            if len(page_items) < 100:
                break
            page += 1
        return items

    def runners(self) -> list[dict[str, Any]]:
        return self.api_list("actions/runners", "runners")

    def workflow_names(self) -> dict[str, str]:
        """Return workflow-definition names keyed by workflow ID.

        Workflow-run ``name`` can be a commit, PR, or Dependabot update title;
        the workflows list is the reliable and efficient source for the actual
        workflow names.
        """
        if self._workflow_names is None:
            try:
                self._workflow_names = {
                    str(workflow["id"]): workflow["name"]
                    for workflow in self.api_list("actions/workflows", "workflows")
                    if workflow.get("id") is not None and isinstance(workflow.get("name"), str) and workflow["name"]
                }
            except Exception:
                self._workflow_names = {}
        return self._workflow_names

    def add_workflow_names(self, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        workflow_names = self.workflow_names()
        for run in runs:
            if not run.get("workflow_name"):
                name = workflow_names.get(str(run.get("workflow_id")))
                if name:
                    run["workflow_name"] = name
        return runs

    def active_runs(self) -> list[dict[str, Any]]:
        runs: dict[int, dict[str, Any]] = {}
        for status in ACTIVE_STATUSES:
            try:
                status_runs = self.api_list(f"actions/runs?status={status}", "workflow_runs")
            except Exception:
                # GitHub occasionally rejects or times out one status query.
                # Keep the other live statuses flowing instead of freezing the board.
                continue
            for run in status_runs:
                runs[run["id"]] = run
        return self.add_workflow_names(sorted(runs.values(), key=lambda run: run.get("created_at", "")))

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        return self.api_list(f"actions/runs/{run_id}/jobs?filter=latest", "jobs")

    def completed_runs(self, limit: int) -> list[dict[str, Any]]:
        return self.api_list("actions/runs?status=completed", "workflow_runs", limit=limit)

    def completed_runs_since(self, cutoff: str) -> list[dict[str, Any]]:
        """Read completed runs until the API's newest-first history is older than cutoff."""
        runs: list[dict[str, Any]] = []
        page = 1
        cutoff_time = parse_timestamp(cutoff)
        while True:
            payload = self.api(f"actions/runs?status=completed&per_page=100&page={page}")
            page_items = payload.get("workflow_runs", [])
            if not isinstance(page_items, list):
                break
            page_runs = [item for item in page_items if isinstance(item, dict)]
            runs.extend(
                run for run in page_runs
                if (not cutoff_time or parse_timestamp(run.get("updated_at") or run.get("created_at")) is None
                    or parse_timestamp(run.get("updated_at") or run.get("created_at")) >= cutoff_time)
            )
            timestamps = [
                parsed
                for parsed in (parse_timestamp(run.get("updated_at") or run.get("created_at")) for run in page_runs)
                if parsed
            ]
            if len(page_runs) < 100 or (cutoff_time and timestamps and min(timestamps) < cutoff_time):
                break
            page += 1
        return self.add_workflow_names(runs)


class BoardStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(data_dir / "runner-board.sqlite3", check_same_thread=False)
        self.lock = threading.Lock()
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS duration_samples (
                    job_id TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_executions (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    workflow TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    runner_id TEXT,
                    runner_name TEXT,
                    labels_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    conclusion TEXT,
                    created_at TEXT,
                    started_at TEXT,
                    completed_at TEXT NOT NULL,
                    html_url TEXT,
                    duration_seconds REAL NOT NULL,
                    head_branch TEXT,
                    event TEXT,
                    pull_request_number INTEGER,
                    pull_request_url TEXT,
                    run_display_title TEXT
                )
                """
            )
            execution_columns = {
                row[1]
                for row in self.connection.execute("PRAGMA table_info(job_executions)").fetchall()
            }
            for column, column_type in (
                ("head_branch", "TEXT"),
                ("event", "TEXT"),
                ("pull_request_number", "INTEGER"),
                ("pull_request_url", "TEXT"),
                ("run_display_title", "TEXT"),
            ):
                if column not in execution_columns:
                    self.connection.execute(f"ALTER TABLE job_executions ADD COLUMN {column} {column_type}")
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_forecasts (
                    job_id TEXT PRIMARY KEY,
                    predicted_start_at TEXT NOT NULL,
                    predicted_end_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """
            )

    def add_samples(self, samples: list[tuple[str, str, str, str, float, str]]) -> None:
        with self.lock, self.connection:
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO duration_samples
                (job_id, workflow, job_name, lane, duration_seconds, completed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                samples,
            )

    def duration_stats(self) -> dict[str, dict[str, float | int]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT workflow, job_name, lane, duration_seconds FROM duration_samples"
            ).fetchall()
        grouped: dict[str, list[float]] = {}
        for workflow, job_name, lane, duration in rows:
            grouped.setdefault(self.key(workflow, job_name, lane), []).append(duration)
            grouped.setdefault(self.key(workflow, job_name, None), []).append(duration)
        return {
            key: {
                "p50Seconds": round(statistics.median(values)),
                "p90Seconds": round(quantile(values, 0.9)),
                "sampleSize": len(values),
            }
            for key, values in grouped.items()
        }

    def add_executions(self, executions: list[dict[str, Any]]) -> None:
        rows = [
            (
                str(execution["jobId"]),
                str(execution.get("runId") or ""),
                execution.get("workflow", "Workflow"),
                execution.get("name", "Job"),
                str(execution["runnerId"]) if execution.get("runnerId") is not None else None,
                execution.get("runner"),
                json.dumps(execution.get("labels") or [], separators=(",", ":")),
                execution.get("status", "completed"),
                execution.get("conclusion"),
                execution.get("createdAt"),
                execution.get("startedAt"),
                execution["completedAt"],
                execution.get("htmlUrl"),
                float(execution["durationSeconds"]),
                execution.get("headBranch"),
                execution.get("event"),
                execution.get("pullRequestNumber"),
                execution.get("pullRequestUrl"),
                execution.get("runDisplayTitle"),
            )
            for execution in executions
            if execution.get("jobId") is not None
            and execution.get("completedAt")
            and execution.get("durationSeconds")
        ]
        if not rows:
            return
        with self.lock, self.connection:
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO job_executions
                (job_id, run_id, workflow, job_name, runner_id, runner_name, labels_json,
                 status, conclusion, created_at, started_at, completed_at, html_url, duration_seconds,
                 head_branch, event, pull_request_number, pull_request_url, run_display_title)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def update_execution_context(self, contexts: list[dict[str, Any]]) -> None:
        rows = [
            (
                context.get("headBranch"),
                context.get("event"),
                context.get("pullRequestNumber"),
                context.get("pullRequestUrl"),
                context.get("runDisplayTitle"),
                str(context["runId"]),
            )
            for context in contexts
            if context.get("runId") is not None
        ]
        if not rows:
            return
        with self.lock, self.connection:
            self.connection.executemany(
                """
                UPDATE job_executions
                SET head_branch = ?, event = ?, pull_request_number = ?, pull_request_url = ?, run_display_title = ?
                WHERE run_id = ?
                """,
                rows,
            )

    def recent_executions(self, cutoff: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT e.job_id, e.run_id, e.workflow, e.job_name, e.runner_id, e.runner_name, e.labels_json,
                       e.status, e.conclusion, e.created_at, e.started_at, e.completed_at, e.html_url,
                       e.duration_seconds, e.head_branch, e.event, e.pull_request_number, e.pull_request_url,
                       e.run_display_title, f.predicted_start_at, f.predicted_end_at
                FROM job_executions e
                LEFT JOIN job_forecasts f ON f.job_id = e.job_id
                WHERE e.completed_at >= ? OR (e.started_at IS NOT NULL AND e.started_at >= ?)
                ORDER BY COALESCE(e.started_at, e.completed_at)
                """,
                (cutoff, cutoff),
            ).fetchall()
        executions = []
        for (
            job_id,
            run_id,
            workflow,
            job_name,
            runner_id,
            runner_name,
            labels_json,
            status,
            conclusion,
            created_at,
            started_at,
            completed_at,
            html_url,
            duration_seconds,
            head_branch,
            event,
            pull_request_number,
            pull_request_url,
            run_display_title,
            predicted_start_at,
            predicted_end_at,
        ) in rows:
            labels = json.loads(labels_json)
            github_hosted = is_github_hosted_job({"historical": True, "labels": labels, "runner": runner_name, "runnerId": runner_id})
            display_runner_id = GITHUB_HOSTED_LANE_ID if github_hosted else runner_id
            display_runner_name = GITHUB_HOSTED_LANE_ID if github_hosted else runner_name
            executions.append({
                "id": job_id,
                "runId": run_id,
                "workflow": workflow,
                "name": job_name,
                "runnerId": display_runner_id,
                "runner": display_runner_name,
                "labels": labels,
                "status": status,
                "conclusion": conclusion,
                "createdAt": created_at,
                "startedAt": started_at,
                "completedAt": completed_at,
                "htmlUrl": html_url,
                "durationSeconds": duration_seconds,
                "headBranch": head_branch,
                "event": event,
                "pullRequestNumber": pull_request_number,
                "pullRequestUrl": pull_request_url,
                "runDisplayTitle": run_display_title,
                "predictedStartAt": predicted_start_at,
                "predictedEndAt": predicted_end_at,
                "startErrorSeconds": absolute_time_error(predicted_start_at, started_at),
                "finishErrorSeconds": absolute_time_error(predicted_end_at, completed_at),
                "historical": True,
                "assignedRunnerId": display_runner_id,
                "assignedRunner": display_runner_name,
            })
        return executions

    def record_forecasts(self, jobs: list[dict[str, Any]], recorded_at: str) -> None:
        rows = [
            (str(job["id"]), job["predictedStartAt"], job["predictedEndAt"], recorded_at)
            for job in jobs
            if job.get("id") is not None and job.get("predictedStartAt") and job.get("predictedEndAt")
        ]
        if not rows:
            return
        with self.lock, self.connection:
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO job_forecasts
                (job_id, predicted_start_at, predicted_end_at, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )

    def save_snapshot(self, captured_at: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"))
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO snapshots (captured_at, payload) VALUES (?, ?)", (captured_at, encoded)
            )
            self.connection.execute(
                "DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT 20)"
            )

    @staticmethod
    def key(workflow: str, job_name: str, lane: str | None) -> str:
        return "\x1f".join((workflow, job_name, lane or "*"))


class Collector:
    def __init__(self, client: GitHubClient, store: BoardStore, history_runs: int = 30):
        self.client = client
        self.store = store
        self.history_runs = history_runs
        self.last_history_refresh = 0.0
        self.history_refreshing = False
        self.history_lock = threading.Lock()
        self.history_error: str | None = None
        self.last_state: dict[str, Any] | None = None

    def refresh(self) -> dict[str, Any]:
        captured_at = timestamp(utc_now())
        try:
            runners = self.client.runners()
            runs = self.client.active_runs()
            current_jobs: list[dict[str, Any]] = []
            errors: list[str] = []
            for run in runs:
                try:
                    jobs = self.client.jobs(run["id"])
                except Exception as error:  # A single disappearing run must not hide the board.
                    errors.append(f"{run.get('workflow_name') or run.get('name', 'workflow')}: {format_error(error)}")
                    jobs = []
                active_jobs = [job for job in jobs if job.get("status") in ACTIVE_STATUSES]
                if not active_jobs:
                    active_jobs = [
                        {
                            "id": f"run-{run['id']}",
                            "name": run.get("workflow_name") or run.get("name", "Workflow"),
                            "synthetic": True,
                        }
                    ]
                for job in active_jobs:
                    current_jobs.append(self._normalise_job(run, job))

            if time.monotonic() - self.last_history_refresh >= 600:
                self._start_history_refresh()
            if self.history_error:
                errors.append(f"history: {self.history_error}")

            state = self._build_state(runners, current_jobs, captured_at, errors)
            self.store.save_snapshot(captured_at or "", state)
            self.last_state = state
            return state
        except Exception as error:
            if self.last_state:
                failed = dict(self.last_state)
                failed["collector"] = {
                    **failed.get("collector", {}),
                    "status": "error",
                    "error": format_error(error),
                    "failedAt": captured_at,
                }
                return failed
            return {
                "repository": self.client.repository,
                "capturedAt": captured_at,
                "collector": {"status": "error", "error": format_error(error)},
                "runners": [],
                "jobs": [],
                "lanes": [],
                "history": [],
            }

    def _start_history_refresh(self) -> None:
        with self.history_lock:
            if self.history_refreshing:
                return
            self.history_refreshing = True
            self.last_history_refresh = time.monotonic()
        threading.Thread(target=self._refresh_history_in_background, name="runner-board-history", daemon=True).start()

    def _refresh_history_in_background(self) -> None:
        try:
            self._refresh_history()
            self.history_error = None
        except Exception as error:  # History improves estimates but must not hide live state.
            self.history_error = format_error(error)
        finally:
            with self.history_lock:
                self.history_refreshing = False

    def _refresh_history(self) -> None:
        samples: list[tuple[str, str, str, str, float, str]] = []
        executions: list[dict[str, Any]] = []
        cutoff = timestamp(utc_now() - dt.timedelta(seconds=HISTORY_LOOKBACK_SECONDS)) or ""
        runs = self.client.completed_runs_since(cutoff)
        run_contexts = []
        for run in runs:
            pull_requests = run.get("pull_requests") or []
            pull_request = pull_requests[0] if pull_requests else {}
            pull_request_number = pull_request.get("number")
            run_contexts.append({
                "runId": run.get("id"),
                "headBranch": run.get("head_branch"),
                "event": run.get("event"),
                "pullRequestNumber": pull_request_number,
                "pullRequestUrl": pull_request.get("html_url") or (
                    f"https://github.com/{self.client.repository}/pull/{pull_request_number}"
                    if pull_request_number else None
                ),
                "runDisplayTitle": run.get("display_title"),
            })
        self.store.update_execution_context(run_contexts)
        for run in runs:
            try:
                jobs = self.client.jobs(run["id"])
            except Exception:
                continue
            for job in jobs:
                duration = seconds_between(job.get("started_at"), job.get("completed_at"))
                if not duration or job.get("id") is None:
                    continue
                labels = job.get("runner_labels") or job.get("labels") or []
                lane = lane_from_labels(labels) or "unknown"
                pull_requests = run.get("pull_requests") or []
                pull_request = pull_requests[0] if pull_requests else {}
                pull_request_number = pull_request.get("number")
                execution = {
                    "jobId": job["id"],
                    "runId": run.get("id"),
                    "workflow": run.get("workflow_name") or run.get("name", "Workflow"),
                    "name": job.get("name", "Job"),
                    "runnerId": job.get("runner_id"),
                    "runner": job.get("runner_name"),
                    "labels": labels,
                    "status": job.get("status", "completed"),
                    "conclusion": job.get("conclusion"),
                    "createdAt": job.get("created_at") or run.get("created_at"),
                    "startedAt": job.get("started_at"),
                    "completedAt": job.get("completed_at"),
                    "htmlUrl": job.get("html_url") or run.get("html_url"),
                    "durationSeconds": duration,
                    "headBranch": run.get("head_branch"),
                    "event": run.get("event"),
                    "pullRequestNumber": pull_request_number,
                    "pullRequestUrl": pull_request.get("html_url") or (
                        f"https://github.com/{self.client.repository}/pull/{pull_request_number}"
                        if pull_request_number else None
                    ),
                    "runDisplayTitle": run.get("display_title"),
                }
                executions.append(execution)
                samples.append(
                    (
                        str(job["id"]),
                        run.get("workflow_name") or run.get("name", "Workflow"),
                        job.get("name", "Job"),
                        lane,
                        duration,
                        job.get("completed_at") or timestamp(utc_now()) or "",
                    )
                )
        self.store.add_samples(samples)
        self.store.add_executions(executions)

    def _normalise_job(self, run: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        labels = job.get("runner_labels") or job.get("labels") or []
        lane = lane_from_labels(labels)
        workflow = run.get("workflow_name") or run.get("name") or job.get("workflow_name") or "Workflow"
        status = job.get("status", run.get("status", "queued"))
        started_at = (job.get("started_at") or run.get("run_started_at")) if status == "in_progress" else None
        pull_requests = run.get("pull_requests") or []
        pull_request = pull_requests[0] if pull_requests else {}
        pull_request_number = pull_request.get("number")
        return {
            "id": job.get("id", f"run-{run['id']}"),
            "runId": run["id"],
            "workflow": workflow,
            "name": job.get("name", workflow),
            "status": status,
            "runner": job.get("runner_name") or None,
            "runnerId": job.get("runner_id"),
            "labels": labels,
            "lane": lane,
            "createdAt": job.get("created_at") or run.get("created_at"),
            "startedAt": started_at,
            "completedAt": job.get("completed_at"),
            "conclusion": job.get("conclusion"),
            "htmlUrl": job.get("html_url") or run.get("html_url"),
            "headBranch": run.get("head_branch"),
            "event": run.get("event"),
            "runStatus": run.get("status"),
            "runDisplayTitle": run.get("display_title"),
            "pullRequestNumber": pull_requests[0].get("number") if pull_requests else None,
            "pullRequestUrl": pull_request.get("html_url") or (
                f"https://github.com/{self.client.repository}/pull/{pull_request_number}"
                if pull_request_number else None
            ),
            "synthetic": job.get("synthetic", False),
        }

    def _build_state(
        self,
        runners: list[dict[str, Any]],
        jobs: list[dict[str, Any]],
        captured_at: str | None,
        errors: list[str],
    ) -> dict[str, Any]:
        stats = self.store.duration_stats()
        now = utc_now()
        cutoff = timestamp(now - dt.timedelta(seconds=HISTORY_LOOKBACK_SECONDS)) or ""
        history = self.store.recent_executions(cutoff)
        runner_rows = [
            {
                "id": runner.get("id"),
                "name": runner.get("name"),
                "os": runner.get("os"),
                "status": runner.get("status"),
                "busy": runner.get("busy", False),
                "labels": [label.get("name") if isinstance(label, dict) else label for label in runner.get("labels", [])],
            }
            for runner in runners
        ]
        for job in jobs:
            stat = self._estimate(job, stats)
            if not stat:
                stat = {"p50Seconds": None, "p90Seconds": None, "sampleSize": 0}
            job["queueSeconds"] = seconds_between(job.get("createdAt"), job.get("startedAt")) or 0
            job["elapsedSeconds"] = seconds_between(job.get("startedAt")) if job.get("startedAt") else None
            job["estimate"] = stat

        self._schedule(jobs, runner_rows, now)
        self.store.record_forecasts(jobs, captured_at or timestamp(now) or "")
        lanes = [
            {"id": str(runner["id"]), "label": runner["name"], "runnerId": runner["id"]}
            for runner in runner_rows
        ]
        known_lane_ids = {lane["id"] for lane in lanes}
        has_github_hosted = any(
            not job.get("synthetic") and is_github_hosted_job(job)
            for job in jobs
        ) or any(
            job.get("runnerId") == GITHUB_HOSTED_LANE_ID
            for job in history
        )
        if has_github_hosted:
            lanes.append({"id": GITHUB_HOSTED_LANE_ID, "label": GITHUB_HOSTED_LANE_ID, "runnerId": GITHUB_HOSTED_LANE_ID})
            known_lane_ids.add(GITHUB_HOSTED_LANE_ID)
        for job in history:
            if is_github_hosted_labels(job.get("labels")) or job.get("runnerId") is None:
                continue
            lane_id = str(job["runnerId"])
            if lane_id not in known_lane_ids:
                lanes.append({"id": lane_id, "label": job.get("runner") or lane_id, "runnerId": job["runnerId"], "historicalOnly": True})
                known_lane_ids.add(lane_id)
        return {
            "repository": self.client.repository,
            "capturedAt": captured_at,
            "collector": {
                "status": "degraded" if errors else "ok",
                "error": "; ".join(errors[:3]) if errors else None,
                "intervalSeconds": DEFAULT_INTERVAL_SECONDS,
            },
            "runners": runner_rows,
            "lanes": lanes,
            "jobs": jobs,
            "history": history,
        }

    @staticmethod
    def _estimate(job: dict[str, Any], stats: dict[str, dict[str, float | int]]) -> dict[str, float | int] | None:
        exact = stats.get(BoardStore.key(job["workflow"], job["name"], job.get("lane")))
        generic = stats.get(BoardStore.key(job["workflow"], job["name"], None))
        return exact or generic

    def _schedule(self, jobs: list[dict[str, Any]], runners: list[dict[str, Any]], now: dt.datetime) -> None:
        slots: dict[str, dt.datetime] = {}
        runner_by_id = {str(runner["id"]): runner for runner in runners if runner.get("id") is not None}
        runner_by_name = {runner["name"]: runner for runner in runners if runner.get("name")}
        for runner in runners:
            slots[str(runner["id"])] = now

        visible_busy_runners: set[str] = set()
        for job in jobs:
            runner = runner_by_id.get(str(job["runnerId"])) if job.get("runnerId") is not None else None
            runner = runner or runner_by_name.get(job.get("runner"))
            if runner:
                runner_id = str(runner["id"])
                visible_busy_runners.add(runner_id)
                job["assignedRunnerId"] = runner["id"]
                job["assignedRunner"] = runner["name"]
                start = parse_timestamp(job.get("startedAt")) or now
                duration = job["estimate"].get("p90Seconds") or UNKNOWN_DURATION_SECONDS
                end = max(start + dt.timedelta(seconds=duration), now + dt.timedelta(seconds=60))
                slots[runner_id] = max(slots[runner_id], end or now)
                job["predictedStartAt"] = timestamp(start)
                job["predictedEndAt"] = timestamp(end)
                job["predictionConfidence"] = self._confidence(job)
                continue
            if not job.get("synthetic") and is_github_hosted_job(job):
                start = parse_timestamp(job.get("startedAt")) or now
                duration = job["estimate"].get("p90Seconds") or UNKNOWN_DURATION_SECONDS
                end = max(start + dt.timedelta(seconds=duration), now + dt.timedelta(seconds=60))
                job["assignedRunnerId"] = GITHUB_HOSTED_LANE_ID
                job["assignedRunner"] = GITHUB_HOSTED_LANE_ID
                job["predictedStartAt"] = timestamp(start)
                job["predictedEndAt"] = timestamp(end)
                job["predictionConfidence"] = self._confidence(job)

        busy_without_visible_job = {
            str(runner["id"])
            for runner in runners
            if runner["busy"] and str(runner["id"]) not in visible_busy_runners
        }

        queued = sorted(
            [job for job in jobs if not job.get("runner")], key=lambda job: job.get("createdAt") or ""
        )
        for job in queued:
            if job.get("assignedRunnerId") == GITHUB_HOSTED_LANE_ID:
                continue
            if job.get("synthetic"):
                job["assignedRunner"] = None
                job["predictionConfidence"] = "unknown"
                job["queueReason"] = "Waiting for GitHub to publish job details."
                continue
            candidates = [
                runner
                for runner in runners
                if (
                    runner["status"] == "online"
                    and str(runner["id"]) not in busy_without_visible_job
                    and runner_matches(runner, job.get("labels", []))
                )
            ]
            if not candidates:
                job["assignedRunner"] = None
                job["predictionConfidence"] = "blocked"
                job["blockedReason"] = self._blocked_reason(job, runners, visible_busy_runners)
                continue
            runner = min(candidates, key=lambda candidate: slots[str(candidate["id"])])
            runner_id = str(runner["id"])
            start = slots[runner_id]
            duration = job["estimate"].get("p90Seconds") or UNKNOWN_DURATION_SECONDS
            end = start + dt.timedelta(seconds=duration)
            job["assignedRunnerId"] = runner["id"]
            job["assignedRunner"] = runner["name"]
            job["predictedStartAt"] = timestamp(start)
            job["predictedEndAt"] = timestamp(end)
            job["predictionConfidence"] = self._confidence(job)
            slots[runner_id] = end

    @staticmethod
    def _confidence(job: dict[str, Any]) -> str:
        sample_size = job["estimate"].get("sampleSize", 0)
        return "historical" if sample_size >= 3 else "limited" if sample_size else "unknown"

    @staticmethod
    def _blocked_reason(job: dict[str, Any], runners: list[dict[str, Any]], visible_busy: set[str]) -> str:
        matching = [runner for runner in runners if runner_matches(runner, job.get("labels", []))]
        if any(runner["busy"] and str(runner["id"]) not in visible_busy for runner in matching):
            return "Matching runner is busy; its active job is not visible yet."
        if any(runner["status"] != "online" for runner in matching):
            return "A matching runner is offline."
        return "No runner matches the required labels."


class BoardHandler(http.server.BaseHTTPRequestHandler):
    service: "BoardService"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(self.service.state())
        elif path == "/api/events":
            self.service.stream_events(self)
        elif path == "/api/health":
            self._json({"status": "ok", "capturedAt": self.service.state().get("capturedAt")})
        elif path in ("/", "/index.html"):
            content = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    def _json(self, payload: dict[str, Any]) -> None:
        content = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        return


class BoardService:
    def __init__(self, collector: Collector, interval: int):
        self.collector = collector
        self.interval = max(10, interval)
        self._state: dict[str, Any] = {"collector": {"status": "starting"}, "jobs": [], "runners": [], "lanes": [], "history": []}
        self._lock = threading.Lock()
        self._listeners: set[queue.Queue[dict[str, Any]]] = set()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        listener: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2)
        with self._lock:
            self._listeners.add(listener)
            listener.put_nowait(self._state)
        return listener

    def unsubscribe(self, listener: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def publish(self, state: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener.put_nowait(state)
            except queue.Full:
                try:
                    listener.get_nowait()
                    listener.put_nowait(state)
                except queue.Empty:
                    pass

    def stream_events(self, handler: BoardHandler) -> None:
        listener = self.subscribe()
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "keep-alive")
            handler.send_header("X-Accel-Buffering", "no")
            handler.end_headers()
            while True:
                try:
                    state = listener.get(timeout=25)
                    payload = json.dumps(state, separators=(",", ":"))
                    handler.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                except queue.Empty:
                    handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            self.unsubscribe(listener)

    def collect_forever(self) -> None:
        while True:
            state = self.collector.refresh()
            with self._lock:
                self._state = state
            self.publish(state)
            time.sleep(self.interval)

    def serve(self, host: str, port: int) -> None:
        try:
            server = http.server.ThreadingHTTPServer((host, port), BoardHandler)
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise RuntimeError(
                    f"{host}:{port} is already in use; stop the existing Runner Board process or choose another port"
                ) from error
            raise
        thread = threading.Thread(target=self.collect_forever, name="runner-board-collector", daemon=True)
        thread.start()
        BoardHandler.service = self
        print(f"Runner Board: http://{host}:{port}")
        print(f"Repository: {self.collector.client.repository}; refresh interval: {self.interval}s")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nRunner Board stopped.")
        finally:
            server.server_close()


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GitHub Runner Board"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "github-runner-board"


def install_launch_agent(repo: str, repo_path: Path, data_dir: Path, interval: int, bind_host: str) -> Path:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{LAUNCH_AGENT_LABEL}.plist"
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    arguments = [
        sys.executable,
        str(Path(__file__).resolve()),
        "serve",
        "--repo",
        repo,
        "--host",
        bind_host,
        "--port",
        str(DEFAULT_PORT),
        "--interval",
        str(interval),
        "--data-dir",
        str(data_dir),
    ]
    gh_path = shutil.which("gh")
    tailscale_path = shutil.which("tailscale")
    if not gh_path:
        raise RuntimeError("gh was not found in PATH; install or expose GitHub CLI before installing launchd.")
    path_entries = [str(Path(gh_path).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    if tailscale_path:
        path_entries.insert(1, str(Path(tailscale_path).parent))
    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(repo_path),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "runner-board.out.log"),
        "StandardErrorPath": str(log_dir / "runner-board.err.log"),
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": os.pathsep.join(dict.fromkeys(path_entries))},
    }
    import plistlib

    plist_path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "unload", str(plist_path)], check=False, capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    return plist_path


def uninstall_launch_agent() -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    if not plist_path.exists():
        print("Runner Board launch agent is not installed.")
        return
    subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
    plist_path.unlink()
    print(f"Removed {plist_path}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="collect GitHub data and serve the dashboard")
    serve.add_argument("--repo", help="GitHub repository in owner/name form; defaults to git origin")
    serve.add_argument("--config", type=Path, help="JSON config file")
    serve.add_argument("--host", help="bind address, or 'tailscale' to use the Tailscale IPv4")
    serve.add_argument("--port", type=int)
    serve.add_argument("--interval", type=int)
    serve.add_argument("--data-dir", type=Path)
    install = subparsers.add_parser("install-launchd", help="install and start the macOS background service")
    install.add_argument("--repo", help="GitHub repository in owner/name form; defaults to git origin")
    install.add_argument("--config", type=Path, help="JSON config file")
    install.add_argument("--repo-path", type=Path, help="checkout used for origin detection and launchd working directory")
    install.add_argument("--interval", type=int)
    install.add_argument("--data-dir", type=Path)
    install.add_argument("--bind-host", help="local address for Tailscale Serve to proxy")
    subparsers.add_parser("uninstall-launchd", help="stop and remove the macOS background service")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "uninstall-launchd":
        uninstall_launch_agent()
        return 0
    config_path_value = getattr(args, "config", None) or os.environ.get("RUNNER_BOARD_CONFIG") or default_config_path()
    config = load_config(Path(config_path_value).expanduser())
    repo = configured_value(args.repo, config, "repo", "RUNNER_BOARD_REPO", None)
    repo_path = Path(configured_value(
        getattr(args, "repo_path", None), config, "repo_path", "RUNNER_BOARD_REPO_PATH", Path.cwd()
    )).expanduser()
    repo = repo or detect_repository(repo_path)
    interval = configured_int(args.interval, config, "interval", "RUNNER_BOARD_INTERVAL", DEFAULT_INTERVAL_SECONDS)
    data_dir = Path(configured_value(args.data_dir, config, "data_dir", "RUNNER_BOARD_DATA_DIR", default_data_dir())).expanduser()
    if args.command == "install-launchd":
        bind_host = configured_value(args.bind_host, config, "bind_host", "RUNNER_BOARD_BIND_HOST", DEFAULT_HOST)
        path = install_launch_agent(repo, repo_path.resolve(), data_dir, interval, bind_host)
        print(f"Runner Board launch agent installed at {path}")
        print("Optionally run `tailscale serve --bg --https=8443 http://127.0.0.1:8765`, then open the MagicDNS hostname on port 8443.")
        return 0
    host = configured_value(args.host, config, "host", "RUNNER_BOARD_HOST", DEFAULT_HOST)
    port = configured_int(args.port, config, "port", "RUNNER_BOARD_PORT", DEFAULT_PORT)
    host = tailscale_address() if host == "tailscale" else host
    service = BoardService(Collector(GitHubClient(repo), BoardStore(data_dir)), interval)
    try:
        service.serve(host, port)
    except RuntimeError as error:
        print(f"runner-board: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
