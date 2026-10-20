#!/usr/bin/env python3
"""Move initialized KaiForward update hosting to its subdomain, preserving old clients."""
from datetime import datetime, timezone
import argparse
import os
from pathlib import Path
import socket
import subprocess

DOMAIN = 'kaiforward.permafrost06.com'
ROOT = Path('/srv/kaiforward-updates')
SITE = Path('/etc/nginx/sites-available') / DOMAIN
ENABLED = Path('/etc/nginx/sites-enabled') / DOMAIN
LEGACY = Path('/etc/nginx/snippets/kaiforward-updates.conf')
CHALLENGES = Path('/var/lib/kaiforward-acme')
CERTIFICATE = Path('/etc/letsencrypt/live') / DOMAIN
RENEW_HOOK = Path('/etc/letsencrypt/renewal-hooks/deploy/kaiforward-nginx')
MANAGED = '# Managed by KaiForward subdomain migration.'


def http_config():
    return f'''{MANAGED}
server {{
    listen 80;
    listen [::]:80;
    server_name {DOMAIN};
    location ^~ /.well-known/acme-challenge/ {{
        root {CHALLENGES};
        default_type text/plain;
    }}
    location / {{ return 301 https://{DOMAIN}$request_uri; }}
}}
'''


def https_config():
    return http_config() + f'''
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {DOMAIN};
    ssl_certificate {CERTIFICATE}/fullchain.pem;
    ssl_certificate_key {CERTIFICATE}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    location ^~ /updates/ {{
        alias {ROOT}/public/;
        auth_basic "KaiForward updates";
        auth_basic_user_file {ROOT}/auth.htpasswd;
        autoindex off;
        add_header Cache-Control "private, no-store" always;
        add_header X-Content-Type-Options "nosniff" always;
        types {{ application/json json; application/vnd.android.package-archive apk; }}
        default_type application/octet-stream;
        limit_except GET {{ deny all; }}
    }}
    location / {{ return 404; }}
}}
'''


def legacy_config(redirect=False):
    if not redirect:
        # Keep old clients working even while their resolver caches NXDOMAIN for the new host.
        return f'''{MANAGED}
location ^~ /kaiforward/updates/ {{
    alias {ROOT}/public/;
    auth_basic "KaiForward updates";
    auth_basic_user_file {ROOT}/auth.htpasswd;
    autoindex off;
    add_header Cache-Control "private, no-store" always;
    add_header X-Content-Type-Options "nosniff" always;
    types {{ application/json json; application/vnd.android.package-archive apk; }}
    default_type application/octet-stream;
    limit_except GET {{ deny all; }}
}}
'''
    # Preserve the complete filename, including APKs referenced by previously fetched manifests.
    return f'''{MANAGED}
location ^~ /kaiforward/updates/ {{
    rewrite ^/kaiforward/updates/(.*)$ https://{DOMAIN}/updates/$1 permanent;
}}
'''


def reload_nginx():
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)


def migrate(redirect_legacy=False):
    if os.geteuid() != 0:
        raise SystemExit('Run this script with sudo')
    if not (ROOT / '.initialized').is_file() or not LEGACY.is_file():
        raise SystemExit('Initialize the existing authenticated update host first')
    try:
        socket.getaddrinfo(DOMAIN, 80)
    except OSError:
        raise SystemExit(f'Create DNS for {DOMAIN} pointing to this server before migrating')
    if SITE.exists() and MANAGED not in SITE.read_text():
        raise SystemExit(f'Refusing to replace an unmanaged configuration: {SITE}')
    if ENABLED.exists() or ENABLED.is_symlink():
        if not ENABLED.is_symlink() or ENABLED.resolve() != SITE.resolve():
            raise SystemExit(f'Refusing to replace an unrelated enabled site: {ENABLED}')
    previous_site = SITE.read_bytes() if SITE.exists() else None
    previous_legacy = LEGACY.read_bytes()
    previous_hook = RENEW_HOOK.read_bytes() if RENEW_HOOK.exists() else None
    was_enabled = ENABLED.is_symlink()
    backup = LEGACY.with_name(LEGACY.name + '.before-subdomain-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'))
    backup.write_bytes(previous_legacy)
    try:
        CHALLENGES.mkdir(parents=True, exist_ok=True, mode=0o755)
        CHALLENGES.chmod(0o755)
        if not (CERTIFICATE / 'fullchain.pem').is_file() or not (CERTIFICATE / 'privkey.pem').is_file():
            SITE.write_text(http_config())
            SITE.chmod(0o644)
            if not was_enabled:
                ENABLED.symlink_to(SITE)
            reload_nginx()
            subprocess.run(['certbot', 'certonly', '--webroot', '--webroot-path', str(CHALLENGES),
                '--domain', DOMAIN, '--cert-name', DOMAIN, '--non-interactive', '--agree-tos'], check=True)
        SITE.write_text(https_config())
        SITE.chmod(0o644)
        if not ENABLED.is_symlink():
            ENABLED.symlink_to(SITE)
        LEGACY.write_text(legacy_config(redirect=redirect_legacy))
        RENEW_HOOK.parent.mkdir(parents=True, exist_ok=True)
        RENEW_HOOK.write_text('#!/bin/sh\nset -eu\nnginx -t\nsystemctl reload nginx\n')
        RENEW_HOOK.chmod(0o755)
        reload_nginx()
    except BaseException:
        LEGACY.write_bytes(previous_legacy)
        if previous_site is None:
            SITE.unlink(missing_ok=True)
        else:
            SITE.write_bytes(previous_site)
        if not was_enabled:
            ENABLED.unlink(missing_ok=True)
        if previous_hook is None:
            RENEW_HOOK.unlink(missing_ok=True)
        else:
            RENEW_HOOK.write_bytes(previous_hook)
        reload_nginx()
        raise
    print(f'Updates moved to https://{DOMAIN}/updates/latest.json')
    print('Legacy URLs ' + ('redirect to the subdomain.' if redirect_legacy else
        'serve the same authenticated files directly for compatibility.'))
    print('Original route backup:', backup)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--redirect-legacy', action='store_true',
        help='Redirect old URLs instead of serving them directly; use only after DNS caches have cleared')
    args = parser.parse_args()
    migrate(redirect_legacy=args.redirect_legacy)
