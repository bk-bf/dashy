#!/usr/bin/env python3
"""dashy — dev service control plane. Phase 4: actions API."""

import collections
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def scan_for_devdash(root: str, max_depth: int, excludes: list[str]) -> list[str]:
    results = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root) :].count(os.sep)
        if depth >= max_depth:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in excludes]
        if "devdash.json" in filenames:
            results.append(os.path.join(dirpath, "devdash.json"))
    return results


def load_manifest(path: str) -> list[dict]:
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
            for path in scan_for_devdash(root, max_depth, excludes):
                for svc in load_manifest(path):
                    sid = svc.get("id")
                    if sid:
                        new_registry[sid] = svc

        for svc in new_registry.values():
            _merge_status(svc)

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


def _merge_status(svc: dict) -> None:
    pid_info = check_pid(svc.get("pid_file"))
    port_bound = check_port(svc.get("port"))
    pid = pid_info["pid"]
    alive = pid_info["alive"]
    stale = pid_info["stale"]

    if alive and port_bound:
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
    start_cmd = svc.get("start_cmd")
    cwd = svc.get("cwd")
    if not start_cmd:
        return {"ok": False, "message": "no start_cmd defined"}
    _log(sid, f"[dashy] starting: {start_cmd}")
    try:
        proc = subprocess.Popen(
            start_cmd,
            shell=True,
            cwd=cwd or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        t = threading.Thread(target=_stream_output, args=(sid, proc), daemon=True)
        t.start()
        return {"ok": True, "message": "started"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def action_stop(svc: dict) -> dict:
    sid = svc["id"]
    stop_cmd = svc.get("stop_cmd")
    pid_file = svc.get("pid_file")

    if stop_cmd:
        _log(sid, f"[dashy] stopping via stop_cmd: {stop_cmd}")
        try:
            subprocess.run(stop_cmd, shell=True, timeout=15)
            return {"ok": True, "message": "stopped via stop_cmd"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": "stop_cmd timed out"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    pid_info = check_pid(pid_file)
    if not pid_info["alive"]:
        return {"ok": False, "message": "process not running"}
    pid = pid_info["pid"]
    _log(sid, f"[dashy] sending SIGTERM to pid {pid}")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):  # wait up to 5s
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                _log(sid, "[dashy] process exited")
                return {"ok": True, "message": "stopped"}
        _log(sid, f"[dashy] pid {pid} still alive after 5s, sending SIGKILL")
        os.kill(pid, signal.SIGKILL)
        return {"ok": True, "message": "killed"}
    except ProcessLookupError:
        return {"ok": True, "message": "stopped"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def action_restart(svc: dict) -> dict:
    stop_result = action_stop(svc)
    _log(svc["id"], f"[dashy] restart: stop → {stop_result['message']}")
    time.sleep(0.5)
    return action_start(svc)


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


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
                    "devdash_filename": "devdash.json",
                    "contract": {
                        "required_fields": ["project", "services"],
                        "service_required_fields": ["id", "name", "start_cmd", "cwd"],
                        "service_optional_fields": [
                            "port",
                            "pid_file",
                            "stop_cmd",
                            "worktree",
                        ],
                        "id_convention": "<project>-<worktree>-<role>  (omit worktree segment for main branch)",
                        "stop_cmd_null": "dashy kills via pid_file: SIGTERM → 5s → SIGKILL",
                        "pid_file": "absolute path written by start_cmd; may not exist when stopped",
                    },
                    "instructions": (
                        "1. Create devdash.json in the project or worktree root. "
                        "2. Ensure that root (or a parent) is listed in scan_roots — "
                        "check this response's scan_roots; if missing POST /api/config/scan_roots. "
                        "3. No registration call needed — dashy picks it up within scan_interval_sec."
                    ),
                }
            )

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
                return self.send_json(action_start(svc))
            elif action == "stop":
                return self.send_json(action_stop(svc))
            elif action == "restart":
                return self.send_json(action_restart(svc))
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

    server = HTTPServer(("", port), Handler)
    print(f"dashy listening on http://localhost:{port}/")
    server.serve_forever()
