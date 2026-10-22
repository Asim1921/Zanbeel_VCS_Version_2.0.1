#!/usr/bin/env bash
#
# Zanbeel on a Linux server: the production counterpart of run-dev.ps1.
#
#   ./deploy.sh        build everything and (re)start it; run it again for every update
#
#   1. Backend   ./venv with the backend's requirements, and server/.env on the first
#                run, with new secrets.
#   2. Frontend  npm ci and npm run build, the page calling /api on whatever address
#                served it.
#   3. PM2       one backend instance per port in deploy/deploy.env, restarted one at a
#                time, each answering again before the next goes down.
#   4. nginx     serves the build over HTTPS and balances /api across the instances, on
#                ZANBEEL_PUBLIC_PORT. Installing or reconfiguring it (and making the TLS
#                certificate) takes root, so that is deploy/setup-system.sh, run from here
#                with sudo when it is needed.
#
# Ports and paths: deploy/deploy.env. Run as the user that owns the app, not as root.

set -Eeuo pipefail
# shellcheck source=deploy/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/deploy/lib.sh"

[ "$(id -u)" -ne 0 ] || die "Run ./deploy.sh as the user that owns the app, not as root. It asks for sudo itself when nginx needs setting up."
validate_config
cd "$ZANBEEL_ROOT"

# Wait for an instance to answer its health check (GET /), for up to $2 seconds.
wait_healthy() {
  local deadline=$((SECONDS + $2))
  until curl -fs -o /dev/null --max-time 3 "http://127.0.0.1:$1/"; do
    [ "$SECONDS" -lt "$deadline" ] || return 1
    sleep 1
  done
}

wait_port_free() {
  local deadline=$((SECONDS + $2))
  while port_in_use "$1"; do
    [ "$SECONDS" -lt "$deadline" ] || return 1
    sleep 1
  done
}

# Why deploy/setup-system.sh has to run; nothing when all it does is in place.
root_setup_reason() {
  if ! command -v nginx >/dev/null 2>&1; then
    echo "nginx is not installed"
  elif [ ! -f "$NGINX_SITE" ] || [ ! -L "$NGINX_SITE_LINK" ]; then
    echo "nginx has no Zanbeel site yet"
  elif ! cmp -s <(render_nginx_conf) "$NGINX_SITE"; then
    echo "the nginx site does not match deploy/deploy.env"
  elif [ -n "$(tls_cert_reason)" ]; then
    tls_cert_reason
  elif [ ! -w "$ZANBEEL_WEB_ROOT/releases" ]; then
    echo "the web root $ZANBEEL_WEB_ROOT is not set up"
  elif ! systemctl is-active --quiet nginx; then
    echo "nginx is not running"
  elif ! systemctl is-enabled --quiet "pm2-$(id -un)" 2>/dev/null; then
    echo "PM2 is not set to start at boot"
  fi
}

# ------------------------------------------------------------------------------------
step "Checking the tools"
for tool in node npm python3 curl ss openssl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed."
done
node -e 'const [a, b] = process.versions.node.split(".").map(Number)
         process.exit((a === 20 && b >= 19) || (a === 22 && b >= 12) || a >= 23 ? 0 : 1)' \
  || die "Node $(node -v) is too old for Vite 7, which needs 20.19+ or 22.12+."
info "node $(node -v), npm $(npm -v), $(python3 --version)"

if ! command -v pm2 >/dev/null 2>&1; then
  if [ -w "$(npm config get prefix)/lib/node_modules" ]; then
    npm install -g pm2 --no-audit --no-fund
  else
    # No root needed: PM2 goes in ~/.local, whose bin/ is on the PATH.
    info "installing PM2 into ~/.local"
    npm install -g pm2 --prefix "$HOME/.local" --no-audit --no-fund
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
command -v pm2 >/dev/null 2>&1 || die "PM2 did not install."
if ! pm2_has pm2-logrotate; then
  info "installing pm2-logrotate, so the instances' logs do not grow without limit"
  pm2 install pm2-logrotate >/dev/null
  pm2 set pm2-logrotate:max_size 50M >/dev/null
  pm2 set pm2-logrotate:retain 14 >/dev/null
  pm2 set pm2-logrotate:compress true >/dev/null
fi
info "PM2 $(pm2 -v | tail -n 1)"

# ------------------------------------------------------------------------------------
step "Checking the ports"
# A port already in use is fine only when it is one of these instances.
declare -A ours=()
while read -r name port status; do
  [ "$status" = online ] && ours[$port]=$name
done < <(pm2_instances)
for port in "${API_PORTS[@]}"; do
  if port_in_use "$port" && [ -z "${ours[$port]:-}" ]; then
    die "Port $port is in use by another program. Choose other ports for ZANBEEL_API_PORTS in deploy/deploy.env.
