#!/usr/bin/env bash
# Provisions the Navet calendar proxy on a Debian host. Idempotent: safe to re-run.
#
# Usage: sudo ./install.sh <server-name>
set -euo pipefail

SERVER_NAME="${1:-}"
if [[ -z "$SERVER_NAME" ]]; then
    echo "usage: $0 <server-name>   e.g. $0 navet.vuhnger.dev" >&2
    exit 1
fi

APP_USER=navet-ics
APP_DIR=/opt/navet-ics
ENV_DIR=/etc/navet-ics
STAGING=/srv/navet-ics/staging
DEPLOY_USER=deploy
# uv is not packaged in Debian. Installing it from PyPI at a pinned version keeps
# the trust anchor identical to every other dependency, unlike `curl | sh`.
UV_VERSION=0.11.30
UV_HOME=/opt/uv

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv nginx certbot python3-certbot-nginx rsync curl

echo "==> Installing uv $UV_VERSION"
if [[ "$("$UV_HOME/bin/uv" --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]]; then
    rm -rf "$UV_HOME"
    python3 -m venv "$UV_HOME"
    "$UV_HOME/bin/pip" install --quiet --upgrade pip
    "$UV_HOME/bin/pip" install --quiet "uv==$UV_VERSION"
fi
ln -sf "$UV_HOME/bin/uv" /usr/local/bin/uv

echo "==> Creating service account"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    # System account: no login shell, no home directory to write into.
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> Installing application to $APP_DIR"
install -d -o root -g "$APP_USER" -m 0750 "$APP_DIR"
rm -rf "$APP_DIR/src"
cp -r "$SRC_DIR/src" "$APP_DIR/src"
# uv.lock pins the full dependency graph; pyproject.toml references README.md.
cp "$SRC_DIR/pyproject.toml" "$SRC_DIR/uv.lock" "$SRC_DIR/README.md" "$APP_DIR/"

echo "==> Syncing dependencies from uv.lock"
# --frozen fails rather than silently re-resolving, so the server matches CI exactly.
( cd "$APP_DIR" && uv sync --frozen --no-dev --quiet )

# The service account only needs to read the code, never to modify it.
chown -R root:"$APP_USER" "$APP_DIR"
chmod -R go-w "$APP_DIR"

echo "==> Writing configuration"
install -d -o root -g "$APP_USER" -m 0750 "$ENV_DIR"
if [[ ! -f "$ENV_DIR/navet-ics.env" ]]; then
    cp "$SRC_DIR/.env.example" "$ENV_DIR/navet-ics.env"
fi
chown root:"$APP_USER" "$ENV_DIR/navet-ics.env"
chmod 0640 "$ENV_DIR/navet-ics.env"

echo "==> Installing systemd unit"
cp "$SRC_DIR/deploy/navet-ics.service" /etc/systemd/system/navet-ics.service
systemctl daemon-reload
systemctl enable navet-ics.service
systemctl restart navet-ics.service

echo "==> Configuring nginx"
install -d -m 0755 /etc/nginx/snippets /var/www/html
cp "$SRC_DIR/deploy/navet-ics-proxy.conf" /etc/nginx/snippets/navet-ics-proxy.conf
# Never overwrite an existing site file: certbot edits it in place to add TLS.
if [[ ! -f /etc/nginx/sites-available/navet-ics.conf ]]; then
    sed "s/SERVER_NAME_PLACEHOLDER/$SERVER_NAME/g" "$SRC_DIR/deploy/nginx.conf" \
        > /etc/nginx/sites-available/navet-ics.conf
    ln -sf /etc/nginx/sites-available/navet-ics.conf /etc/nginx/sites-enabled/navet-ics.conf
    rm -f /etc/nginx/sites-enabled/default
fi
nginx -t
systemctl enable nginx
systemctl reload nginx

echo "==> Setting up the CI deploy path"
if ! id -u "$DEPLOY_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$DEPLOY_USER"
fi
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0755 "$STAGING"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0700 "/home/$DEPLOY_USER/.ssh"

# Root-owned and not writable by the deploy account, so CI cannot alter what it
# is allowed to run as root.
install -o root -g root -m 0755 "$SRC_DIR/deploy/server-deploy.sh" /usr/local/bin/navet-ics-deploy

# A single-command sudo rule: this is the only thing CI may do as root.
cat > /etc/sudoers.d/navet-ics-deploy <<EOF
$DEPLOY_USER ALL=(root) NOPASSWD: /usr/local/bin/navet-ics-deploy
EOF
chmod 0440 /etc/sudoers.d/navet-ics-deploy
visudo -cf /etc/sudoers.d/navet-ics-deploy

echo
echo "==> Done. Service status:"
systemctl --no-pager --lines=5 status navet-ics.service || true
echo
echo "If this host has no certificate yet:"
echo "  sudo certbot --nginx -d $SERVER_NAME --redirect --agree-tos -m <email> --no-eff-email"
