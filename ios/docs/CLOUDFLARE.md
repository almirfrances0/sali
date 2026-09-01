# Cloudflare Deployment — Sali Remote Presence

How to expose the Sali API at `https://sali.salieno.com` **securely** (Prompt 13 §22/§26). Cloudflare is
**one** layer of defense, not the only one — the API authenticates and authorizes every request itself
(device sessions, roles). Never rely on the URL being hard to guess.

```
Internet
   │  TLS
   ▼
Cloudflare edge  ──  WAF · rate limiting · TLS · (optional) Access
   │  Cloudflare Tunnel (outbound-only, no inbound port)
   ▼
cloudflared (on the Kali host)
   │  loopback
   ▼
Sali API  (uvicorn on 127.0.0.1:8080)  ──  device auth · roles · policy · workspace authority
   │
   ▼
PostgreSQL (unix socket, peer auth, no password)
```

## 1. Why a tunnel (not a port-forward)

A Cloudflare Tunnel makes an **outbound** connection from the host to Cloudflare; there is **no inbound
port** open on the Kali box and no public origin IP. This is the intended topology (§26): the origin must
never be directly reachable. Bind uvicorn to **loopback** in production so nothing but `cloudflared` can
reach it:

```bash
# production: bind to localhost only — the tunnel is the sole ingress
sali serve --host 127.0.0.1 --port 8080
```

(During LAN development you can use `--host 0.0.0.0` on a trusted network and enroll over `http://<lan-ip>:8080`.)

## 2. Install & authenticate cloudflared (on the Kali host)

```bash
# Debian/Kali
curl -L https://pkg.cloudflare.com/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb

cloudflared tunnel login                    # opens a browser; authorize the salieno.com zone
cloudflared tunnel create sali              # creates a tunnel + credentials json (~/.cloudflared/<UUID>.json)
cloudflared tunnel route dns sali sali.salieno.com   # CNAME sali.salieno.com → <UUID>.cfargotunnel.com
```

## 3. Tunnel config

`~/.cloudflared/config.yml`:
```yaml
tunnel: <TUNNEL-UUID>
credentials-file: /home/almir/.cloudflared/<TUNNEL-UUID>.json

ingress:
  - hostname: sali.salieno.com
    service: http://127.0.0.1:8080
    originRequest:
      # WebSocket (/ws) is proxied automatically; keep long-lived connections alive
      connectTimeout: 30s
      noHappyEyeballs: true
  - service: http_status:404          # everything else is refused
```

Run it as a service so it survives reboot:
```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

## 4. Edge protections (Cloudflare dashboard / API)

Configure on the `salieno.com` zone for `sali.salieno.com`:

- **SSL/TLS mode:** *Full (strict)* — even though the origin is loopback via the tunnel, keep strict.
- **WAF:** enable the managed ruleset. Add a custom rule to allow only the paths the app uses
  (`/api/v1/*`, `/ws`, `/healthz`, `/docs` optional) and challenge/deny everything else.
- **Rate limiting:** e.g. limit `POST /api/v1/enroll` and `POST /api/v1/auth/refresh` to a few requests
  per minute per IP (defends the pairing/refresh endpoints from brute force — the codes/tokens are
  high-entropy and expiring, this is belt-and-suspenders).
- **Bot Fight Mode / Managed Challenge** for suspicious traffic.
- **(Optional, strong) Cloudflare Access (Zero Trust):** put the whole hostname behind an Access policy
  (e.g. one-time-PIN to `almirfrances1@gmail.com`, or a service token for the app). This adds an
  authentication layer *in front of* the API. If you use Access **service tokens** for the app, send the
  `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers from the app (store them in the Keychain, add
  them in `APIClient`'s request builder). This is optional because the API already authenticates devices;
  Access is defense-in-depth for the enrollment surface.

## 5. What the app needs

Nothing app-side changes for the tunnel except the base URL: set the **Production** environment to
`https://sali.salieno.com` (already the default). The app derives the WebSocket URL (`wss://sali.salieno.com/ws`)
automatically. Enrollment still requires a code minted **on the host** (`sali enroll-code`) — the tunnel
does not change the trust bootstrap.

## 6. Health & operations

- `GET https://sali.salieno.com/healthz` → `{"status":"ok"}` — use for an uptime monitor and to verify the
  tunnel is up (no auth, no data).
- If the tunnel drops, the app shows **offline** and reconnects with backoff; on reconnect the WebSocket
  replays missed events by sequence (§29), so no state is lost.
- Rotate the tunnel credentials if the host is ever compromised, and **revoke devices** from the app
  (Settings → Devices) — revocation is server-side and immediate, independent of Cloudflare.

## 7. Security checklist

- [ ] uvicorn bound to `127.0.0.1` in production (no public port).
- [ ] Tunnel is the only ingress; DNS is proxied (orange cloud).
- [ ] TLS Full (strict); WAF + rate limiting on enroll/refresh.
- [ ] Device tokens in the iOS Keychain; only hashes server-side; secrets never logged.
- [ ] Owner can list/revoke devices; observer devices are read-only.
- [ ] No DB/shell/`/run` endpoints exposed — only structured operations (§28).
