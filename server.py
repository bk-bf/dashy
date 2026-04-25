#!/usr/bin/env python3
"""dashy — dev service control plane. Phase 4: actions API."""

import collections
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")

_registry: dict = {}
_registry_lock = threading.Lock()

LOG_MAXLINES = 200
_logs: dict[str, collections.deque] = {}  # id → deque of str
_logs_lock = threading.Lock()

_sse_clients: list = []
_sse_lock = threading.Lock()

_applied_policies: dict[str, str] = {}  # sid → last restart_policy we applied
_restart_policy_overrides: dict[
    str, str
] = {}  # sid → runtime policy (non-systemd services)
_intentional_stops: set[str] = set()  # services stopped deliberately via dashy
_prev_statuses: dict[str, str] = {}  # sid → status from previous scan cycle


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def scan_for_dashy(root: str, max_depth: int, excludes: list[str]) -> list[str]:
    results = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root) :].count(os.sep)
        if depth >= max_depth:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in excludes]
        if "dashy.json" in filenames:
            results.append(os.path.join(dirpath, "dashy.json"))
    return results


def load_dashy_manifest(path: str) -> list[dict]:
    try:
        with open(path) as f:
            data = json.load(f)
        services = data.get("services", [])
        for svc in services:
            svc["_source_file"] = path
            svc.setdefault("project", data.get("project", ""))
            svc.setdefault("worktree", data.get("worktree"))
        return services
    except Exception as e:
        print(f"[dashy] failed to load {path}: {e}")
        return []


def refresh_services() -> None:
    while True:
        cfg = load_config()
        roots = cfg.get("scan_roots", [])
        max_depth = cfg.get("scan_max_depth", 4)
        excludes = cfg.get("scan_exclude", [])

        new_registry: dict = {}
        for root in roots:
            if not os.path.isdir(root):
                continue
            for path in scan_for_dashy(root, max_depth, excludes):
                for svc in load_dashy_manifest(path):
                    sid = svc.get("id")
                    if sid:
                        new_registry[sid] = svc

        for svc in new_registry.values():
            _merge_status(svc)

        # Auto-restart non-systemd services that stopped unexpectedly
        for svc in new_registry.values():
            sid = svc["id"]
            status = svc.get("status")
            prev = _prev_statuses.get(sid)
            policy = svc.get("_restart_policy", "no")
            is_systemd = bool(_systemd_unit(svc))
            crashed = prev == "running" and status in ("stopped", "error")
            if (
                not is_systemd
                and policy in ("always", "on-failure")
                and status in ("stopped", "error")
                and crashed
                and sid not in _intentional_stops
            ):
                _log(
                    sid,
                    f"[dashy] unexpected stop detected (policy={policy}) — restarting",
                )
                threading.Thread(
                    target=lambda s=svc: action_start(s), daemon=True
                ).start()
            _prev_statuses[sid] = status

        with _registry_lock:
            _registry.clear()
            _registry.update(new_registry)

        _sse_broadcast()
        time.sleep(cfg.get("scan_interval_sec", 10))


# ---------------------------------------------------------------------------
# Status engine
# ---------------------------------------------------------------------------

_BOOT_TIME: float = 0.0


def _boot_time() -> float:
    global _BOOT_TIME
    if not _BOOT_TIME:
        try:
            with open("/proc/uptime") as f:
                uptime_sec = float(f.read().split()[0])
            _BOOT_TIME = time.time() - uptime_sec
        except Exception:
            _BOOT_TIME = time.time()
    return _BOOT_TIME


def check_pid(pid_file: str | None) -> dict:
    if not pid_file:
        return {"alive": False, "pid": None, "stale": False}
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return {"alive": False, "pid": None, "stale": False}
    try:
        os.kill(pid, 0)
        return {"alive": True, "pid": pid, "stale": False}
    except ProcessLookupError:
        return {"alive": False, "pid": pid, "stale": True}
    except PermissionError:
        return {"alive": True, "pid": pid, "stale": False}


