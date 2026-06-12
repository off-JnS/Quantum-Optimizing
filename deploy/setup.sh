#!/usr/bin/env bash
# =============================================================================
# Quantum Portfolio Optimizer — Hostinger VPS setup script
# =============================================================================
# Tested on Ubuntu 22.04 LTS (Hostinger KVM VPS / Cloud Hosting).
# Run as root on a freshly provisioned server:
#
#   DOMAIN=mysite.com bash deploy/setup.sh
#
# What this script does:
#   1. Installs system packages (Python 3.11, Nginx, Certbot, Git)
#   2. Creates a dedicated system user  "quantumapp"
#   3. Copies the app to /opt/quantum-optimizer
#   4. Creates a Python virtualenv and installs all dependencies
#   5. Creates an .env file from .env.example (edit afterwards)
#   6. Installs and starts a systemd service for 24/7 uptime
#   7. Configures Nginx as a reverse proxy
#   8. Obtains a free Let's Encrypt SSL certificate
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
APP_DIR="/opt/quantum-optimizer"
APP_USER="quantumapp"
DOMAIN="${DOMAIN:-yourdomain.com}"   # override with: DOMAIN=mysite.com bash setup.sh
EMAIL="${EMAIL:-admin@${DOMAIN}}"    # used for Let's Encrypt notifications
PYTHON="python3.11"

# ── Colour helpers ────────────────────────────────────────────────────────────
info()  { echo -e "\e[34m[INFO]\e[0m  $*"; }
ok()    { echo -e "\e[32m[ OK ]\e[0m  $*"; }
warn()  { echo -e "\e[33m[WARN]\e[0m  $*"; }
abort() { echo -e "\e[31m[FAIL]\e[0m  $*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || abort "Please run as root (sudo bash deploy/setup.sh)"

# ── 1. System packages ────────────────────────────────────────────────────────
info "Updating package index and installing system dependencies…"
apt-get update -y
apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3.11-dev python3-pip \
    nginx certbot python3-certbot-nginx \
    curl git build-essential gcc g++ gfortran
ok "System packages installed."

# ── 2. Application user ───────────────────────────────────────────────────────
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -s /sbin/nologin -d "$APP_DIR" -m "$APP_USER"
    ok "Created user $APP_USER."
else
    info "User $APP_USER already exists — skipping."
fi

# ── 3. Copy application files ─────────────────────────────────────────────────
info "Deploying application to $APP_DIR…"
mkdir -p "$APP_DIR"

# If running from inside the repo directory, rsync everything over
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ -f "$REPO_ROOT/app.py" ]]; then
    rsync -a --exclude=".git" --exclude=".venv" --exclude="__pycache__" \
        "$REPO_ROOT/" "$APP_DIR/"
    ok "Files copied from $REPO_ROOT."
else
    abort "Cannot find app.py — run this script from inside the repository."
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── 4. Python virtual environment ─────────────────────────────────────────────
info "Creating Python virtual environment…"
sudo -u "$APP_USER" "$PYTHON" -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip wheel

info "Installing Python dependencies (this may take 5–10 minutes)…"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --no-cache-dir \
    -r "$APP_DIR/requirements.txt"
ok "Python dependencies installed."

# ── 5. Environment file ───────────────────────────────────────────────────────
if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    warn ".env created from template. Edit it now:"
    warn "  nano $APP_DIR/.env"
    warn "  → Set IBM_QUANTUM_TOKEN to use real IBM quantum hardware (optional)."
fi

# ── 6. Systemd service ────────────────────────────────────────────────────────
info "Installing systemd service…"
cp "$APP_DIR/deploy/quantum-optimizer.service" /etc/systemd/system/
# Patch the service file so it uses the correct user
sed -i "s|User=quantumapp|User=$APP_USER|; s|Group=quantumapp|Group=$APP_USER|" \
    /etc/systemd/system/quantum-optimizer.service

systemctl daemon-reload
systemctl enable quantum-optimizer
systemctl restart quantum-optimizer
ok "Service started. Check with: systemctl status quantum-optimizer"

# ── 7. Nginx reverse proxy ────────────────────────────────────────────────────
info "Configuring Nginx for $DOMAIN…"
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/quantum-optimizer
sed -i "s/YOURDOMAIN/$DOMAIN/g" /etc/nginx/sites-available/quantum-optimizer

ln -sf /etc/nginx/sites-available/quantum-optimizer \
        /etc/nginx/sites-enabled/quantum-optimizer
rm -f  /etc/nginx/sites-enabled/default

# Temporarily serve HTTP only so Certbot can complete its challenge
cat > /etc/nginx/sites-available/quantum-optimizer-temp <<NGINX
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;
    location / { proxy_pass http://127.0.0.1:8501; }
    location /.well-known/acme-challenge/ { root /var/www/html; }
}
NGINX
ln -sf /etc/nginx/sites-available/quantum-optimizer-temp \
        /etc/nginx/sites-enabled/quantum-optimizer
nginx -t && systemctl reload nginx
ok "Nginx configured (HTTP)."

# ── 8. SSL certificate ────────────────────────────────────────────────────────
info "Obtaining Let's Encrypt SSL certificate for $DOMAIN…"
if certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" \
        --non-interactive --agree-tos --email "$EMAIL" \
        --redirect 2>/dev/null; then
    ok "SSL certificate obtained."
else
    warn "Certbot failed — this usually means DNS for $DOMAIN is not yet pointing"
    warn "to this server. Re-run after DNS propagates:"
    warn "  certbot --nginx -d $DOMAIN -d www.$DOMAIN"
fi

# Install the final (HTTPS) Nginx config
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/quantum-optimizer
sed -i "s/YOURDOMAIN/$DOMAIN/g" /etc/nginx/sites-available/quantum-optimizer
ln -sf /etc/nginx/sites-available/quantum-optimizer \
        /etc/nginx/sites-enabled/quantum-optimizer
nginx -t && systemctl reload nginx

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
ok "═══════════════════════════════════════════════════════"
ok " Quantum Portfolio Optimizer is live!"
ok "═══════════════════════════════════════════════════════"
echo ""
echo "  URL          : https://$DOMAIN"
echo "  App directory: $APP_DIR"
echo "  Logs         : journalctl -u quantum-optimizer -f"
echo "  Service      : systemctl status quantum-optimizer"
echo ""
echo "  Next steps:"
echo "  1. Edit .env to add your IBM Quantum token (optional):"
echo "       nano $APP_DIR/.env"
echo "       systemctl restart quantum-optimizer"
echo ""
echo "  2. Set up automatic certificate renewal (already installed by Certbot):"
echo "       systemctl status certbot.timer"
echo ""
echo "  3. Monitor resources:"
echo "       htop"
