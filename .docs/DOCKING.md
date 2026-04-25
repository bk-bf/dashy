# Docking a project into dashy

dashy auto-discovers services via a `dashy.json` file committed to each project or
worktree root. No registration call, no dashy restart — it picks the file up within
`scan_interval_sec` (default 10s).

---

## Process isolation — dashy is not a single point of failure

dashy is a **read-only observer**. It never owns your processes.

- **If dashy crashes or restarts**, all running services continue unaffected. Their
  processes keep running, pid files stay on disk, and dashy reads the correct state
  again on the next scan after it comes back up.
- **If dashy is down**, services are completely unreachable from the dashboard but
  otherwise unaffected. There is no crash cascade.
- dashy reads pid files and polls ports to determine status. It does not inject itself
  into process groups or hold file locks.
- The only thing lost on a dashy restart is the **in-memory log ring buffer** (last
  200 lines of output from processes dashy started). The processes themselves keep
  running.

### One caveat: foreground processes started through the dashboard

When you click ▶ Start, dashy calls `Popen(start_cmd, shell=True)`. The process
becomes a child of dashy's process tree. If dashy is killed with SIGKILL, the OS
may send SIGHUP to any foreground child that hasn't detached.

**To fully decouple your service from dashy's lifecycle, ensure `start_cmd` does one
of the following:**

- Backgrounds itself and writes a pid file (`my-server & echo $! > .pids/server.pid`)
- Uses `nohup` or `setsid` to detach from the terminal session
- Is managed by systemd (in which case `start_cmd` is just `systemctl start …` and
  dashy never owns the process at all)

A `start_cmd` that runs a foreground process (e.g. bare `pnpm dev`) without
backgrounding is safe as long as dashy is only ever stopped gracefully (SIGTERM),
which is the default for `systemctl stop dashy`. SIGKILL should never be needed
in normal operation.

---

## 1. Check the docking info endpoint

Before writing any files, fetch dashy's current config so you know where to place
things and what scan roots are already registered:

```bash
curl -s http://localhost:7800/api/dock | jq '.'
```

Key fields in the response:

| Field | Meaning |
|---|---|
| `scan_roots` | Directories dashy watches. Your project must be under one of these. |
| `scan_max_depth` | How deep under each root dashy walks. |
| `scan_interval_sec` | How long until dashy picks up a new file (max wait). |

If your project root is **not** under any `scan_root`, add it:

```bash
curl -s -X POST http://localhost:7800/api/config/scan_roots \
  -H 'Content-Type: application/json' \
  -d '{"action":"add","root":"/home/ubuntu/server/my-project"}'
```

---

## 2. Create `dashy.json` in the project/worktree root

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
      "start_cmd": "MY_PORT=5176 pnpm run dev",
      "stop_cmd": null,
      "cwd": "/abs/path/to/worktree/apps/web"
    }
  ]
}
```

### Field rules

| Field | Required | Notes |
|---|---|---|
| `project` | yes | Shared across all worktrees of the same project. Used for grouping in the UI. |
| `worktree` | no | Omit or `null` for the main branch. |
| `services[].id` | yes | **Globally unique** across all projects. Convention: `<project>-<worktree>-<role>`. For main: `<project>-<role>`. |
| `services[].name` | yes | Human-readable label shown in the dashboard. |
| `services[].start_cmd` | yes | Fully self-contained shell string. Dashy runs it with `shell=True` as the current process user (`ubuntu`). Scripts must source their own `.env`. |
| `services[].cwd` | yes | Absolute path. Working directory for `start_cmd`. |
| `services[].port` | no | Used for liveness checks. Omit if the service doesn't bind a port. |
| `services[].pid_file` | no | Absolute path. Written by `start_cmd`; may not exist yet (stopped state). Required if `stop_cmd` is null. |
| `services[].stop_cmd` | no | Shell string. `null` → dashy kills via `pid_file` (SIGTERM → 5 s → SIGKILL). |

### Multi-service example (API + worker pair)

```json
{
  "project": "my-project",
  "worktree": null,
  "services": [
    {
      "id": "my-project-api",
      "name": "API",
      "port": 8000,
      "pid_file": "/home/ubuntu/server/my-project/.pids/api.pid",
      "start_cmd": "source .env && ./scripts/run-api.sh",
      "stop_cmd": null,
      "cwd": "/home/ubuntu/server/my-project"
    },
    {
      "id": "my-project-worker",
      "name": "Worker",
      "port": null,
      "pid_file": "/home/ubuntu/server/my-project/.pids/worker.pid",
      "start_cmd": "source .env && ./scripts/run-worker.sh",
      "stop_cmd": null,
      "cwd": "/home/ubuntu/server/my-project"
    }
  ]
}
```

### systemd-managed service (no pid_file)

When systemd owns the process, use `stop_cmd` instead:

```json
{
  "id": "my-project-api",
  "name": "API",
  "port": 8000,
  "pid_file": null,
  "start_cmd": "sudo systemctl start my-project-api",
  "stop_cmd": "sudo systemctl stop my-project-api",
  "cwd": "/home/ubuntu/server/my-project"
}
```

---

## 3. Validate before committing (optional but recommended)

```bash
curl -s -X POST http://localhost:7800/api/dock/validate \
  -H 'Content-Type: application/json' \
  -d @dashy.json | jq '.'
```

Pass `_source_hint` to also check whether the file location is under a scan root:

```bash
jq '. + {"_source_hint": "/home/ubuntu/server/my-project"}' dashy.json \
  | curl -s -X POST http://localhost:7800/api/dock/validate \
    -H 'Content-Type: application/json' -d @- | jq '.'
```

A valid response looks like:

```json
{ "ok": true, "errors": [], "warnings": [] }
```

Errors block the service from being discovered correctly. Warnings are advisory.

---

## 4. Commit the file and wait

```bash
git add dashy.json && git commit -m "chore: dock into dashy"
```

Within `scan_interval_sec` the service appears in the dashboard and in:

```bash
curl -s http://localhost:7800/api/services | jq '.[] | select(.project=="my-project")'
```

---

## Agent checklist

When an agent is told to "dock into dashy":

1. `GET http://localhost:7800/api/dock` — read `scan_roots` and `scan_interval_sec`.
2. Check whether the project root is under a `scan_root`. If not, POST to add it.
3. Determine all services the project runs (ports, pid files, start commands).
4. Write `dashy.json` to the project or worktree root following the schema above.
5. `POST /api/dock/validate` with `_source_hint` set — fix any errors.
6. Commit the file.
7. Verify: `curl http://localhost:7800/api/services | jq '.[] | select(.project=="<name>") | {id,status}'`