def check_port(port: int | None) -> bool:
    if not port:
        return False
    try:
        out = subprocess.check_output(
            ["ss", "-HtlnO", f"sport = :{port}"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return bool(out.strip())
    except Exception:
        return False


def pids_on_port(port: int) -> list[int]:
    """Return PIDs of processes listening on the given TCP port.

    Uses `fuser` which reads /proc/net/tcp (world-readable) and works for
    processes owned by any user — unlike `ss -p` which only shows own-user PIDs.
    """
    try:
        out = subprocess.check_output(
            ["fuser", f"{port}/tcp"],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        )
        return [int(p) for p in out.split() if p.strip().isdigit()]
    except subprocess.CalledProcessError:
        return []  # fuser exits 1 when nothing is on the port
    except Exception:
        return []


def action_clean(svc: dict) -> dict:
    """Kill any process holding the service's port and remove stale pid_file."""
    sid = svc["id"]
    port = svc.get("port")
    pid_file = svc.get("pid_file")

    if port:
        _kill_port(sid, port)

    if pid_file:
        try:
            os.remove(pid_file)
        except FileNotFoundError:
            pass

    return {"ok": True, "message": "cleaned"}


def get_uptime(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        starttime_ticks = int(fields[21])
        clk_tck = os.sysconf("SC_CLK_TCK")
        start_sec = _boot_time() + starttime_ticks / clk_tck
        return max(0, int(time.time() - start_sec))
    except Exception:
        return None


def _systemd_unit(svc: dict) -> str | None:
    """Extract the primary systemd unit name from stop_cmd or start_cmd."""
    for cmd in (svc.get("stop_cmd") or "", svc.get("start_cmd") or ""):
        parts = cmd.split()
        try:
            idx = parts.index("systemctl")
            # systemctl [sudo] <verb> <unit> — unit is two positions after 'systemctl'
            if idx + 2 < len(parts):
                return parts[idx + 2]
        except ValueError:
            pass
    return None


def _systemd_info(unit: str) -> dict:
    """Query systemd for restart policy, last exit status, and reverse dependencies."""
    info: dict = {"restart_policy": None, "exit_status": None, "reverse_deps": []}
    try:
        out = subprocess.check_output(
            ["systemctl", "show", unit, "--property=Restart,ExecMainStatus"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        for line in out.splitlines():
            if line.startswith("Restart="):
                info["restart_policy"] = line.split("=", 1)[1]
            elif line.startswith("ExecMainStatus="):
                try:
                    info["exit_status"] = int(line.split("=", 1)[1])
                except ValueError:
                    pass
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["systemctl", "list-dependencies", unit, "--reverse", "--plain"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # Filter out standard system targets — every enabled service is a dep of these
        _SYSTEM_TARGETS = {
            "multi-user.target",
            "graphical.target",
            "default.target",
            "sysinit.target",
            "basic.target",
            "network.target",
        }
        info["reverse_deps"] = [l for l in lines[1:] if l and l not in _SYSTEM_TARGETS]
    except Exception:
        pass
    return info


def _auto_apply_restart_policy(svc: dict, unit: str, sd: dict) -> None:
    """If dashy.json declares restart_policy and it differs from current, apply it once."""
    declared = svc.get("restart_policy")
    if not declared:
        return
    sid = svc["id"]
    current = sd.get("restart_policy")
    if current == declared:
        _applied_policies[sid] = declared
        _restart_policy_overrides.pop(
            sid, None
        )  # systemd confirmed — override no longer needed
        return
    if _applied_policies.get(sid) == declared:
        return  # already attempted this cycle — don't hammer systemd every scan

    def _do():
        try:
            subprocess.run(
                ["sudo", "/usr/local/bin/dashy-set-restart", unit, declared],
                check=True,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _applied_policies[sid] = declared
            _log(sid, f"[dashy] auto-applied Restart={declared} from dashy.json")
        except Exception as e:
            _log(sid, f"[dashy] auto-apply restart_policy failed: {e}")
        _refresh_status(sid)

    threading.Thread(target=_do, daemon=True).start()


def _merge_status(svc: dict) -> None:
    pid_file = svc.get("pid_file")
    pid_info = check_pid(pid_file)
    port_bound = check_port(svc.get("port"))
    pid = pid_info["pid"]
    alive = pid_info["alive"]
    stale = pid_info["stale"]

    if pid_file is None:
        # No pid tracking — check systemd first, then fall back to port presence
        _unit_early = _systemd_unit(svc)
        if _unit_early:
            import subprocess as _sp

            _rc = _sp.run(
                ["systemctl", "is-active", "--quiet", _unit_early],
                capture_output=True,
            ).returncode
            status = "running" if _rc == 0 else "stopped"
        else:
            status = "running" if port_bound else "stopped"
    elif alive and port_bound:
        status = "running"
    elif alive and not port_bound:
        status = "starting"
    elif not alive and not port_bound and stale:
        status = "error"
    elif not alive and port_bound:
        status = "zombie"
    else:
        status = "stopped"

    svc["status"] = status
    svc["pid"] = pid if alive else None
    svc["port_bound"] = port_bound
    svc["uptime_sec"] = get_uptime(pid) if alive and pid else None

    # Systemd dependency/restart health warnings
    unit = _systemd_unit(svc)
    if unit:
        sd = _systemd_info(unit)
        svc["_systemd"] = sd
        warns = []
        effective_pol = _restart_policy_overrides.get(svc["id"]) or sd["restart_policy"]
        if effective_pol == "always":
            warns.append(
                f"Restart=always on '{unit}' — stop may not stick (change to on-failure)"
            )
        if sd["reverse_deps"]:
            warns.append(
                f"reverse deps: {', '.join(sd['reverse_deps'])} — may restart this service"
            )
        # Exit status 0 = clean; 15 = SIGTERM (systemctl stop); 9 = SIGKILL — all normal
        _CLEAN_EXIT = {0, 9, 15}
        if sd["exit_status"] not in (None, *_CLEAN_EXIT) and status == "stopped":
            warns.append(
                f"last exit status: {sd['exit_status']} — systemd may treat this as a crash"
            )
        svc["systemd_warnings"] = warns
        _auto_apply_restart_policy(svc, unit, sd)
    else:
        svc.setdefault("systemd_warnings", [])

    # Unified _restart_policy field for all services (used by UI toggle).
    # Override takes precedence over live systemd state — set on manual toggle,
    # cleared by _auto_apply_restart_policy once systemd confirms the change.
    override = _restart_policy_overrides.get(svc["id"])
    if override:
        svc["_restart_policy"] = override
    elif unit and svc.get("_systemd"):
        svc["_restart_policy"] = svc["_systemd"].get("restart_policy") or "no"
    else:
        svc["_restart_policy"] = svc.get("restart_policy") or "no"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _log(sid: str, line: str) -> None:
    with _logs_lock:
        if sid not in _logs:
            _logs[sid] = collections.deque(maxlen=LOG_MAXLINES)
        _logs[sid].append(line)


def _stream_output(sid: str, proc: subprocess.Popen) -> None:
    """Read proc stdout into the ring buffer until EOF."""
    for raw in proc.stdout:
        _log(sid, raw.rstrip("\n"))
    proc.wait()


def action_start(svc: dict) -> dict:
    sid = svc["id"]
    _intentional_stops.discard(sid)
    start_cmd = svc.get("start_cmd")
    cwd = svc.get("cwd")
    if not start_cmd:
        return {"ok": False, "message": "no start_cmd defined"}
    with _logs_lock:
        _logs[sid] = collections.deque(maxlen=LOG_MAXLINES)
    _log(sid, f"[dashy] starting: {start_cmd}")
    try:
        proc = subprocess.Popen(
            start_cmd,
            shell=True,
            cwd=cwd or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,  # new process group — isolates from dashy
        )
        t = threading.Thread(target=_stream_output, args=(sid, proc), daemon=True)
        t.start()
        return {"ok": True, "message": "started"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _kill_port(sid: str, port: int) -> bool:
    """Kill all processes on port via 'sudo fuser -k', works for any owning user."""
    if not check_port(port):
        return True
    _log(sid, f"[dashy] killing port {port} (sudo fuser -k)")
    try:
        subprocess.run(
            ["sudo", "fuser", "-k", f"{port}/tcp"],
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        _log(sid, f"[dashy] fuser -k error: {e}")
    # Wait up to 3 s for port to free
    for _ in range(30):
        time.sleep(0.1)
        if not check_port(port):
            _log(sid, "[dashy] stopped")
            return True
    _log(sid, f"[dashy] port {port} still bound after kill attempt")
    return False


def action_stop(svc: dict) -> dict:
    """Stop a service.

    Strategy (in order):
    1. Run stop_cmd if defined (best-effort, async — e.g. systemctl, custom script).
       After stop_cmd completes, fall through to port-kill if port is still bound.
    2. Kill by port using fuser — works for any user, no pid_file required.
    3. Clean up pid_file if present.

    Port ownership is the ground truth. pid_file is only used for cleanup.
    """
    sid = svc["id"]
    stop_cmd = svc.get("stop_cmd")
    pid_file = svc.get("pid_file")
    port = svc.get("port")
    _intentional_stops.add(sid)

    def _cleanup_pid_file():
        if pid_file:
            try:
                os.remove(pid_file)
            except FileNotFoundError:
                pass

    def _do_stop():
        stop_cmd_succeeded = False
        if stop_cmd:
            _log(sid, f"[dashy] stop_cmd: {stop_cmd}")
            try:
                proc = subprocess.run(
                    stop_cmd,
                    shell=True,
                    timeout=15,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in (proc.stdout or "").splitlines():
                    _log(sid, line)
                if proc.returncode != 0:
                    _log(sid, f"[dashy] stop_cmd exited {proc.returncode}")
                else:
                    stop_cmd_succeeded = True
            except subprocess.TimeoutExpired:
                _log(sid, "[dashy] stop_cmd timed out — falling back to port-kill")
            except Exception as e:
                _log(sid, f"[dashy] stop_cmd error: {e}")

        # Only port-kill if there was no stop_cmd or it failed — never after a
        # successful systemctl stop, as an external SIGKILL would look like a
        # crash to systemd and trigger Restart=on-failure
        if not stop_cmd_succeeded and port and check_port(port):
            _kill_port(sid, port)

        _cleanup_pid_file()
        _refresh_status(sid)

    threading.Thread(target=_do_stop, daemon=True).start()
    return {"ok": True, "message": "stopping"}


def action_set_restart(svc: dict, policy: str) -> dict:
    """Set restart policy. Systemd services: dropin via helper. Others: in-memory override."""
    if policy not in ("always", "on-failure", "no"):
        return {"ok": False, "message": f"invalid policy: {policy}"}
    sid = svc["id"]
    unit = _systemd_unit(svc)

    if not unit:
        # Non-systemd: store override, update svc immediately, broadcast
        _restart_policy_overrides[sid] = policy
        _log(sid, f"[dashy] restart policy set to {policy}")
        threading.Thread(target=lambda: _refresh_status(sid), daemon=True).start()
        return {"ok": True, "message": f"restart_policy={policy}"}

    # Systemd: set override immediately so scans don't revert the UI while dropin is being written
    _restart_policy_overrides[sid] = policy

    def _do():
        try:
            result = subprocess.run(
                ["sudo", "/usr/local/bin/dashy-set-restart", unit, policy],
                check=True,
                timeout=15,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in (result.stdout or "").splitlines():
                _log(sid, f"[dashy] {line}")
        except subprocess.CalledProcessError as e:
            _log(
                sid, f"[dashy] set-restart failed: {(e.output or '').strip() or str(e)}"
            )
        except Exception as e:
            _log(sid, f"[dashy] set-restart error: {e}")
        _refresh_status(sid)

    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True, "message": f"setting Restart={policy}…"}


def action_restart(svc: dict) -> dict:
    stop_result = action_stop(svc)
    _log(svc["id"], f"[dashy] restart: stop → {stop_result['message']}")
    time.sleep(0.5)
    return action_start(svc)


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def _refresh_status(sid: str) -> None:
    """Re-check status for one service and broadcast to SSE clients."""
    with _registry_lock:
        svc = _registry.get(sid)
    if svc is None:
        return
    _merge_status(svc)
    with _registry_lock:
        _registry[sid] = svc
    _sse_broadcast()


def _sse_broadcast() -> None:
    with _registry_lock:
        payload = json.dumps(list(_registry.values()))
    data = f"event: services.updated\ndata: {payload}\n\n".encode()
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(data)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_svc(self, sid: str) -> dict | None:
        with _registry_lock:
            return _registry.get(sid)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = path.strip("/").split("/")

        if path in ("/", "/dashboard.html"):
            try:
                with open(DASHBOARD_PATH, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_error(404, "dashboard.html not found")

        elif path == "/api/services":
            with _registry_lock:
                self.send_json(list(_registry.values()))

        # GET /api/services/{id}/log
        elif (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "services"
            and parts[3] == "log"
        ):
            sid = parts[2]
            with _logs_lock:
                lines = list(_logs.get(sid, []))
            # If the ring buffer is empty, fall back to log_file declared in dashy.json
            if not lines:
                with _registry_lock:
                    svc = _registry.get(sid, {})
                log_file = svc.get("log_file")
                if log_file:
                    try:
                        with open(log_file) as f:
                            all_lines = f.read().splitlines()
                        lines = all_lines[-LOG_MAXLINES:]
                    except Exception as e:
                        lines = [f"[dashy] could not read log_file: {e}"]
                else:
                    lines = []
            self.send_json({"lines": lines})

        elif path == "/api/config":
            self.send_json(load_config())

        elif path == "/api/events":
            import queue

            q: queue.Queue = queue.Queue(maxsize=10)
            with _sse_lock:
                _sse_clients.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                # send current state immediately
                with _registry_lock:
                    payload = json.dumps(list(_registry.values()))
                self.wfile.write(
                    f"event: services.updated\ndata: {payload}\n\n".encode()
                )
                self.wfile.flush()
                while True:
                    try:
                        data = q.get(timeout=30)
                        self.wfile.write(data)
                        self.wfile.flush()
                    except Exception:
                        # heartbeat keep-alive
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _sse_lock:
                    if q in _sse_clients:
                        _sse_clients.remove(q)

        elif path == "/api/dock":
            cfg = load_config()
            self.send_json(
                {
                    "dashy_url": f"http://localhost:{cfg.get('port', 7800)}",
                    "docking_guide": "http://localhost:{}/api/dock/guide".format(
                        cfg.get("port", 7800)
                    ),
                    "scan_roots": cfg.get("scan_roots", []),
                    "scan_max_depth": cfg.get("scan_max_depth", 4),
                    "scan_exclude": cfg.get("scan_exclude", []),
                    "scan_interval_sec": cfg.get("scan_interval_sec", 10),
                    "devdash_filename": "dashy.json",
                    "contract": {
                        "required_fields": ["project", "services"],
                        "service_required_fields": ["id", "name", "start_cmd", "cwd"],
                        "service_optional_fields": [
                            "port",
                            "pid_file",
                            "stop_cmd",
                            "worktree",
                            "log_file",
                            "restart_policy",
                        ],
                        "restart_policy": (
                            "Optional. Applies to all services. "
                            "Values: 'no' (recommended default — never auto-restart), "
                            "'on-failure' (restart on crash/error), "
                            "'always' (restart on any unexpected stop). "
                            "For systemd-managed services: dashy writes a dropin to "
                            "/etc/systemd/system/<unit>.d/dashy-restart.conf and reloads on discovery. "
                            "For dashy-started (pid-file) services: policy is stored in memory "
                            "(declare in dashy.json to persist across dashy restarts); dashy "
                            "auto-restarts on unexpected stop detected during scan. "
                            "Clicking Stop always suppresses auto-restart regardless of policy."
                        ),
                        "id_convention": "<project>-<worktree>-<role>  (omit worktree segment for main branch)",
                        "stop_cmd_null": "dashy kills via pid_file: SIGTERM → 5s → SIGKILL",
                        "pid_file": "absolute path written by start_cmd; may not exist when stopped",
                    },
                    "instructions": (
                        "1. Create dashy.json in the project or worktree root. "
                        "2. Ensure that root (or a parent) is listed in scan_roots — "
                        "check this response's scan_roots; if missing POST /api/config/scan_roots. "
                        "3. No registration call needed — dashy picks it up within scan_interval_sec."
                    ),
                }
            )

        elif path == "/api/dock/guide":
            doc = os.path.join(BASE_DIR, ".docs", "DOCKING.md")
            try:
                with open(doc, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_error(404, "DOCKING.md not found")

        elif path == "/api/fs/dirs":
            import queue as _q
            from urllib.parse import parse_qs

            qs = parse_qs(parsed.query)
            prefix = qs.get("q", [""])[0]
            results = []
            if prefix:
                parent = os.path.dirname(prefix) if not prefix.endswith("/") else prefix
                try:
                    for entry in sorted(os.scandir(parent), key=lambda e: e.name):
                        if entry.is_dir() and entry.path.startswith(prefix):
                            results.append(entry.path)
                            if len(results) >= 20:
                                break
                except Exception:
                    pass
            self.send_json(results)

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = path.strip("/").split("/")

        # POST /api/services/{id}/start|stop|restart
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "services":
            sid = parts[2]
            action = parts[3]
            svc = self._get_svc(sid)
            if svc is None:
                return self.send_json(
                    {"ok": False, "message": "service not found"}, 404
                )
            if action == "start":
                result = action_start(svc)
                threading.Thread(
                    target=lambda: (time.sleep(1.5), _refresh_status(sid)), daemon=True
                ).start()
                return self.send_json(result)
            elif action == "stop":
                result = action_stop(svc)
                threading.Thread(
                    target=lambda: _refresh_status(sid), daemon=True
                ).start()
                return self.send_json(result)
            elif action == "restart":
                result = action_restart(svc)
                threading.Thread(
                    target=lambda: (time.sleep(1.5), _refresh_status(sid)), daemon=True
                ).start()
                return self.send_json(result)
            elif action == "clean":
                result = action_clean(svc)
                threading.Thread(
                    target=lambda: _refresh_status(sid), daemon=True
                ).start()
                return self.send_json(result)
            elif action == "set-restart":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                policy = body.get("policy", "")
                return self.send_json(action_set_restart(svc, policy))
            else:
                return self.send_error(404)

        elif path == "/api/dock/guide":
            doc = os.path.join(BASE_DIR, ".docs", "DOCKING.md")
            try:
                with open(doc, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_error(404, "DOCKING.md not found")

        elif path == "/api/dock/validate":
            length = int(self.headers.get("Content-Length", 0))
            try:
                manifest = json.loads(self.rfile.read(length))
            except Exception:
                return self.send_json({"ok": False, "errors": ["invalid JSON"]}, 400)

            errors = []
            warnings = []

            if not manifest.get("project"):
                errors.append("missing required field: project")

            services_raw = manifest.get("services")
            if not isinstance(services_raw, list) or len(services_raw) == 0:
                errors.append(
                    "missing required field: services (must be a non-empty array)"
                )
            else:
                seen_ids = set()
                for i, svc in enumerate(services_raw):
                    prefix = f"services[{i}]"
                    for f in ("id", "name", "start_cmd", "cwd"):
                        if not svc.get(f):
                            errors.append(f"{prefix}: missing required field: {f}")
                    sid = svc.get("id", "")
                    if sid in seen_ids:
                        errors.append(f"{prefix}: duplicate id '{sid}'")
                    seen_ids.add(sid)
                    # id convention check
                    project = manifest.get("project", "")
                    if sid and project and not sid.startswith(project):
                        warnings.append(
                            f"{prefix}: id '{sid}' does not start with project '{project}' "
                            f"— convention is <project>-<worktree>-<role>"
                        )
                    if not svc.get("pid_file") and not svc.get("stop_cmd"):
                        warnings.append(
                            f"{prefix}: no pid_file and no stop_cmd — "
                            "dashy will be unable to stop this service"
                        )
                    # Check for systemd stop_cmd with Restart=always anti-pattern
                    stop_cmd = svc.get("stop_cmd") or ""
                    start_cmd = svc.get("start_cmd") or ""
                    is_systemd = "systemctl" in stop_cmd or "systemctl" in start_cmd
                    if is_systemd:
                        rp = svc.get("restart_policy")
                        if not rp:
                            warnings.append(
                                f"{prefix}: systemd-managed service has no 'restart_policy' field — "
                                'add \'"restart_policy": "no"\' so dashy enforces the policy on discovery '
                                "(no = dashy controls start/stop; on-failure = restart on crash only)"
                            )
                        elif rp not in ("no", "on-failure", "always"):
                            errors.append(
                                f"{prefix}: invalid restart_policy '{rp}' — "
                                "must be 'no', 'on-failure', or 'always'"
                            )
                        elif rp == "always":
                            warnings.append(
                                f"{prefix}: restart_policy 'always' — "
                                "the stop button will not work reliably; prefer 'no' or 'on-failure'"
                            )
                    if "systemctl stop" in stop_cmd:
                        unit = stop_cmd.strip().split()[-1]
                        try:
                            out = subprocess.check_output(
                                ["systemctl", "show", unit, "--property=Restart"],
                                text=True,
                                stderr=subprocess.DEVNULL,
                                timeout=2,
                            )
                            if "Restart=always" in out:
                                warnings.append(
                                    f"{prefix}: systemd unit '{unit}' has Restart=always — "
                                    "dashy's stop button will not work reliably because systemd "
                                    "will respawn the process after every stop. "
                                    "Change the unit to Restart=on-failure."
                                )
                        except Exception:
                            pass
                    if svc.get("cwd") and not os.path.isabs(svc["cwd"]):
                        errors.append(f"{prefix}: cwd must be an absolute path")
                    if svc.get("pid_file") and not os.path.isabs(svc["pid_file"]):
                        errors.append(f"{prefix}: pid_file must be an absolute path")

            # Check if project root would be scanned
            cfg = load_config()
            roots = cfg.get("scan_roots", [])
            source_hint = manifest.get("_source_hint", "")
            if source_hint:
                covered = any(
                    os.path.abspath(source_hint).startswith(os.path.abspath(r))
                    for r in roots
                )
                if not covered:
                    warnings.append(
                        f"path '{source_hint}' is not under any scan_root — "
                        f"add it via POST /api/config/scan_roots"
                    )

            self.send_json(
                {
                    "ok": len(errors) == 0,
                    "errors": errors,
                    "warnings": warnings,
                }
            )

        elif path == "/api/config/scan_roots":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            cfg = load_config()
            roots = cfg.setdefault("scan_roots", [])
            action = body.get("action", "add")
            root = body.get("root", "").strip()
            if not root:
                return self.send_json({"ok": False, "message": "root is required"}, 400)
            if action == "add" and root not in roots:
                roots.append(root)
            elif action == "remove" and root in roots:
                roots.remove(root)
            save_config(cfg)
            self.send_json({"ok": True, "scan_roots": roots})

        else:
            self.send_error(404)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = load_config()
    port = cfg.get("port", 7800)

    t = threading.Thread(target=refresh_services, daemon=True)
    t.start()

    server = ThreadingHTTPServer(("", port), Handler)
    print(f"dashy listening on http://localhost:{port}/")
    server.serve_forever()
