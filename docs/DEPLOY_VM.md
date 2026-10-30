# Running cashfloor on a dedicated VM behind nginx

cashfloor ships with **no authentication** and binds to loopback by design.
Putting it on a LAN-reachable VM is fine — one always-on box running
`cashfloor serve` (which includes the daily scheduler) — as long as nginx
in front provides TLS and a login. Treat the VM's disk as sensitive: it holds
your statements, the SQLite database and browser profiles.

## 1. Install

```bash
sudo useradd -r -m -d /opt/cashfloor cashfloor
sudo -u cashfloor git clone <repo> /opt/cashfloor/app
cd /opt/cashfloor/app && sudo -u cashfloor scripts/setup.sh
# for scrapers on a headless box:
sudo -u cashfloor .venv/bin/playwright install --with-deps chromium
```

## 2. Secrets without a desktop keyring

A headless server usually has no Secret Service, so `keyring` raises
"No recommended backend was available". Options:

* **Encrypted file backend** (simplest): `pip install keyrings.cryptfile`
  and set `PYTHON_KEYRING_BACKEND=keyrings.cryptfile.cryptfile.CryptFileKeyring`
  plus `KEYRING_CRYPTFILE_PASSWORD=<passphrase>` in the service environment.
* `keyrings.alt` plaintext file backend — only acceptable if the VM disk is
  encrypted and the file is `chmod 600`.
* A headless `gnome-keyring-daemon` — works, fiddlier to keep unlocked.

Set the backend env vars in the systemd unit below *before* running
`cashfloor simplefin setup` (or pasting the token in the GUI), so the token
lands in the backend the service will read.

## 3. systemd unit

```ini
# /etc/systemd/system/cashfloor.service
[Unit]
Description=cashfloor
After=network-online.target

[Service]
User=cashfloor
WorkingDirectory=/opt/cashfloor/app
Environment=CASHFLOOR_CONFIG=/opt/cashfloor/app/config.toml
Environment=PYTHON_KEYRING_BACKEND=keyrings.cryptfile.cryptfile.CryptFileKeyring
EnvironmentFile=/etc/cashfloor.env        # KEYRING_CRYPTFILE_PASSWORD=..., chmod 600
ExecStart=/opt/cashfloor/app/.venv/bin/cashfloor serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Keep `web.host = "127.0.0.1"` in `config.toml` and run nginx **on the same
VM**. If nginx is elsewhere, bind to the VM's LAN IP and firewall port 8477
to the nginx host only.

## 4. nginx

Use a **subdomain**, not a sub-path: the app's links are root-relative.

```nginx
server {
    listen 443 ssl http2;
    server_name cashfloor.yourdomain.lan;
    # ssl_certificate / ssl_certificate_key per your existing setup

    auth_basic "cashfloor";
    auth_basic_user_file /etc/nginx/cashfloor.htpasswd;   # or your SSO (Authelia, Authentik…)

    location / {
        proxy_pass http://127.0.0.1:8477;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;   # "Sync now" runs inline; scrapers can be slow
    }
}
```

`cashfloor serve` already trusts `X-Forwarded-*` from 127.0.0.1, so
redirects come back as `https://cashfloor.yourdomain.lan/...`.

## 5. First login for scrapers (MFA)

Headless Chromium can't show you an MFA prompt. Either do the first login on
a desktop with `headless = false` and copy `data/browser-profiles/<name>/`
to the VM, or install `xvfb` and run the first sync through a VNC session.
After that, set `headless = true`.

## 6. Backups

Everything lives under `data/` and `statements/`. Back those two folders up
(the SQLite file is safe to copy while the service runs thanks to WAL mode,
but `sqlite3 data/cashfloor.sqlite ".backup backup.sqlite"` is cleaner).
