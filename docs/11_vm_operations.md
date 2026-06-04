# VM Operations Runbook

Practical setup and maintenance procedures for the production VM (Hetzner).

## Postgres backups via rclone → R2

### 1. Install rclone

```bash
curl https://rclone.org/install.sh | sudo bash
```

### 2. Configure the R2 remote

```bash
rclone config
```

Choose `n` (new remote), name it `r2`, select `s3` as the type, then `Cloudflare R2` as the provider. Fill in:

| Field | Value |
|---|---|
| `access_key_id` | Your R2 access key (same as `R2_ACCESS_KEY_ID` in `.env`) |
| `secret_access_key` | Your R2 secret (same as `R2_SECRET_ACCESS_KEY` in `.env`) |
| `endpoint` | Your R2 endpoint URL (same as `R2_ENDPOINT_URL` in `.env`) |
| `region` | `auto` |

Leave everything else blank/default. Confirm with `y`.

Test it:
```bash
rclone ls r2:snake-replay-bundles
```

### 3. Create the backup bucket

Create a separate private bucket in the Cloudflare R2 dashboard named `snake-arena-backups`. Do **not** make it public.

Set a lifecycle rule on the `snake-arena-backups` bucket: delete objects older than 30 days. This satisfies the GDPR backup retention limit from §2 of the pre-launch checklist.

### 4. Add the cron job

```bash
crontab -e
```

Add:
```
0 2 * * * docker compose -f /home/snake/snake_arena/docker-compose.yml exec -T postgres pg_dump -U snake_arena snake_arena | gzip | rclone rcat r2:snake-arena-backups/snake_arena_$(date +\%Y\%m\%d).sql.gz
```

Adjust the path to match wherever the repo lives on the VM.

### 5. Test a restore

After the first backup runs, verify it actually works:

```bash
# Download the backup
rclone copy r2:snake-arena-backups/snake_arena_YYYYMMDD.sql.gz /tmp/

# Restore to a scratch database
gunzip -c /tmp/snake_arena_YYYYMMDD.sql.gz | docker compose exec -T postgres psql -U snake_arena -d postgres -c "CREATE DATABASE snake_arena_restore;"
gunzip -c /tmp/snake_arena_YYYYMMDD.sql.gz | docker compose exec -T postgres psql -U snake_arena -d snake_arena_restore
```

A backup that has never been restored is not a backup. Do this once.

## Log retention

Keep application logs ≤ 30 days (GDPR commitment). Configure Docker log rotation in `/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "7"
  }
}
```

Then restart Docker: `sudo systemctl restart docker`.

## Deploying updates

```bash
cd /home/snake/snake_arena
git pull
cd frontend && npm run build && cd ..
docker compose up --build -d
```

## Environment variables

Never commit `.env` to git. On the VM, create it manually and keep a secure offline copy (e.g. in a password manager). Key things that differ from dev:

- `POSTGRES_PASSWORD` — strong random password, not `dev_password_change_me`
- `CLERK_ISSUER` — `https://clerk.gridsnake.com`
- `CLERK_WEBHOOK_SECRET` — prod webhook secret from Clerk dashboard
- `VITE_CLERK_PUBLISHABLE_KEY` in `frontend/.env.production` — `pk_live_...`
