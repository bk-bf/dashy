# dashy — ROADMAP

> **Related:** [`/home/ubuntu/server/dashy/`](../../..)

Local dev service control plane. A stdlib Python server + single-file HTML dashboard at port `7800`
that auto-discovers services across dev projects via committed `devdash.json` contract files, and
exposes a unified start/stop/status API usable by agents, scripts, and the browser UI.

---

## Phases

| Phase | Title                          | Status     | LoC budget |
| ----- | ------------------------------ | ---------- | ---------- |
| 1     | Scaffold                       | ✅ done    | ~50        |
| 2     | Discovery engine               | ✅ done    | ~150       |
| 3     | Status engine                  | ✅ done    | ~100       |
| 4     | Actions API                    | ✅ done    | ~120       |
| 5     | Dashboard UI                   | ✅ done    | ~800       |
| 6     | Migration — devdash.json files | ⏳ pending | —          |
| 7     | Systemd install                | ⏳ pending | ~40        |

---

## Phase 1 — Scaffold

Create `/home/ubuntu/server/dashy/` with these files:

```
server.py          stdlib HTTP server, port 7800, serves dashboard.html + JSON API
dashboard.html     single-file vanilla JS + CSS UI (no build step)
config.json        scan roots, depth, excludes, port
dev-dashboard.service  systemd unit
install.sh         registers + starts the systemd unit
```

**`config.json` initial content:**

```json
{
  "port": 7800,
  "scan_roots": ["/home/ubuntu/server/yact", "/home/ubuntu/server/openswarm"],
  "scan_max_depth": 4,
  "scan_exclude": [
    "node_modules",
    ".venv",
    "build",
    "dist",
    "__pycache__",
    ".git"
  ],
  "scan_interval_sec": 10
}
```

Exit: `python3 server.py` starts without error; `curl http://localhost:7800/` returns 200.

---

## Phase 2 — Discovery engine

Implement in `server.py`:

- `scan_for_devdash(root, max_depth, excludes)` — `os.walk` with depth limit and exclude pruning; returns list of `devdash.json` absolute paths.
- `load_manifest(path)` — parse + validate `devdash.json`; returns list of service dicts with `_source_file` metadata attached.
- `refresh_services()` — background thread running every `scan_interval_sec`; re-scans all roots, merges all manifests into an in-memory service registry dict keyed by `id`.
- `GET /api/services` — serialise full registry as JSON array.
- `GET /api/config` — return parsed `config.json`.
- `POST /api/config/scan_roots` — add/remove a scan root at runtime (persists to `config.json`).

### `devdash.json` schema (the contract)

Committed to every project and worktree root. Agents create it; the dashboard picks it up automatically on the next scan.

```json
{
  "project": "yact-web",
  "worktree": "quant-pipeline",
  "services": [
    {
      "id": "yact-web-quant-pipeline",
      "name": "Web · quant-pipeline",
      "port": 5176,
      "pid_file": "/home/ubuntu/server/yact/.pids/web-quant-pipeline.pid",
      "start_cmd": "YACT_WEB_PORT=5176 YACT_ANALYTICS_URL=http://localhost:8001 pnpm run dev:web",
      "stop_cmd": null,
      "cwd": "/home/ubuntu/server/yact/yact-web/features/quant-pipeline/apps/web"
    }
  ]
}
```

Field rules:

- `id` — globally unique across all projects; use `<project>-<worktree>-<role>` convention.
- `stop_cmd: null` — dashboard kills via `pid_file` directly (SIGTERM → 5s → SIGKILL).
- `start_cmd` — fully self-contained shell string; no env injection by dashboard.
- `worktree` — omit or `null` for the main branch of a project.
- `pid_file` — absolute path; may not exist yet (service stopped state).
- Future extension: `"type": "docker"` reserved; not implemented in this phase.

Exit: `GET /api/services` returns JSON array containing all services from all `devdash.json` files found under `scan_roots`.

---

## Phase 3 — Status engine

Implement service liveness checks merged into the registry on every refresh:

- `check_pid(pid_file)` → `{alive: bool, pid: int|None}` via `os.kill(pid, 0)` (no-op signal, pure existence check; catches `ProcessLookupError` and `PermissionError`).
- `check_port(port)` → `{bound: bool}` via subprocess `ss -HtlnO "sport = :N"` (sees all processes regardless of owner).
- `get_uptime(pid)` → seconds since process start, derived from `/proc/<pid>/stat` field 22 (starttime) + `/proc/uptime` for boot epoch.

