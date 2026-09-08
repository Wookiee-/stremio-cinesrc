#!/usr/bin/env bash
set -euo pipefail

# setup-nginx.sh - Ubuntu host nginx + certbot for cinesrc.ddns.net -> 127.0.0.1:7001 (like stremio-movy)
# Usage:
#   sudo ./setup-nginx.sh --email you@example.com
#   sudo ./setup-nginx.sh --domain cinesrc.ddns.net --email you@example.com --port 7001 --no-certbot

DOMAIN="cinesrc.ddns.net"
EMAIL=""
PORT="7001"
DO_CERTBOT=true
DO_UFW=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2;;
    --email) EMAIL="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --no-certbot) DO_CERTBOT=false; shift;;
    --no-ufw) DO_UFW=false; shift;;
    -h|--help)
      echo "Usage: sudo $0 [--domain DOMAIN] [--email EMAIL] [--port PORT] [--no-certbot] [--no-ufw]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "[!] Run as root: sudo $0 --email you@example.com" >&2
  exit 1
fi

echo "[*] Domain: $DOMAIN"
echo "[*] Port:   $PORT"
echo "[*] Installing nginx + certbot..."
apt update
apt install -y nginx certbot python3-certbot-nginx

if [[ "$DO_UFW" == true ]] && command -v ufw >/dev/null 2>&1; then
  ufw allow 'Nginx Full' || true
  ufw allow OpenSSH || true
fi

NGINX_CONF="/etc/nginx/sites-available/${DOMAIN}"
echo "[*] Writing ${NGINX_CONF}..."
cp "$(dirname "$0")/nginx/cinesrc.conf" "$NGINX_CONF"
sed -i "s/cinesrc.ddns.net/${DOMAIN}/g; s/127.0.0.1:7001/127.0.0.1:${PORT}/g" "$NGINX_CONF"

ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/${DOMAIN}"
nginx -t
systemctl reload nginx || systemctl restart nginx
systemctl enable nginx

if [[ "$DO_CERTBOT" == true ]]; then
  if [[ -z "$EMAIL" ]]; then
    certbot --nginx -d "$DOMAIN" --agree-tos --redirect --register-unsafely-without-email --non-interactive || {
      echo "[!] certbot failed - check DNS A record for ${DOMAIN} points to this server and port 80 is open" >&2; exit 1; }
  else
    certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --redirect --non-interactive || {
      echo "[!] certbot failed - check DNS A record for ${DOMAIN} points to this server and port 80 is open" >&2; exit 1; }
  fi
  certbot renew --dry-run || true
fi

echo "[✓] Done! https://${DOMAIN}/manifest.json -> http://127.0.0.1:${PORT}"
