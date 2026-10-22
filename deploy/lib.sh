# Shared by deploy.sh and deploy/setup-system.sh: the settings, the checks, and the steps
# both of them run. Sourced, not executed.
# shellcheck shell=bash

ZANBEEL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$ZANBEEL_ROOT/deploy"
SERVER_DIR="$ZANBEEL_ROOT/server"
FRONTEND_DIR="$ZANBEEL_ROOT/foxnestFrontend"
EXTENSION_DIR="$ZANBEEL_ROOT/vscode-extension"
VENV_DIR="$ZANBEEL_ROOT/venv"
ECOSYSTEM="$DEPLOY_DIR/ecosystem.config.js"
NGINX_SITE=/etc/nginx/sites-available/zanbeel.conf
NGINX_SITE_LINK=/etc/nginx/sites-enabled/zanbeel.conf
# Builds kept under the web root, the live one included, so an earlier one can be put back.
RELEASES_KEPT=5
# Seconds nginx leaves an instance out after a connection to it fails. deploy.sh waits this
# long (and a second more) after restarting one instance before it restarts the next, so
# nginx never has every instance out at once.
NGINX_FAIL_TIMEOUT=2

# Defaults; deploy/deploy.env overrides them.
ZANBEEL_PUBLIC_PORT=8090
ZANBEEL_API_PORTS="8091 8092 8093 8094"
ZANBEEL_WEB_ROOT=/var/www/zanbeel
ZANBEEL_SERVER_NAME=_
ZANBEEL_TLS_CERT=/etc/nginx/ssl/zanbeel.crt
ZANBEEL_TLS_KEY=/etc/nginx/ssl/zanbeel.key
ZANBEEL_PUBLIC_URL=''
if [ -f "$DEPLOY_DIR/deploy.env" ]; then
  # shellcheck source=deploy.env
  . "$DEPLOY_DIR/deploy.env"
fi
read -r -a API_PORTS <<< "$ZANBEEL_API_PORTS"

if [ -t 1 ]; then
  C_STEP=$'\033[1;36m' C_OK=$'\033[32m' C_WARN=$'\033[1;33m' C_ERR=$'\033[1;31m' C_OFF=$'\033[0m'
else
  C_STEP='' C_OK='' C_WARN='' C_ERR='' C_OFF=''
