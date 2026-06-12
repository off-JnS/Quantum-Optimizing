# 🚀 Hosting on Hostinger (24/7) — straight from this GitHub repo

This repo is structured so a Hostinger **VPS** can run it directly from
GitHub: the included `Dockerfile` + `docker-compose.yml` start the app and a
Caddy reverse proxy (automatic HTTPS), restart it after crashes and reboots,
and read your IBM Quantum key from an environment variable.

> **Why a VPS and not Hostinger's shared "Web Hosting"?** Shared hosting runs
> PHP websites; it cannot keep a Python/Streamlit server process (or the
> qiskit stack) running. Hostinger's VPS plans give you a full Linux machine
> with Docker — that's what this app needs to run 24/7.

**Recommended plan:** KVM 2 (2 vCPU / 8 GB RAM) or larger. The qiskit stack
plus Aer simulations are memory-hungry; 4 GB (KVM 1) works for IBM-hardware
mode but is tight for local simulation of 16-qubit chunks.

---

## Option A — deploy from GitHub via hPanel (no terminal needed)

1. **Buy a VPS** at hostinger.com → VPS. During setup choose the OS template
   **"Ubuntu 24.04 with Docker"** (under *Applications*; any Docker template
   works).
2. In **hPanel → VPS → your server**, open the **Docker Manager**.
3. Click **Create project / Compose from URL** and paste this repository's
   URL — Docker Manager reads the `docker-compose.yml` from the repo root:
   ```
   https://github.com/off-JnS/Quantum-Optimizing
   ```
4. Add the **environment variables** when prompted (or in the project's
   `.env`):
   | Variable | Value |
   |---|---|
   | `IBM_QUANTUM_TOKEN` | your IBM Quantum API key (optional but recommended) |
   | `DOMAIN` | your domain, e.g. `quantum.yourdomain.com` — leave unset to serve plain HTTP on the VPS IP |
5. **Deploy.** The first build takes a few minutes (it installs the whole
   qiskit stack). Then open `http://<your-vps-ip>` — or your domain once DNS
   is set (next section).

### Point a domain at it

1. hPanel → **Domains → DNS Zone** (or wherever your DNS lives).
2. Add an **A record**: host `quantum` (or `@`), value = your VPS IP.
3. Set `DOMAIN=quantum.yourdomain.com` in the Docker project's environment and
   redeploy. Caddy fetches a Let's Encrypt certificate automatically — your
   app is now at `https://quantum.yourdomain.com`.

---

## Option B — deploy over SSH (terminal, 5 commands)

```bash
ssh root@<your-vps-ip>

# Docker is preinstalled on the "with Docker" template; otherwise:
# curl -fsSL https://get.docker.com | sh

git clone https://github.com/off-JnS/Quantum-Optimizing.git
cd Quantum-Optimizing

# Configure (both variables optional — see table above)
printf "IBM_QUANTUM_TOKEN=paste-your-key-here\nDOMAIN=quantum.yourdomain.com\n" > .env

docker compose up -d --build
```

That's it. Check status and logs with:

```bash
docker compose ps
docker compose logs -f app
```

### Updating to a new version

```bash
cd Quantum-Optimizing
git pull
docker compose up -d --build
```

(In hPanel's Docker Manager the same thing is the **Redeploy/Rebuild** button.)

---

## Why this stays up 24/7

* `restart: unless-stopped` on both containers — Docker revives the app after
  a crash **and** after a VPS reboot.
* A **health check** hits Streamlit's `/_stcore/health` endpoint every 30 s so
  Docker knows when the app is genuinely up.
* Caddy terminates HTTPS and proxies WebSockets (which Streamlit requires) —
  no manual nginx/certbot maintenance, certificates renew themselves.

---

## Security checklist

* **Never commit your IBM key.** It belongs in the `.env` file / hPanel
  environment variables only (`.env` is git-ignored).
* The app is public once deployed. To restrict access, add basic auth to
  `deploy/Caddyfile`:
  ```
  {$DOMAIN} {
      basic_auth {
          you   <hash from: docker run caddy caddy hash-password --plaintext 'yourpass'>
      }
      reverse_proxy app:8501
  }
  ```
  then `docker compose restart caddy`.
* Keep the VPS firewall to ports 22/80/443 (hPanel → VPS → Firewall), and use
  SSH keys instead of passwords.
* Users of the hosted site share **your** IBM quota if `IBM_QUANTUM_TOKEN` is
  set server-side. For a public demo, consider leaving it unset — visitors
  then use the local-simulator mode or paste their own key in the sidebar
  (it is never stored).

---

## Sizing & cost notes

| Setup | Works? | Notes |
|---|---|---|
| Shared / cloud "Web Hosting" | ❌ | PHP-only; cannot run a Python server. |
| VPS KVM 1 (4 GB) | ⚠️ | Fine for IBM-hardware mode; local simulation of big chunks may hit RAM limits. |
| VPS KVM 2 (8 GB) | ✅ | Recommended. Handles 16-qubit local chunks + 500-ticker data comfortably. |

First Docker build: ~3–5 minutes (the qiskit stack is large). Subsequent
rebuilds are much faster thanks to layer caching.
