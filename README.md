# dashy

Local dev service control plane. Stdlib Python BFF at port `7800` + single-file vanilla JS dashboard. Auto-discovers services via `devdash.json` files committed to project/worktree roots.

## Running

```bash
python3 server.py
# http://localhost:7800
```

## Installing as a systemd service

```bash
sudo bash install.sh
systemctl status dashy
```

## Docking a project into dashy

```
1. curl http://localhost:7800/api/dock/guide  — read the full guide
2. Follow the agent checklist at the bottom
```