**Status resolution matrix:**

| pid alive                     | port bound | status     |
| ----------------------------- | ---------- | ---------- |
| yes                           | yes        | `running`  |
| yes                           | no         | `starting` |
| no                            | no         | `stopped`  |
| no                            | yes        | `zombie`   |
| pid_file exists, process dead | —          | `error`    |

Merged into each service object in the registry:

```json
{
  "id": "yact-web-quant-pipeline",
  "status": "running",
  "pid": 12345,
  "uptime_sec": 3820,
  "port_bound": true
}
```

Exit: `GET /api/services` returns status fields; running yact and openswarm services show `"status": "running"`.

---

## Phase 4 — Actions API

- `POST /api/services/{id}/start` — `subprocess.Popen(start_cmd, shell=True, cwd=cwd, stdout=PIPE, stderr=STDOUT)` in a daemon thread; captures output into a rotating per-service ring buffer (max 200 lines, in-memory only); returns `{ok: true, message: "started"}` immediately.
- `POST /api/services/{id}/stop` — if `stop_cmd` is set: run it with 15s timeout; else `os.kill(pid, SIGTERM)` then after 5s `os.kill(pid, SIGKILL)` if still alive; returns `{ok: true|false, message: "..."}`.
- `POST /api/services/{id}/restart` — stop then start sequentially.
- `GET /api/services/{id}/log` — return `{lines: [...]}` from ring buffer.
- `GET /api/events` (SSE) — server pushes a full `services` payload diff every `scan_interval_sec`; event type `services.updated`; JS falls back to 5s polling if SSE fails.

All action endpoints run the subprocess as the current process user (systemd unit runs as `ubuntu`, so no sudo needed).

Exit: stop a running yact feature dev server via `curl -X POST localhost:7800/api/services/yact-web-quant-pipeline/stop` → status changes to `stopped` within scan interval; start it again → returns to `running`.

---

## Phase 5 — Dashboard UI

Single `dashboard.html` — no build step, plain HTML + vanilla JS.

### Visual style

CSS custom properties copied verbatim from `openswarm/features/ux-fixes/dashboard.html`:

```css
:root {
  --bg: #0d0d0f;
  --surface: #16181d;
  --border: #26292f;
  --muted: #4a4f5a;
  --text: #d4d8e2;
  --dim: #7a8090;
  --green: #3dd68c;
  --green-bg: #0e2a1c;
  --blue: #4f9cf9;
  --blue-bg: #0d1e38;
  --amber: #f0a050;
  --amber-bg: #2a1d08;
  --red: #f46060;
  --red-bg: #2a0e0e;
  --pulse-col: #4f9cf933;
}
font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace;
font-size: 13px;
```

### Layout

```
#shell (flex column, 100vh)
  header
    .logo            "dev[dash]"  (<span> blue highlight on "dash")
    .header-meta
      project filter dropdown   (one entry per unique `project` value + "All")
      refresh interval selector (5s / 30s / off)
      live dot                  (green = SSE connected, amber = polling, red = disconnected)
  #content (scrollable)
    per-project section (hidden if filter active and not selected)
      .section-header  project label + worktree count + running count
      .service-table
        thead: Name | Port | Status | Worktree | Uptime | Actions
        tbody: one row per service
          status badge  (colour-coded: running=green, starting=amber, stopped=muted, zombie/error=red)
          ▶ start / ■ stop / ↺ restart buttons (opacity:0, reveal on row :hover)
  #log-panel (draggable overlay from bottom, same pattern as openswarm orch-panel)
    triggered by clicking any service row
    shows GET /api/services/{id}/log output, auto-scrolls
  #confirm-modal
    "Stop <service name>?" with Cancel / Confirm buttons
    shown before any stop or restart action
  #add-root-drawer (slide-in from right, 340px)
    path input with directory autocomplete (GET /api/fs/dirs?q=)
    add / cancel buttons
```

Exit: visual parity with openswarm legacy dashboard style; all services visible; start/stop/restart functional; log panel opens on row click; confirm modal fires before destructive actions; project filter hides/shows project sections.

---

## Phase 6 — Migration: create `devdash.json` files

Create one `devdash.json` per project/worktree. Start commands are self-contained and mirror what the current start scripts do.

### yact — main services (systemd-managed)

`/home/ubuntu/server/yact/yact-server/devdash.json`

