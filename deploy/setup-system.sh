#!/usr/bin/env bash
#
# The part of a Zanbeel deploy that needs root: nginx, the web root it serves, and PM2
# starting at boot. Safe to run again; ./deploy.sh runs it (with sudo) whenever any of
# this is missing or out of date.
#
#   sudo ./deploy/setup-system.sh
#
#   1. Installs nginx if needed, and switches off its stock site on port 80.
#   2. Creates the web root (ZANBEEL_WEB_ROOT), owned by the app's user so that deploys
#      need no root, and publishes the current build when there is one.
#   3. Makes a self-signed TLS certificate (ZANBEEL_TLS_CERT and ZANBEEL_TLS_KEY) when
#      there is none, or when the self-signed one is about to expire.
#   4. Writes the Zanbeel site from deploy/nginx/zanbeel.conf.template, checks it with
#      nginx -t (putting the previous one back if that fails) and reloads nginx.
#   5. Opens the public port in ufw, when ufw is active.
#   6. Registers PM2 with systemd, so the saved backend instances start at boot.

set -Eeuo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ "$(id -u)" -eq 0 ] || die "This needs root:  sudo $0"
validate_config

# The user that owns and runs the app: whoever called sudo, else the checkout's owner.
APP_USER=${SUDO_USER:-}
if [ -z "$APP_USER" ] || [ "$APP_USER" = root ]; then APP_USER=$(stat -c %U "$ZANBEEL_ROOT"); fi
[ "$APP_USER" != root ] || die "Cannot tell which user runs the app: run this with sudo from that user's account."
APP_GROUP=$(id -gn "$APP_USER")
APP_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)

# ------------------------------------------------------------------------------------
step "nginx"
if ! command -v nginx >/dev/null 2>&1; then
  info "installing nginx"
  # The package starts nginx at once, with its stock site on port 80. Where another web
  # server already holds port 80 (Apache does on this server) that start fails and takes
  # the install down with it, so the start waits until the stock site is out of the way.
  held_start=0
  if [ ! -e /usr/sbin/policy-rc.d ]; then
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
    chmod 755 /usr/sbin/policy-rc.d
    held_start=1
    trap 'rm -f /usr/sbin/policy-rc.d' EXIT
  fi
  apt-get update -qq
  # Ubuntu's needrestart runs after every install and, run from apt, restarts on its own
  # every service still using a library replaced by any earlier upgrade. Other projects
  # run on this server, so this install restarts nothing.
  NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx
  if [ "$held_start" = 1 ]; then
    rm -f /usr/sbin/policy-rc.d
    trap - EXIT
  fi
fi
info "$(nginx -v 2>&1)"

# The stock site listens on port 80 on every address; with another web server on port 80,
# nginx would not start at all. Its file stays in sites-available.
if [ "$(readlink /etc/nginx/sites-enabled/default 2>/dev/null)" = /etc/nginx/sites-available/default ]; then
  rm -f /etc/nginx/sites-enabled/default
  info "switched off nginx's stock site on port 80"
fi

# ------------------------------------------------------------------------------------
step "Web root $ZANBEEL_WEB_ROOT"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 755 "$ZANBEEL_WEB_ROOT" "$ZANBEEL_WEB_ROOT/releases"
ok "owned by $APP_USER, so deploys publish to it without root"
if [ -f "$FRONTEND_DIR/dist/index.html" ]; then
  sudo -u "$APP_USER" bash -c 'set -Eeuo pipefail; . "$1"; publish_frontend' _ "$DEPLOY_DIR/lib.sh"
elif [ ! -e "$ZANBEEL_WEB_ROOT/current" ]; then
  warn "There is no build yet; ./deploy.sh makes one and publishes it."
fi
nginx_user=$(awk '$1 == "user" { sub(/;$/, "", $2); print $2; exit }' /etc/nginx/nginx.conf)
if [ -e "$ZANBEEL_WEB_ROOT/current/index.html" ] && [ -n "$nginx_user" ] \
   && ! sudo -u "$nginx_user" test -r "$ZANBEEL_WEB_ROOT/current/index.html"; then
  warn "nginx runs as $nginx_user, which cannot read $ZANBEEL_WEB_ROOT/current: pick a ZANBEEL_WEB_ROOT outside home directories."
fi

# ------------------------------------------------------------------------------------
step "TLS certificate"

