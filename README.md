# GitHub Runner Board

Read-only, mobile-friendly visibility for GitHub Actions jobs and self-hosted runners.

Runner Board shows which runners are online or busy, what is queued, and an estimated timeline
for the currently planned work. It keeps collecting when no browser is open and stores only local
duration history for estimates. It does not dispatch, cancel, reorder or modify workflows.

## Quick start

Authenticate the GitHub CLI, then install the tool:

```bash
gh auth login
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --editable .
```

Run it for a repository:

```bash
runner-board serve --repo owner/repository
```

When run from an existing GitHub checkout, the repository can be detected from its origin:

```bash
runner-board serve
```

Open `http://127.0.0.1:8765`. GitHub credentials stay on the host; the browser receives only the
read-only board state.

If port `8765` is already in use, either reuse the existing board at that address or choose another
port, for example `runner-board serve --repo owner/repository --port 8766`.

## Configuration

Command-line options take precedence over environment variables, which take precedence over the
optional JSON config file at `~/.config/github-runner-board/config.json` (or
`$XDG_CONFIG_HOME/github-runner-board/config.json`). Use `--config` or
`RUNNER_BOARD_CONFIG` to select another file.

```json
{
  "repo": "owner/repository",
  "host": "127.0.0.1",
  "port": 8765,
  "interval": 30,
  "data_dir": "~/.local/state/github-runner-board"
}
```

The equivalent environment variables are `RUNNER_BOARD_REPO`, `RUNNER_BOARD_HOST`,
`RUNNER_BOARD_PORT`, `RUNNER_BOARD_INTERVAL`, `RUNNER_BOARD_DATA_DIR`, `RUNNER_BOARD_REPO_PATH`,
and `RUNNER_BOARD_BIND_HOST` for the launchd command.

## Background service

On macOS, install a per-user launch agent:

```bash
runner-board install-launchd --repo owner/repository --repo-path /path/to/checkout
```

The collector polls GitHub independently of the browser. Tailscale Serve can expose the local
service privately to a phone:

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8765
```

Other service managers can run the equivalent `runner-board serve` command.

## What it shows

- online, busy and offline self-hosted runners;
- active, queued, pending and waiting jobs;
- queued, active and completed-action sections;
- per-runner timeline with a live current-time marker, a 5-minute default grid, round zoom ranges from 1 minute to 6 hours, and a scrollable 24-hour history window;
- one consolidated `github-hosted` timeline row for GitHub-hosted workflows;
- completed jobs mapped to runner-name rows for the previous 24 hours;
- branch and pull-request context with GitHub links;
- p50/p90 duration estimates from recent completed jobs;
- stale-data warnings when the collector cannot refresh.

GitHub does not expose a guaranteed queue order. Start and finish times are therefore forecasts,
not promises. Jobs that have not yet received their runner labels remain unassigned rather than
being reported as a false runner mismatch. Historical timeline display is intentionally limited
to the previous 24 hours; older executions remain available locally for duration estimates.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile github_runner_board/__init__.py
```

The first release supports one GitHub.com repository at a time and uses the authenticated `gh` CLI.
The board remains read-only: it does not dispatch, cancel, reorder, or modify workflows.

## Licence

MIT. See [LICENSE](LICENSE).
