<!-- LOC cap: 80 (created: 2026-04-24) -->

# AGENTS.md — dashy

Local dev service control plane. Stdlib Python BFF (`server.py`) at port `7800` + single-file
vanilla JS dashboard (`dashboard.html`). Auto-discovers services via `devdash.json` contract files
committed to each project/worktree root.

## Rules

- Never commit or push unprompted.
- Always use `YYYY-MM-DD` date format.
- stdlib Python only — no third-party packages, no build step.
- `server.py` runs as the `ubuntu` user (systemd service); write code accordingly.
- Never paste live secrets, credentials, or PII into any file.
- No env handling in `server.py` — start commands in `devdash.json` are self-contained.

## Layout

```
server.py              stdlib HTTP server — port 7800, JSON API, serves dashboard.html
dashboard.html         single-file UI — vanilla JS + CSS, no build step
config.json            scan_roots, scan_max_depth, scan_exclude, scan_interval_sec, port
dashy.service             systemd unit (User=ubuntu, Restart=on-failure)
install.sh             copies unit → /etc/systemd/system/, daemon-reload, enable, start
.docs/
  features/open/
    ROADMAP.md         phased implementation plan
```

## `devdash.json` contract (what agents create in other repos)

```json
{
  "project": "my-project",
  "worktree": "feat-name",
  "services": [
    {
      "id": "my-project-feat-name-web",
      "name": "Web · feat-name",
      "port": 5176,
      "pid_file": "/abs/path/to/.pids/web-feat-name.pid",
      "start_cmd": "MYPORT=5176 pnpm run dev",
      "stop_cmd": null,
      "cwd": "/abs/path/to/worktree/apps/web"
    }
  ]
}
```

Rules:

- `id` must be globally unique — use `<project>-<worktree>-<role>` convention.
- `stop_cmd: null` → dashy kills via `pid_file` (SIGTERM → 5s → SIGKILL).
- `start_cmd` is a fully self-contained shell string; scripts source their own `.env`.
- `worktree` omit or `null` for the main branch of a project.
- `pid_file` may not exist yet (stopped state).

## Adding a project to the dashboard

1. Commit `devdash.json` to the project/worktree root.
2. Ensure the project's dir is under a `scan_root` in `config.json` (or add it via `POST /api/config/scan_roots`).
3. Dashboard auto-discovers the file within `scan_interval_sec` — no registration call needed.

## Running locally (without systemd)

```bash
cd /home/ubuntu/server/dashy
python3 server.py
# dashboard at http://localhost:7800
```

## Installing as a systemd service

```bash
sudo bash /home/ubuntu/server/dashy/install.sh
systemctl status dashy
```

## API surface

| Method | Path                         | Purpose                                             |
| ------ | ---------------------------- | --------------------------------------------------- |
| GET    | `/api/services`              | Full service registry with live status              |
| POST   | `/api/services/{id}/start`   | Start a service                                     |
| POST   | `/api/services/{id}/stop`    | Stop a service                                      |
| POST   | `/api/services/{id}/restart` | Restart a service                                   |
| GET    | `/api/services/{id}/log`     | Last 200 lines of start/stop output                 |
| GET    | `/api/events`                | SSE — pushes `services.updated` every scan interval |
| GET    | `/api/config`                | Return parsed config.json                           |
| POST   | `/api/config/scan_roots`     | Add/remove a scan root at runtime                   |
| GET    | `/api/fs/dirs?q=`            | Path autocomplete (max 20 results)                  |

## Verification

```bash
curl http://localhost:7800/                        # 200 HTML
curl http://localhost:7800/api/services | jq '.'   # all discovered services
curl -X POST http://localhost:7800/api/services/yact-web-main/stop
curl http://localhost:7800/api/services | jq '.[] | select(.id=="yact-web-main") | .status'
```

## Docs — read when

| File                             | Read when…                                                           |
| -------------------------------- | -------------------------------------------------------------------- |
| `.docs/features/open/ROADMAP.md` | implementing any phase, checking LoC budgets, or reviewing decisions |

## Testing

No automated suite. Manual verification steps are in each phase's "Exit" criterion in ROADMAP.md.
Run `python3 -m py_compile server.py && echo "syntax OK"` after any server change.