Services: `yact-server-api` (port 8000) and `yact-server-miner` — both controlled via `sudo systemctl start/stop yact-coindata-api yact-coindata-miner`. Since systemd manages PIDs, use `start_cmd` wrapping `systemctl`; `pid_file: null`; `stop_cmd` uses `systemctl stop`.

`/home/ubuntu/server/yact/yact-web/devdash.json`

Service: `yact-web-main` (port 5175). `start_cmd` mirrors `start-main.sh` launch args. `pid_file: "/home/ubuntu/server/yact/.pids/web-main.pid"`.

### yact — feature worktrees

Each at `<worktree>/devdash.json`. Ports and pid_file names from `ports.json` and `.pids/` naming convention.

| File                                                         | Services                                                             | Ports             |
| ------------------------------------------------------------ | -------------------------------------------------------------------- | ----------------- |
| `yact-server/features/quant-pipeline/devdash.json`           | `yact-server-quant-pipeline-api`, `yact-server-quant-pipeline-miner` | 8001              |
| `yact-server/features/t-212-btc-dominance/devdash.json`      | `yact-server-t-212-api`, `yact-server-t-212-miner`                   | (from ports.json) |
| `yact-web/features/quant-pipeline/devdash.json`              | `yact-web-quant-pipeline`                                            | 5176              |
| `yact-web/features/t-210-surface-oi/devdash.json`            | `yact-web-t-210`                                                     | (from ports.json) |
| `yact-web/features/t-211-surface-funding-rates/devdash.json` | `yact-web-t-211`                                                     | (from ports.json) |
| `yact-web/features/t-212-btc-dominance/devdash.json`         | `yact-web-t-212`                                                     | (from ports.json) |

### openswarm

`/home/ubuntu/server/openswarm/devdash.json`

Services: `openswarm-dashboard` (port 7700, systemd) and `openswarm-dashboard-dev` (port 7701, pid-file at `.pids/dashboard-dev.pid`).

Exit: `curl localhost:7800/api/services | jq 'length'` → ≥ 9; all running services show `"status": "running"`.

---

## Phase 7 — Systemd install

**`dev-dashboard.service`:**

```ini
[Unit]
Description=dev-dashboard service control plane
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/server/dashy
ExecStart=python3 /home/ubuntu/server/dashy/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**`install.sh`:**

1. `sudo cp dev-dashboard.service /etc/systemd/system/`
2. `sudo systemctl daemon-reload`
3. `sudo systemctl enable dev-dashboard`
4. `sudo systemctl start dev-dashboard`
5. Polls `curl http://localhost:7800/` up to 10s; prints pass/fail.

User runs: `sudo bash /home/ubuntu/server/dashy/install.sh`

Exit: `systemctl status dev-dashboard` shows `active (running)`; service survives reboot.

---

## Verification (full stack)

1. `curl http://localhost:7800/` → 200 HTML
2. `curl http://localhost:7800/api/services | jq '[.[] | .status]'` → all running services show `"running"`
3. Stop a yact feature dev server via UI → status badge changes to `stopped` within 10s; no page refresh needed
4. Start it → returns to `running`
5. Drop a new `devdash.json` under any subdir of a scan root → service appears in next refresh without server restart
6. `systemctl status dev-dashboard` → `active (running)`
7. `sudo bash /home/ubuntu/server/dashy/install.sh` exits 0

---

## Decisions

| Decision                    | Choice                                         | Rationale                                                                           |
| --------------------------- | ---------------------------------------------- | ----------------------------------------------------------------------------------- |
| Contract format             | File (`devdash.json`)                          | Works when service is dead; committed to branch, flows into worktrees automatically |
| Env/secrets                 | Not in contract; `start_cmd` is self-contained | Secrets stay in project `.env`; dashboard never touches them                        |
| Registration                | Auto-discovery via scan_roots                  | Agent commits the file; no registration call needed                                 |
| New worktree agent workflow | Create `devdash.json` in worktree root         | Dashboard picks it up within `scan_interval_sec`; zero extra steps                  |
| yact `start-features.sh`    | Not modified                                   | `devdash.json` start_cmd is per-service, bypasses all-or-nothing script             |
| Process user                | Systemd unit runs as `ubuntu`                  | `start_cmd` executes as ubuntu directly; no sudo wrapper                            |
| Frontend stack              | Stdlib Python + single-file HTML               | No build step; same pattern as openswarm legacy dashboard                           |
| Port                        | 7800                                           | Clear of all existing services (yact: 8000–8001, openswarm: 7700–7701)              |
| Docker support              | Deferred                                       | `"type": "docker"` field reserved in schema; not implemented                        |
