#!/usr/bin/env python3
"""Serve a private, read-only GitHub Actions runner board."""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import json
import math
import os
from pathlib import Path
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
UNKNOWN_DURATION_SECONDS = 15 * 60
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


def format_error(error: Exception) -> str:
    message = str(error).strip().splitlines()[-1] if str(error).strip() else error.__class__.__name__
    return message[:240]


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
    for label in labels:
        if label.startswith("mini-"):
            return label
    for label in labels:
        if label in ("linux", "macos", "windows"):
            return label
    return None


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

    def api(self, path: str) -> dict[str, Any]:
        result = subprocess.run(
            ["gh", "api", f"repos/{self.repository}/{path}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def runners(self) -> list[dict[str, Any]]:
        return self.api("actions/runners?per_page=100").get("runners", [])

    def active_runs(self) -> list[dict[str, Any]]:
        runs: dict[int, dict[str, Any]] = {}
        for status in ACTIVE_STATUSES:
            for run in self.api(f"actions/runs?status={status}&per_page=100").get("workflow_runs", []):
                runs[run["id"]] = run
        return sorted(runs.values(), key=lambda run: run.get("created_at", ""))

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        return self.api(f"actions/runs/{run_id}/jobs?per_page=100&filter=latest").get("jobs", [])

    def completed_runs(self, limit: int) -> list[dict[str, Any]]:
        return self.api(f"actions/runs?status=completed&per_page={limit}").get("workflow_runs", [])


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
        return {
            key: {
                "p50Seconds": round(statistics.median(values)),
                "p90Seconds": round(quantile(values, 0.9)),
                "sampleSize": len(values),
            }
            for key, values in grouped.items()
        }

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
        for run in self.client.completed_runs(self.history_runs):
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

    def _normalise_job(self, run: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        labels = job.get("runner_labels") or job.get("labels") or []
        lane = lane_from_labels(labels)
        workflow = run.get("workflow_name") or run.get("name") or job.get("workflow_name") or "Workflow"
        status = job.get("status", run.get("status", "queued"))
        started_at = (job.get("started_at") or run.get("run_started_at")) if status == "in_progress" else None
        pull_requests = run.get("pull_requests") or []
        return {
            "id": job.get("id", f"run-{run['id']}"),
            "runId": run["id"],
            "workflow": workflow,
            "name": job.get("name", workflow),
            "status": status,
            "runner": job.get("runner_name") or None,
            "labels": labels,
            "lane": lane,
            "createdAt": job.get("created_at") or run.get("created_at"),
            "startedAt": started_at,
            "completedAt": job.get("completed_at"),
            "htmlUrl": job.get("html_url") or run.get("html_url"),
            "headBranch": run.get("head_branch"),
            "event": run.get("event"),
            "runStatus": run.get("status"),
            "runDisplayTitle": run.get("display_title"),
            "pullRequestNumber": pull_requests[0].get("number") if pull_requests else None,
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
        runner_rows = [
            {
                "id": runner.get("id"),
                "name": runner.get("name"),
                "os": runner.get("os"),
                "status": runner.get("status"),
                "busy": runner.get("busy", False),
                "labels": [label.get("name") for label in runner.get("labels", [])],
            }
            for runner in runners
        ]
        for job in jobs:
            key = BoardStore.key(job["workflow"], job["name"], job.get("lane"))
            stat = stats.get(key) or stats.get(BoardStore.key(job["workflow"], job["name"], None))
            if not stat:
                stat = {"p50Seconds": None, "p90Seconds": None, "sampleSize": 0}
            job["queueSeconds"] = seconds_between(job.get("createdAt"), job.get("startedAt")) or 0
            job["elapsedSeconds"] = seconds_between(job.get("startedAt")) if job.get("startedAt") else None
            job["estimate"] = stat

        self._schedule(jobs, runner_rows, now)
        lanes = [{"id": runner["name"], "label": runner["name"], "runnerId": runner["id"]} for runner in runner_rows]
        if any(job.get("assignedRunner") is None for job in jobs):
            lanes.append({"id": "unassigned", "label": "Unassigned queue", "runnerId": None})
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
        }

    def _schedule(self, jobs: list[dict[str, Any]], runners: list[dict[str, Any]], now: dt.datetime) -> None:
        slots: dict[str, dt.datetime] = {}
        runner_by_name = {runner["name"]: runner for runner in runners}
        for runner in runners:
            slots[runner["name"]] = now if runner["status"] == "online" and not runner["busy"] else now

        visible_busy_runners: set[str] = set()
        for job in jobs:
            if job.get("runner") in runner_by_name:
                runner_name = job["runner"]
                visible_busy_runners.add(runner_name)
                job["assignedRunner"] = runner_name
                start = parse_timestamp(job.get("startedAt")) or now
                duration = job["estimate"].get("p90Seconds") or UNKNOWN_DURATION_SECONDS
                end = max(start + dt.timedelta(seconds=duration), now + dt.timedelta(seconds=60))
                slots[runner_name] = max(slots[runner_name], end or now)
                job["predictedStartAt"] = timestamp(start)
                job["predictedEndAt"] = timestamp(end)
                job["predictionConfidence"] = self._confidence(job)

        busy_without_visible_job = {
            name for name, runner in runner_by_name.items() if runner["busy"] and name not in visible_busy_runners
        }

        queued = sorted(
            [job for job in jobs if not job.get("runner")], key=lambda job: job.get("createdAt") or ""
        )
        for job in queued:
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
                    and runner["name"] not in busy_without_visible_job
                    and runner_matches(runner, job.get("labels", []))
                )
            ]
            if not candidates:
                job["assignedRunner"] = None
                job["predictionConfidence"] = "blocked"
                job["blockedReason"] = self._blocked_reason(job, runners, visible_busy_runners)
                continue
            runner = min(candidates, key=lambda candidate: slots[candidate["name"]])
            start = slots[runner["name"]]
            duration = job["estimate"].get("p90Seconds") or UNKNOWN_DURATION_SECONDS
            end = start + dt.timedelta(seconds=duration)
            job["assignedRunner"] = runner["name"]
            job["predictedStartAt"] = timestamp(start)
            job["predictedEndAt"] = timestamp(end)
            job["predictionConfidence"] = self._confidence(job)
            slots[runner["name"]] = end

    @staticmethod
    def _confidence(job: dict[str, Any]) -> str:
        sample_size = job["estimate"].get("sampleSize", 0)
        return "historical" if sample_size >= 3 else "limited" if sample_size else "unknown"

    @staticmethod
    def _blocked_reason(job: dict[str, Any], runners: list[dict[str, Any]], visible_busy: set[str]) -> str:
        matching = [runner for runner in runners if runner_matches(runner, job.get("labels", []))]
        if any(runner["busy"] and runner["name"] not in visible_busy for runner in matching):
            return "Matching runner is busy; its active job is not visible yet."
        if any(runner["status"] != "online" for runner in matching):
            return "A matching runner is offline."
        return "No runner matches the required labels."


class BoardHandler(http.server.BaseHTTPRequestHandler):
    service: "BoardService"

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(self.service.state())
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
        self._state: dict[str, Any] = {"collector": {"status": "starting"}, "jobs": [], "runners": [], "lanes": []}
        self._lock = threading.Lock()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state

    def collect_forever(self) -> None:
        while True:
            state = self.collector.refresh()
            with self._lock:
                self._state = state
            time.sleep(self.interval)

    def serve(self, host: str, port: int) -> None:
        thread = threading.Thread(target=self.collect_forever, name="runner-board-collector", daemon=True)
        thread.start()
        server = http.server.ThreadingHTTPServer((host, port), BoardHandler)
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
    serve.add_argument("--host", default="127.0.0.1", help="bind address, or 'tailscale' to use the Tailscale IPv4")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    serve.add_argument("--data-dir", type=Path, default=default_data_dir())
    install = subparsers.add_parser("install-launchd", help="install and start the macOS background service")
    install.add_argument("--repo", help="GitHub repository in owner/name form; defaults to git origin")
    install.add_argument("--repo-path", type=Path, default=Path.cwd())
    install.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    install.add_argument("--data-dir", type=Path, default=default_data_dir())
    install.add_argument("--bind-host", default="127.0.0.1", help="local address for Tailscale Serve to proxy")
    subparsers.add_parser("uninstall-launchd", help="stop and remove the macOS background service")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "uninstall-launchd":
        uninstall_launch_agent()
        return 0
    repo = args.repo or detect_repository(args.repo_path if hasattr(args, "repo_path") else Path.cwd())
    if args.command == "install-launchd":
        path = install_launch_agent(repo, args.repo_path.resolve(), args.data_dir, args.interval, args.bind_host)
        print(f"Runner Board launch agent installed at {path}")
        print("Optionally run `tailscale serve --bg --https=8443 http://127.0.0.1:8765`, then open the MagicDNS hostname on port 8443.")
        return 0
    host = tailscale_address() if args.host == "tailscale" else args.host
    service = BoardService(Collector(GitHubClient(repo), BoardStore(args.data_dir)), args.interval)
    service.serve(host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
