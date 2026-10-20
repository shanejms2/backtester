# Backtester JSON API

Read-only HTTPS API over stored ClickHouse bars. Does **not** ingest or run strategies.

| URL | Purpose |
|-----|---------|
| https://backtester-api.shanejames.dev | Public HTTPS (nginx → localhost `:8088`) |
| http://127.0.0.1:8088 | Local bind (Docker / `backtester serve`) |

Auth: send `X-API-Key: <BACKTESTER_API_KEY>` on `/v1/*`. `/health` is open.

## Endpoints

### `GET /health`

```json
{"status": "ok"}
```

### `GET /v1/instruments?q=AAPL`

Registered catalog lookup only (no synthesize). `q` may be a stable id or alias.

### `GET /v1/coverage`

- Inventory: omit `instrument_id`
- One series: `instrument_id` + `tf` (+ optional `source`)

### `GET /v1/bars`

| Param | Required | Notes |
|-------|----------|-------|
| `instrument_id` | yes | e.g. `EQ:XNAS:AAPL` |
| `tf` | yes | Native stored timeframe |
| `from` | yes | ISO date or datetime |
| `to` | no | Default now UTC; date-only is exclusive end (+1 day) |
| `source` | no | Vendor filter |
| `tz` | no | IANA display zone; adds `ts_local` |
| `resample` | no | Derived timeframe |
| `limit` | no | Cap rows (default / max from `BACKTESTER_API_MAX_BARS`) |

Response includes `bars` (JSON array), `count`, `truncated`, `limit`.

Example:

```bash
curl -sS -H "X-API-Key: $BACKTESTER_API_KEY" \
  "https://backtester-api.shanejames.dev/v1/bars?instrument_id=EQ:XNAS:AAPL&tf=1d&from=2024-01-01&to=2024-03-01&tz=UTC"
```

## Run locally

```bash
# with Compose (recommended on this VPS)
docker compose up -d --build api

# or host venv
pip install -e ".[api]"
backtester serve
```

## First-time public HTTPS (sudo + DNS)

1. **DNS** — Cloudflare A record `backtester-api` → VPS IPv4 (proxied or DNS-only).

2. Set a strong `BACKTESTER_API_KEY` in `.env`, then `docker compose up -d --build api`.

3. **Nginx + Certbot**:

   ```bash
   sudo cp ~/projects/backtester/infra/nginx/backtester-api.shanejames.dev.conf \
     /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d backtester-api.shanejames.dev
   ```

4. Open https://backtester-api.shanejames.dev/health and call `/v1/*` with the API key.
