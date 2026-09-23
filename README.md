# GitHub Runner Board

Read-only, mobile-friendly visibility for GitHub Actions jobs and self-hosted runners.

Runner Board shows which runners are online or busy, what is queued, and an estimated timeline
for the currently planned work. It keeps collecting when no browser is open and stores only local
duration history for estimates. It does not dispatch, cancel, reorder or modify workflows.

## Quick start

Authenticate the GitHub CLI, then install the tool:

```bash
gh auth login
python3 -m pip install .
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
- per-runner timeline with a live current-time marker;
- branch and pull-request context with GitHub links;
- p50/p90 duration estimates from recent completed jobs;
- stale-data warnings when the collector cannot refresh.

GitHub does not expose a guaranteed queue order. Start and finish times are therefore forecasts,
not promises. Jobs that have not yet received their runner labels remain unassigned rather than
being reported as a false runner mismatch.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile github_runner_board/__init__.py
```

The first release intentionally supports one GitHub repository at a time and uses the authenticated
`gh` CLI. Planned follow-ups include configuration files, multiple repositories, GitHub App
authentication, organisation runner scope, webhook-assisted updates, metrics, and packaged
releases.

## Licence

MIT. See [LICENSE](LICENSE).