$(ss -Htlnp "sport = :$port" 2>/dev/null)"
  fi
done
if port_in_use "$ZANBEEL_PUBLIC_PORT" && ! nginx_serves_zanbeel; then
  die "Port $ZANBEEL_PUBLIC_PORT is in use by another program. Choose another ZANBEEL_PUBLIC_PORT in deploy/deploy.env."
fi
ok "public :$ZANBEEL_PUBLIC_PORT (nginx), backends 127.0.0.1:${API_PORTS[*]}"

# ------------------------------------------------------------------------------------
step "Backend: Python packages"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  info "creating venv/"
  python3 -m venv "$VENV_DIR"
fi
# docs/requirements.txt is the complete list (the upload routes need its python-multipart);
# run-dev.ps1 prefers it for the same reason.
requirements="$ZANBEEL_ROOT/docs/requirements.txt"
[ -f "$requirements" ] || requirements="$ZANBEEL_ROOT/requirements.txt"
"$VENV_DIR/bin/python" -m pip install --quiet --disable-pip-version-check -r "$requirements"
ok "installed ${requirements#"$ZANBEEL_ROOT/"} into venv/"

step "Backend: server/.env"
ensure_server_env

# Import the application before stopping anything: a missing package or a bad setting
# fails here, while the running instances are still serving.
( cd "$SERVER_DIR" && "$VENV_DIR/bin/python" -c 'import server' >/dev/null ) \
  || die "The backend does not import (see above); nothing was restarted."
ok "the backend imports cleanly"

# ------------------------------------------------------------------------------------
step "Frontend: npm ci && npm run build"
(
  cd "$FRONTEND_DIR"
  # --include=dev: Vite is a devDependency, which NODE_ENV=production would leave out.
  npm ci --include=dev --no-audit --no-fund
  # Same origin: the page calls /api on whatever address served it and nginx passes that
  # on, so no address or port of this server is built into the bundle. Rollup's notice
  # for each library "use client" line it drops is noise in a browser-only build.
  VITE_API_URL=/api VITE_API_SERVER_URL= npm run build 2>&1 \
    | { grep -v 'Module level directives cause errors when bundled' || true; }
)
[ -f "$FRONTEND_DIR/dist/index.html" ] || die "npm run build produced no dist/index.html."
ok "built foxnestFrontend/dist"

# ------------------------------------------------------------------------------------
step "Downloads: the VS Code extension and the fox CLI"
# Published with the web app under /downloads/, so developers install from the server
# itself. The extension carries the server's certificate, so it is packaged here with
# the one nginx is serving now, and a renewed certificate reaches it on the next deploy.
downloads="$FRONTEND_DIR/dist/downloads"
mkdir -p "$downloads"
if [ -r "$ZANBEEL_TLS_CERT" ]; then
  cp "$ZANBEEL_TLS_CERT" "$EXTENSION_DIR/certs/zanbeel.crt"
fi
extension_version=$(node -p 'require(process.argv[1]).version' "$EXTENSION_DIR/package.json")
(
  cd "$EXTENSION_DIR"
  npm ci --include=dev --no-audit --no-fund >/dev/null
  npx vsce package --allow-missing-repository --skip-license \
    --out "$downloads/zanbeel-vcs-$extension_version.vsix" >/dev/null
)
# The same file under a name that does not change, for links in documents.
cp "$downloads/zanbeel-vcs-$extension_version.vsix" "$downloads/zanbeel-vcs.vsix"
cp "$ZANBEEL_ROOT/release/fox.py" "$downloads/fox.py"
ok "downloads/zanbeel-vcs.vsix (version $extension_version) and downloads/fox.py"

# ------------------------------------------------------------------------------------
step "PM2: backend instances, one at a time"
wanted=()
for i in "${!API_PORTS[@]}"; do
  name="zanbeel-api-$((i + 1))"
  port=${API_PORTS[$i]}
  wanted+=("$name")
  if pm2_has "$name"; then
    pm2 delete "$name" >/dev/null
  fi
  wait_port_free "$port" 60 || die "Port $port was still in use a minute after $name stopped."
  pm2 start "$ECOSYSTEM" --only "$name" >/dev/null
  if wait_healthy "$port" 180; then
    ok "$name answers on 127.0.0.1:$port"
  else
    pm2 logs "$name" --lines 40 --nostream || true
    die "$name did not answer on 127.0.0.1:$port within 3 minutes (its log is above). The instances after it were left running as they were."
  fi
  # nginx may have left this instance out while it was down. Give it time to take it back
  # before the next one goes down, so there is always an instance to send requests to.
  if [ $((i + 1)) -lt "${#API_PORTS[@]}" ]; then
    sleep $((NGINX_FAIL_TIMEOUT + 1))
  fi