# Every name and address clients may use for this server, as subjectAltName entries:
# browsers and TLS libraries check the address they connected to against these.
tls_subject_alt_names() {
  local -A seen=()
  local entries=() name host
  host=${ZANBEEL_PUBLIC_URL#*://}
  host=${host%%/*}
  if [[ $host == \[* ]]; then host=${host#[}; host=${host%%]*}; else host=${host%:*}; fi
  # shellcheck disable=SC2046  # hostname -I prints addresses separated by spaces
  for name in $host $(hostname -I 2>/dev/null) 127.0.0.1 ::1 \
              "$(hostname -f 2>/dev/null)" "$(hostname -s 2>/dev/null)" localhost $ZANBEEL_SERVER_NAME; do
    [ -n "$name" ] && [ "$name" != _ ] && [ -z "${seen[$name]:-}" ] || continue
    seen[$name]=1
    if [[ $name =~ ^[0-9.]+$ || $name == *:* ]]; then entries+=("IP:$name"); else entries+=("DNS:$name"); fi
  done
  local IFS=,
  echo "${entries[*]}"
}

reason=$(tls_cert_reason)
if [ -z "$reason" ]; then
  if tls_cert_self_signed; then kind="self-signed"; else kind="CA-issued"; fi
  ok "using the $kind certificate $ZANBEEL_TLS_CERT, valid until $(openssl x509 -noout -enddate -in "$ZANBEEL_TLS_CERT" | cut -d= -f2)"
else
  info "$reason: making a self-signed one"
  san=$(tls_subject_alt_names)
  cnf=$(mktemp)
  # basicConstraints CA:FALSE: Firefox will not accept a certificate marked as a CA for a
  # web server at all, even with an exception. 825 days is the longest validity macOS
  # and iOS accept for a server certificate, even one the user trusts.
  cat > "$cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = server
prompt = no
[dn]
CN = Zanbeel $(hostname -s)
O = Zanbeel
[server]
subjectAltName = $san
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
EOF
  install -d -m 755 "$(dirname "$ZANBEEL_TLS_CERT")" "$(dirname "$ZANBEEL_TLS_KEY")"
  # Made beside the live pair and moved over it, so nginx never reads half a file.
  if ! out=$(umask 077
             openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 825 -config "$cnf" \
               -keyout "$ZANBEEL_TLS_KEY.new" -out "$ZANBEEL_TLS_CERT.new" 2>&1); then
    rm -f "$cnf" "$ZANBEEL_TLS_KEY.new" "$ZANBEEL_TLS_CERT.new"
    die "openssl could not make the certificate:
$out"
  fi
  rm -f "$cnf"
  chmod 600 "$ZANBEEL_TLS_KEY.new"
  chmod 644 "$ZANBEEL_TLS_CERT.new"
  mv -f "$ZANBEEL_TLS_KEY.new" "$ZANBEEL_TLS_KEY"
  mv -f "$ZANBEEL_TLS_CERT.new" "$ZANBEEL_TLS_CERT"
  ok "made $ZANBEEL_TLS_CERT for ${san//,/ }"
  info "valid until $(openssl x509 -noout -enddate -in "$ZANBEEL_TLS_CERT" | cut -d= -f2); SHA-256 fingerprint:"
  info "$(openssl x509 -noout -fingerprint -sha256 -in "$ZANBEEL_TLS_CERT" | cut -d= -f2)"
  warn "Browsers warn about a self-signed certificate until it is trusted on each computer; clients can download it from /zanbeel.crt."
fi

# ------------------------------------------------------------------------------------
step "nginx site on port $ZANBEEL_PUBLIC_PORT"
if port_in_use "$ZANBEEL_PUBLIC_PORT" && [[ $(ss -Htlnp "sport = :$ZANBEEL_PUBLIC_PORT") != *'"nginx"'* ]]; then
  die "Port $ZANBEEL_PUBLIC_PORT is in use by another program. Choose another ZANBEEL_PUBLIC_PORT in deploy/deploy.env.
$(ss -Htlnp "sport = :$ZANBEEL_PUBLIC_PORT")"
fi
rendered=$(mktemp)
render_nginx_conf > "$rendered"
previous=''
if [ -f "$NGINX_SITE" ]; then
  previous=$(mktemp)
  cp -p "$NGINX_SITE" "$previous"
fi
install -m 644 "$rendered" "$NGINX_SITE"
rm -f "$rendered"
ln -sfn "$NGINX_SITE" "$NGINX_SITE_LINK"
if ! test_output=$(nginx -t 2>&1); then
  if [ -n "$previous" ]; then cp -p "$previous" "$NGINX_SITE"; else rm -f "$NGINX_SITE" "$NGINX_SITE_LINK"; fi
  die "nginx rejected the new site, and the previous configuration is back in place:
$test_output"
fi
[ -z "$previous" ] || rm -f "$previous"
systemctl enable nginx >/dev/null 2>&1 || true
if systemctl is-active --quiet nginx; then systemctl reload nginx; else systemctl start nginx; fi
ok "nginx serves $NGINX_SITE"

if command -v ufw >/dev/null 2>&1 && [[ $(ufw status 2>/dev/null) == 'Status: active'* ]]; then
  ufw allow "$ZANBEEL_PUBLIC_PORT/tcp" comment 'Zanbeel (nginx)' >/dev/null
  ok "ufw allows $ZANBEEL_PUBLIC_PORT/tcp"
fi

# ------------------------------------------------------------------------------------
step "PM2 at boot"
pm2_bin=''
for candidate in "$APP_HOME/.local/bin/pm2" /usr/local/bin/pm2 /usr/bin/pm2; do
  if [ -x "$candidate" ]; then pm2_bin=$candidate; break; fi
done
if [ -z "$pm2_bin" ]; then
  warn "PM2 is not installed yet. ./deploy.sh installs it, then runs this again to finish."
else
  # pm2 is a node script: the unit it writes runs with this PATH.
  env PATH="$(dirname "$(readlink -f "$(command -v node)")"):$PATH" \
    "$pm2_bin" startup systemd -u "$APP_USER" --hp "$APP_HOME" >/dev/null
  ok "pm2-$APP_USER.service is enabled: the instances saved with 'pm2 save' start again after a reboot"
fi

host=$(hostname -I 2>/dev/null | awk '{print $1}')
printf '\nZanbeel: %s\n' "${ZANBEEL_PUBLIC_URL:-https://${host:-$(hostname)}:$ZANBEEL_PUBLIC_PORT}"
