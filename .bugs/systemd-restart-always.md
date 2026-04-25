# Bug: systemd service with Restart=always cannot be stopped via dashy

**Date:** 2026-04-25  
**Status:** UNRESOLVED — do not attempt to diagnose again without a working test first

---

## Symptom

Clicking Stop on a systemd-managed service (e.g. `openswarm-dashboard`, port 7700)
causes it to go inactive for ~5 seconds then come back up. The dashboard shows it
running again shortly after every stop attempt.

---

## Root service facts

```
Unit:    /etc/systemd/system/openswarm-dashboard.service
User:    agent
Restart: was Restart=always, changed to Restart=on-failure — did not fix it
```

---

## Everything attempted (all failed)

1. **Set `stop_cmd: null`** in `openswarm/dashy.json` — dashy killed via `fuser -k`.
   Process died, systemd respawned it.

2. **`ss -p` for PID discovery** — only shows own-user PIDs, missed agent-owned process.

3. **Switched to `fuser` for PID discovery** — finds PIDs cross-user correctly.
   But `os.kill()` from `ubuntu` on `agent`-owned process → `EPERM`. Kill failed silently.

4. **Changed `dashy.service` to `User=agent`** — agent can kill its own processes.
   But then agent can't kill ubuntu-owned processes. Migrated the problem, didn't solve it.

5. **Added sudoers rule for `ubuntu`** — wrong user, dashy runs as `agent`. Rule never applied.

6. **Added sudoers rule for `agent`** — correct user. But `install.sh` was never run as root
   so the file was never written. Stop kept failing.

7. **Fixed `install.sh` to use `SUDO_USER`** — install ran, sudoers file written.
   `sudo fuser -k` now works. But systemd with `Restart=always` respawns after kill.

8. **Changed `Restart=always` → `Restart=on-failure`** in repo source file only.
   `/etc/systemd/system/` copy not updated. `daemon-reload` had no effect.

9. **Copied fixed unit to `/etc/systemd/system/`** + `daemon-reload`. systemd picked up
   `Restart=on-failure`. Still respawned after stop.

10. **Diagnosed race condition**: `stop_cmd` (systemctl stop) + `_kill_port` both running,
    SIGKILL from fuser making systemd think it was a crash → restart triggered.
    Fixed `_do_stop` to skip `_kill_port` when `stop_cmd` succeeded. Still respawns.

---

## Current state

- `Restart=on-failure` is confirmed active in systemd (`systemctl show` confirms it)
- `sudo systemctl stop openswarm-dashboard` stops it cleanly (exit 0)
- It comes back ~8 seconds later anyway
- Unknown why `Restart=on-failure` is not respecting a clean `systemctl stop`

---

## Do not touch until

A minimal reproduction is confirmed:
```bash
sudo systemctl stop openswarm-dashboard
sleep 8
systemctl is-active openswarm-dashboard  # should be inactive
```
If that returns `active`, the issue is entirely in systemd config, not in dashy.
Fix it there first before touching any dashy code.