done
# Instances whose ports were taken out of deploy/deploy.env.
while read -r name _; do
  if [[ " ${wanted[*]} " != *" $name "* ]]; then
    pm2 delete "$name" >/dev/null
    info "removed $name"
  fi
done < <(pm2_instances)
pm2 save >/dev/null
ok "saved the PM2 process list"

# ------------------------------------------------------------------------------------
step "Frontend: publishing the build"
if [ -w "$ZANBEEL_WEB_ROOT/releases" ]; then
  publish_frontend
else
  info "$ZANBEEL_WEB_ROOT is not set up yet; deploy/setup-system.sh creates it and publishes this build"
fi

# ------------------------------------------------------------------------------------
step "nginx"
root_pending=''
reason=$(root_setup_reason)
if [ -z "$reason" ]; then
  ok "nginx serves Zanbeel on port $ZANBEEL_PUBLIC_PORT, and PM2 starts at boot"
elif [ -t 0 ] || sudo -n true 2>/dev/null; then
  info "$reason: running deploy/setup-system.sh with sudo"
  sudo bash "$DEPLOY_DIR/setup-system.sh" || die "deploy/setup-system.sh failed (see above)."
else
  root_pending=$reason
  warn "$reason. That part needs root:  sudo $DEPLOY_DIR/setup-system.sh"
fi

# ------------------------------------------------------------------------------------
step "Checking it end to end"
for i in "${!API_PORTS[@]}"; do
  port=${API_PORTS[$i]}
  if curl -fs -o /dev/null --max-time 5 "http://127.0.0.1:$port/"; then
    ok "zanbeel-api-$((i + 1))  127.0.0.1:$port  healthy"
  else
    warn "zanbeel-api-$((i + 1)) on 127.0.0.1:$port is not answering: pm2 logs zanbeel-api-$((i + 1))"
  fi
done
if nginx_serves_zanbeel; then
  # -k: this checks that nginx serves, not that this machine trusts the certificate (a
  # CA-issued one need not name 127.0.0.1).
  base="https://127.0.0.1:$ZANBEEL_PUBLIC_PORT"
  if [[ $(curl -kfs --max-time 5 "$base/") == *'<div id="root">'* ]]; then ok "web app        $base/"; else warn "the web app is not being served at $base/"; fi
  if curl -kfs -o /dev/null --max-time 5 "$base/api/"; then ok "API            $base/api/"; else warn "the API does not answer through nginx at $base/api/"; fi
  if curl -kfs -o /dev/null --max-time 5 "$base/healthz"; then ok "health check   $base/healthz"; else warn "$base/healthz does not answer"; fi
  redirect=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 5 "http://127.0.0.1:$ZANBEEL_PUBLIC_PORT/")
  if [[ $redirect == "301 https://"* ]]; then ok "plain HTTP     redirected to https"; else warn "plain HTTP on port $ZANBEEL_PUBLIC_PORT is not redirected to https ($redirect)"; fi
fi
if [ -r "$ZANBEEL_TLS_CERT" ] && ! openssl x509 -checkend $((30 * 86400)) -noout -in "$ZANBEEL_TLS_CERT" >/dev/null 2>&1; then
  warn "The TLS certificate $ZANBEEL_TLS_CERT expires within 30 days: $(openssl x509 -noout -enddate -in "$ZANBEEL_TLS_CERT" | cut -d= -f2)"
fi
pm2 ls

host=$(hostname -I 2>/dev/null | awk '{print $1}')
url=${ZANBEEL_PUBLIC_URL:-"https://${host:-$(hostname)}:$ZANBEEL_PUBLIC_PORT"}
cat <<EOF

Zanbeel
  Web app    $url
  API        $url/api/   (nginx, least connections across ${#API_PORTS[@]} instances)
  API docs   $url/api-docs
  fox CLI    FOXNEST_SERVER=$url   or   fox init --server $url
  VS Code    $url/downloads/zanbeel-vcs.vsix   (Extensions view > ... > Install from VSIX)
  fox CLI    $url/downloads/fox.py
  TLS cert   $url/zanbeel.crt   (trust it on each client while it is self-signed)
  Backends   127.0.0.1:${API_PORTS[*]}   (PM2: zanbeel-api-1 .. zanbeel-api-${#API_PORTS[@]})

  pm2 ls   pm2 logs   pm2 monit      ./deploy.sh deploys again
EOF
if [ -n "$root_pending" ]; then
  warn "Not served yet: $root_pending. Finish with:  sudo $DEPLOY_DIR/setup-system.sh"
fi
