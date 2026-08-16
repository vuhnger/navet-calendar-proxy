#!/usr/bin/env bash
# Privileged half of the CI deploy, installed to /usr/local/bin/navet-ics-deploy.
#
# CI can write to the staging directory but cannot modify this script (root-owned,
# not writable by the deploy account), and the deploy account's sudo rule grants
# exactly this one command. So a compromised CI token can ship application code —
# which it could anyway — but cannot run arbitrary commands as root.
set -euo pipefail

STAGING=/srv/navet-ics/staging
APP_DIR=/opt/navet-ics
APP_USER=navet-ics
HEALTH_URL=http://127.0.0.1:8000/readyz

log() { printf '==> %s\n' "$*"; }

# --- validate the payload before touching the running install ----------------
for required in \
    "$STAGING/src/navet_ics/app.py" \
    "$STAGING/pyproject.toml" \
    "$STAGING/uv.lock" \
    "$STAGING/README.md"; do
    if [[ ! -f "$required" ]]; then
        echo "refusing to deploy: missing $required" >&2
        exit 1
    fi
done

log "Snapshotting current release for rollback"
ROLLBACK="$(mktemp -d /srv/navet-ics/rollback.XXXXXX)"
cp -a "$APP_DIR/src" "$ROLLBACK/src"
cp -a "$APP_DIR/pyproject.toml" "$APP_DIR/uv.lock" "$ROLLBACK/"

rolled_back=0
rollback() {
    # The ERR trap can fire again from inside this function; only run it once.
    [[ $rolled_back -eq 1 ]] && exit 1
    rolled_back=1
    trap - ERR
    log "Deploy failed, rolling back"
    rm -rf "$APP_DIR/src"
    cp -a "$ROLLBACK/src" "$APP_DIR/src"
    cp -a "$ROLLBACK/pyproject.toml" "$ROLLBACK/uv.lock" "$APP_DIR/"
    ( cd "$APP_DIR" && uv sync --frozen --no-dev --quiet ) || true
    chown -R root:"$APP_USER" "$APP_DIR" || true
    systemctl restart navet-ics.service || true
    rm -rf "$ROLLBACK"
    exit 1
}
trap rollback ERR

log "Installing new release"
rm -rf "$APP_DIR/src"
cp -r "$STAGING/src" "$APP_DIR/src"
cp "$STAGING/pyproject.toml" "$STAGING/uv.lock" "$STAGING/README.md" "$APP_DIR/"

log "Syncing dependencies from uv.lock"
# --frozen refuses to re-resolve, so the server gets exactly what CI tested.
( cd "$APP_DIR" && uv sync --frozen --no-dev --quiet )

chown -R root:"$APP_USER" "$APP_DIR"
chmod -R go-w "$APP_DIR"

# systemd and nginx config change rarely, but a deploy should still pick them up
# rather than needing a manual step.
if ! cmp -s "$STAGING/deploy/navet-ics.service" /etc/systemd/system/navet-ics.service; then
    log "Updating systemd unit"
    cp "$STAGING/deploy/navet-ics.service" /etc/systemd/system/navet-ics.service
    systemctl daemon-reload
fi

# Note what is *not* here: deploy/nginx.conf. Certbot rewrites the site file in
# place, so copying ours over it would destroy the TLS configuration. Both
# snippets below are ours alone, which is why routes live in one of them.
nginx_dirty=0
for snippet in navet-ics-proxy.conf navet-ics-locations.conf; do
    if ! cmp -s "$STAGING/deploy/$snippet" "/etc/nginx/snippets/$snippet"; then
        log "Updating nginx snippet $snippet"
        cp "$STAGING/deploy/$snippet" "/etc/nginx/snippets/$snippet"
        nginx_dirty=1
    fi
done
if [[ $nginx_dirty -eq 1 ]]; then
    # A snippet that does not parse must not take the site down: nginx -t fails,
    # the ERR trap rolls the release back, and the running nginx keeps its old
    # config because reload never happens.
    nginx -t
    systemctl reload nginx
fi

log "Restarting service"
systemctl restart navet-ics.service

# The unit reports "active" as soon as the process starts, but the feed is only
# usable once the first upstream fetch has completed. Wait for the real signal.
log "Waiting for readiness"
status=none
for attempt in $(seq 1 30); do
    status="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$HEALTH_URL" || echo 000)"
    if [[ "$status" == "200" ]]; then
        log "Ready after ${attempt} attempt(s)"
        trap - ERR
        rm -rf "$ROLLBACK"
        log "Deploy complete"
        exit 0
    fi
    sleep 2
done

echo "service never became ready (last status: $status)" >&2
rollback
