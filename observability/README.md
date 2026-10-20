# Backtester observability

Project-scoped **Grafana + Loki + Alloy + Prometheus**. Collects Docker logs only from Compose project `backtester` (ClickHouse / Postgres). This stack itself runs as `backtester-obs` and is not ingested.

| URL | Purpose |
|-----|---------|
| https://backtester-logs.shanejames.dev | Grafana (login required) |
| http://127.0.0.1:3201 | Grafana (localhost) |
| http://127.0.0.1:3101 | Loki API (localhost) |
| http://127.0.0.1:9091 | Prometheus (localhost) |

Separate from [`~/projects/observability`](../../observability) (shared learner/homepage logs).

## Quick start

From the backtester repo root, ensure `GRAFANA_PASSWORD` is set in `.env`.
`observability/.env` is a symlink to that file (Compose needs it for interpolation).

```bash
cd ~/projects/backtester/observability
# if missing: ln -sfn ../.env .env
docker compose up -d
docker compose ps
```

Admin login: `admin` / value of `GRAFANA_PASSWORD`.

### First-time public HTTPS (sudo + DNS)

1. **DNS** — Cloudflare A record `backtester-logs` → VPS IPv4 (proxied).

2. **Nginx** — install site and TLS (needs sudo on this VPS):

   ```bash
   sudo cp ~/projects/backtester/observability/infra/nginx/backtester-logs.shanejames.dev.conf \
     /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d backtester-logs.shanejames.dev
   ```

   The site file is ready in-repo; copying into `sites-enabled` was not done from this session (sudo password required).

3. Open https://backtester-logs.shanejames.dev

### SSH tunnel (if DNS is not ready)

```bash
ssh -p 2847 -L 3201:127.0.0.1:3201 shane@46.225.153.199
# then http://127.0.0.1:3201
```

## What gets collected

| Source | Labels | Notes |
|--------|--------|-------|
| `backtester` compose containers | `project`, `service`, `container` | Alloy → Loki |
| ClickHouse `:9363/metrics` | `project=backtester` | Queries, memory, disk, connections |
| Postgres (via `postgres-exporter`) | `project=backtester` | Connections, txns, DB size |
| Alloy / Loki / Grafana / Prometheus | `project=backtester-obs` | Pipeline + stack health |

Dashboard: [Backtester overview](https://backtester-logs.shanejames.dev/d/backtester-overview/backtester-overview) (Overview, Logs, Alloy, ClickHouse, Postgres, Observability stack).

No host Nginx scrape in v1 (no backtester public app vhost yet). No host `node_exporter`.

### Explore (LogQL)

```logql
{project="backtester"}
```

```logql
{project="backtester", service="clickhouse"}
```

## Useful commands

```bash
cd ~/projects/backtester/observability
docker compose logs -f alloy
docker compose logs -f loki
docker compose down          # keeps volumes
```