fi
step() { printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_OFF"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s%s%s\n' "$C_OK" "$*" "$C_OFF"; }
warn() { printf '%s[warn]%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '%s[error]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

validate_config() {
  local port seen=' '
  [ "${#API_PORTS[@]}" -gt 0 ] || die "ZANBEEL_API_PORTS in deploy/deploy.env lists no ports."
  for port in "$ZANBEEL_PUBLIC_PORT" "${API_PORTS[@]}"; do
    [[ $port =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] \
      || die "'$port' in deploy/deploy.env is not a port number."
    [[ $seen != *" $port "* ]] || die "Port $port appears twice in deploy/deploy.env."
    seen+="$port "
  done
  for port in "${API_PORTS[@]}"; do
    [ "$port" -ge 1024 ] || die "Backend port $port is below 1024, which only root may listen on."
  done
  [[ $ZANBEEL_WEB_ROOT =~ ^/[A-Za-z0-9._/-]+$ && $ZANBEEL_WEB_ROOT != / ]] \
    || die "ZANBEEL_WEB_ROOT must be an absolute path of letters, digits, '.', '_', '-' and '/'."
  [[ $ZANBEEL_SERVER_NAME =~ ^[A-Za-z0-9._*\ -]+$ ]] \
    || die "ZANBEEL_SERVER_NAME takes host names separated by spaces, or _ for any."
  local path
  for path in "$ZANBEEL_TLS_CERT" "$ZANBEEL_TLS_KEY"; do
    [[ $path =~ ^/[A-Za-z0-9._/-]+$ ]] \
      || die "ZANBEEL_TLS_CERT and ZANBEEL_TLS_KEY must be absolute paths of letters, digits, '.', '_', '-' and '/'."
  done
  [ "$ZANBEEL_TLS_CERT" != "$ZANBEEL_TLS_KEY" ] || die "ZANBEEL_TLS_CERT and ZANBEEL_TLS_KEY are the same file."
  [[ -z $ZANBEEL_PUBLIC_URL || $ZANBEEL_PUBLIC_URL =~ ^https?://[^[:space:]/]+$ ]] \
    || die "ZANBEEL_PUBLIC_URL must be https://host:port, with no path."
}

# Why the TLS certificate has to be made (again); nothing when it is fine. A self-signed one
# is made again within 30 days of expiring; a CA-issued one is never replaced.
tls_cert_reason() {
  if [ ! -e "$ZANBEEL_TLS_CERT" ] || [ ! -e "$ZANBEEL_TLS_KEY" ]; then
    echo "there is no TLS certificate yet"
  elif tls_cert_self_signed && ! openssl x509 -checkend $((30 * 86400)) -noout -in "$ZANBEEL_TLS_CERT" >/dev/null 2>&1; then
    echo "the self-signed TLS certificate expires within 30 days"
  fi
}

tls_cert_self_signed() {
  [ "$(openssl x509 -noout -subject -nameopt RFC2253 -in "$ZANBEEL_TLS_CERT" 2>/dev/null | sed 's/^subject=//')" \
    = "$(openssl x509 -noout -issuer -nameopt RFC2253 -in "$ZANBEEL_TLS_CERT" 2>/dev/null | sed 's/^issuer=//')" ]
}

# Something is listening on this TCP port, on any address.
port_in_use() { [ -n "$(ss -Htln "sport = :$1" 2>/dev/null)" ]; }

# nginx is running with the Zanbeel site on the public port.
nginx_serves_zanbeel() {
  [ -f "$NGINX_SITE" ] && grep -Eq "^\s*listen\s+$ZANBEEL_PUBLIC_PORT[[:space:];]" "$NGINX_SITE" \
    && systemctl is-active --quiet nginx 2>/dev/null
}

# The nginx site for the current settings, on stdout.
render_nginx_conf() {
  local port api_servers='' docs_servers='' role checks="max_fails=1 fail_timeout=${NGINX_FAIL_TIMEOUT}s"
  for port in "${API_PORTS[@]}"; do
    api_servers+="    server 127.0.0.1:$port $checks;"$'\n'
    # The first instance runs documentation jobs; the others only stand in for it.
    if [ -z "$docs_servers" ]; then role=" $checks"; else role=' backup'; fi
    docs_servers+="    server 127.0.0.1:$port$role;"$'\n'
  done
  API_SERVERS="${api_servers%$'\n'}" DOCS_JOB_SERVERS="${docs_servers%$'\n'}" \
  PUBLIC_PORT="$ZANBEEL_PUBLIC_PORT" WEB_ROOT="$ZANBEEL_WEB_ROOT" SERVER_NAME="$ZANBEEL_SERVER_NAME" \
  TLS_CERT="$ZANBEEL_TLS_CERT" TLS_KEY="$ZANBEEL_TLS_KEY" \
    awk '
      $0 == "__API_SERVERS__"      { print ENVIRON["API_SERVERS"]; next }
      $0 == "__DOCS_JOB_SERVERS__" { print ENVIRON["DOCS_JOB_SERVERS"]; next }
      {
        gsub(/__PUBLIC_PORT__/, ENVIRON["PUBLIC_PORT"])
        gsub(/__WEB_ROOT__/, ENVIRON["WEB_ROOT"])
        gsub(/__SERVER_NAME__/, ENVIRON["SERVER_NAME"])
        gsub(/__TLS_CERT__/, ENVIRON["TLS_CERT"])
        gsub(/__TLS_KEY__/, ENVIRON["TLS_KEY"])
        print
      }' "$DEPLOY_DIR/nginx/zanbeel.conf.template"
}

# server/.env: made from server/.env.example on the first deploy, with new secrets. One
# that exists is never changed.
ensure_server_env() {
  local env_file="$SERVER_DIR/.env" secret setup_key
  if [ -f "$env_file" ]; then
    info "using the existing server/.env"
    grep -q '^FOXNEST_DOCS_DIR=' "$env_file" || warn "server/.env does not set FOXNEST_DOCS_DIR, so generated documents go to /tmp/foxnest_server/docs: cleared on reboot, and shared with any other FoxNest server on this machine."
    return 0
  fi
  secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
  setup_key=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  (
    umask 077
    sed -e "s|^FOXNEST_AUTH_SECRET=.*|FOXNEST_AUTH_SECRET=$secret|" \
        -e "s|^FOXNEST_PASSWORD_SETUP_KEY=.*|FOXNEST_PASSWORD_SETUP_KEY=$setup_key|" \
        "$SERVER_DIR/.env.example" > "$env_file"
    cat >> "$env_file" <<EOF

# --- Production (added by deploy.sh) ---------------------------------------------
# PM2 sets SERVER_HOST, SERVER_PORT and FOXNEST_TRUST_PROXY for each instance
# (deploy/ecosystem.config.js); the values above apply only to server.py run by hand.
# Generated documents are kept with the rest of this server's data, not in /tmp,
# which is cleared on reboot and shared with any other FoxNest server on this machine.
FOXNEST_DOCS_DIR=$SERVER_DIR/generated_docs
EOF
  )
  grep -q "^FOXNEST_AUTH_SECRET=$secret\$" "$env_file" || die "Could not write FOXNEST_AUTH_SECRET into server/.env."
  ok "created server/.env with a new FOXNEST_AUTH_SECRET and FOXNEST_PASSWORD_SETUP_KEY"
  warn "Keep a copy of server/.env somewhere safe, apart from the backups: commits are signed with FOXNEST_AUTH_SECRET."
}

# Copy the build to a new folder under the web root and point "current" at it. The switch
# is a single rename, so nginx serves the old build or the new one, never a mixture.
publish_frontend() {
  local dist="$FRONTEND_DIR/dist" releases="$ZANBEEL_WEB_ROOT/releases" current="$ZANBEEL_WEB_ROOT/current"
  local name target old
  [ -f "$dist/index.html" ] || die "No build to publish: $dist/index.html is missing."
  if [ -d "$current" ] && diff -rq "$dist" "$current" >/dev/null 2>&1; then
    info "the web root already serves this build ($(readlink "$current"))"
    return 0
  fi
  name=$(date +%Y%m%d-%H%M%S)
  target="$releases/$name"
  if [ -e "$target" ]; then name+="-$$"; target="$releases/$name"; fi
  mkdir -p "$target"
  cp -a "$dist/." "$target/"
  chmod -R a+rX "$target"
  ln -sfn "releases/$name" "$ZANBEEL_WEB_ROOT/.current.new"
  mv -Tf "$ZANBEEL_WEB_ROOT/.current.new" "$current"
  ok "published the build: $current -> releases/$name"
  while IFS= read -r old; do
    [ -n "$old" ] && rm -rf -- "${releases:?}/$old"
  done < <(ls -1 "$releases" | sort | head -n -"$RELEASES_KEPT")
}

# Every app PM2 runs, one "name port status" line each (port: the SERVER_PORT it was given).
pm2_apps() {
  pm2 jlist 2>/dev/null | node -e '
    let raw = ""
    process.stdin.on("data", (chunk) => { raw += chunk }).on("end", () => {
      // The list is one line of JSON; PM2 can print notices before it.
      const line = raw.split("\n").reverse().find((l) => l.startsWith("[")) || "[]"
      let apps = []
      try { apps = JSON.parse(line) } catch {}
      for (const app of apps) {
        const env = app.pm2_env || {}
        console.log(app.name, (env.env && env.env.SERVER_PORT) || env.SERVER_PORT || "-", env.status || "-")
      }
    })'
}

# The zanbeel-api-* instances among them. (awk reads to the end: `| grep -q` can stop
# early, and under pipefail the writer's SIGPIPE would read as "not found".)
pm2_instances() { pm2_apps | awk '$1 ~ /^zanbeel-api-[0-9]+$/'; }

# PM2 has an app of this name.
pm2_has() { pm2_apps | awk -v name="$1" '$1 == name { found = 1 } END { exit !found }'; }
