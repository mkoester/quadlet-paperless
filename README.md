# quadlet-paperless

Quadlet setup for [Paperless-ngx](https://docs.paperless-ngx.com/) — self-hosted document
management with OCR (`ghcr.io/paperless-ngx/paperless-ngx:latest`). Full stack: app +
PostgreSQL + Redis + Gotenberg + Tika (office-document conversion & text extraction).

This project was created with the help of Claude Code and https://github.com/mkoester/quadlet-my-guidelines/blob/main/new_quadlet_with_ai_assistance.md.

## Files in this repo

| File | Description |
|---|---|
| `paperless.network` | Quadlet network shared by all containers |
| `paperless-db.container` | PostgreSQL database container |
| `paperless-broker.container` | Redis broker container |
| `paperless-gotenberg.container` | Gotenberg — office documents → PDF |
| `paperless-tika.container` | Apache Tika — office-document text extraction |
| `paperless.container` | Paperless-ngx web/app container |
| `paperless.env` | Default environment variables (non-secret) |
| `paperless.override.env.template` | Template for local overrides (secrets, URL) |
| `paperless-backup.service` | Backs up the DB (`pg_dump`) + media/data dirs |
| `paperless-backup.timer` | Triggers the backup daily |

## Setup

```sh
# 1. Create service user (regular user, home in /var/lib)
sudo useradd -m -d /var/lib/paperless -s /usr/sbin/nologin paperless

REPO_URL=https://github.com/mkoester/quadlet-paperless.git
REPO=~paperless/quadlet-paperless
```

```sh
# 2. Enable linger
sudo loginctl enable-linger paperless

# 3. Clone this repo into the service user's home
sudo -u paperless git clone $REPO_URL $REPO

# 4. Create quadlet and data directories
sudo -u paperless mkdir -p ~paperless/.config/containers/systemd
sudo -u paperless mkdir -p ~paperless/{db,redis,data,media,consume,export}

# 5. Create .override.env from template and fill in required values
sudo -u paperless cp $REPO/paperless.override.env.template $REPO/paperless.override.env
sudo -u paperless nano $REPO/paperless.override.env

# 6. Symlink all quadlet files from the repo
for f in paperless.network paperless-db.container paperless-broker.container \
         paperless-gotenberg.container paperless-tika.container paperless.container \
         paperless.env paperless.override.env; do
  sudo -u paperless ln -s $REPO/$f ~paperless/.config/containers/systemd/$f
done

# 7. Reload and start
sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user daemon-reload
sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user start paperless

# 8. Verify (starting the app pulls in db/broker/gotenberg/tika via dependencies)
sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user status 'paperless*'
```

> **Migrating from an existing install?** Do **not** run step 7 against an empty database
> yet — follow the [Migration](#migration-from-an-older-install) section below instead, which
> pins the app to your current version, restores your data, then upgrades.

## Configuration

`paperless.env` contains non-sensitive defaults:

| Variable | Default | Description |
|---|---|---|
| `PAPERLESS_REDIS` | `redis://systemd-paperless-broker:6379` | Redis broker (default quadlet name of `paperless-broker.container`) |
| `PAPERLESS_DBHOST` | `systemd-paperless-db` | DB host (default quadlet name of `paperless-db.container`) |
| `PAPERLESS_DBNAME` / `PAPERLESS_DBUSER` | `paperless` | Database name / user |
| `PAPERLESS_TIKA_ENABLED` | `1` | Enable Tika/Gotenberg office-doc pipeline |
| `PAPERLESS_TIKA_GOTENBERG_ENDPOINT` | `http://systemd-paperless-gotenberg:3000` | Gotenberg endpoint |
| `PAPERLESS_TIKA_ENDPOINT` | `http://systemd-paperless-tika:9998` | Tika endpoint |
| `PAPERLESS_OCR_LANGUAGE` / `PAPERLESS_OCR_LANGUAGES` | `deu` / `eng` | Primary + extra OCR languages — adjust to your documents |
| `PAPERLESS_TIME_ZONE` | `Europe/Berlin` | Container timezone |
| `POSTGRES_DB` / `POSTGRES_USER` | `paperless` | DB-container init values |

`paperless.override.env` (created from the template) holds instance-specific and sensitive values:

| Variable | Description |
|---|---|
| `POSTGRES_PASSWORD` + `PAPERLESS_DBPASS` | DB password — set the **same value** for both |
| `PAPERLESS_SECRET_KEY` | Django secret key (`openssl rand -base64 48`) |
| `PAPERLESS_URL` | Public URL, e.g. `https://paperless.example.com` |
| `PAPERLESS_ADMIN_USER` / `PAPERLESS_ADMIN_PASSWORD` | Optional — only bootstraps a superuser on a fresh empty DB; not needed when migrating |

## Migration (from an older install)

Strategy: **logical DB dump + media/data copy**, version-pinned to avoid schema breakage.
Because a logical `pg_dump`/`pg_restore` is used, the target PostgreSQL major (18 here) need
**not** match the source — only the Paperless **app** version must match during the restore.

```sh
# --- On the source host: capture facts + dump ---
# Current Paperless version → the tag to pin below
podman inspect <old-app> --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
# Quiesce the source (stop the app so no writes race the dump), then:
podman exec <old-db> pg_dump -U <user> -Fc <db> > /tmp/paperless.dump
```

```sh
# --- On the new host, as the paperless user ---
# 1. Pin the app image to the SOURCE version in paperless.container:
#      Image=ghcr.io/paperless-ngx/paperless-ngx:2.20.15
# 2. Copy documents/data into the bind mounts
rsync -a <source>:<old-media>/ ~paperless/media/
rsync -a <source>:<old-data>/  ~paperless/data/
sudo -u paperless podman unshare chown -R 1000:1000 ~paperless/{media,data}

# 3. Start ONLY the database, create schema-less DB, restore the dump
sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user start paperless-db
sudo -u paperless podman exec -i systemd-paperless-db \
  pg_restore -U paperless -d paperless --clean --if-exists < /tmp/paperless.dump

# 4. Start the rest, verify against the pinned version
sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user start paperless

# 5. Upgrade: set Image back to :latest in paperless.container, then
sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user daemon-reload
sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user restart paperless
# Paperless applies forward DB migrations automatically on startup. If search or
# thumbnails look off after the jump, rebuild them:
sudo -u paperless podman exec systemd-paperless document_index reindex
sudo -u paperless podman exec systemd-paperless document_thumbnails
```

## Reverse proxy (Caddy)

```
@paperless host paperless.my_domain.tld
handle @paperless {
    reverse_proxy localhost:8000
}
```

Add a DNS A/CNAME record for `paperless.my_domain.tld` pointing to your server.

## UID verification

The containers assume: app **1000**, postgres **999**, redis **999**. Verify before starting
(and re-chown the matching bind dir if a value differs):

```sh
podman inspect ghcr.io/paperless-ngx/paperless-ngx:latest --format '{{.Config.User}}'
podman inspect docker.io/library/postgres:18 --format '{{.Config.User}}'
podman inspect docker.io/library/redis:8 --format '{{.Config.User}}'
# e.g. if postgres differs: sudo -u paperless podman unshare chown -R <uid>:<gid> ~paperless/db
```

Gotenberg and Tika are stateless (no bind mounts), so their UID does not need mapping.

## Backup

`paperless-backup.service` runs `pg_dump` inside the DB container and mirrors the `media/` and
`data/` directories to `/var/backups/paperless/`. A remote machine pulls via `rsync` over SSH
using the shared `backupuser`. See the [general backup setup](https://github.com/mkoester/quadlet-my-guidelines#backup)
for the one-time server-wide setup (group, backup user, SSH key).

```sh
# 1. Create backup staging directory (owned by paperless, readable by backup-readers group)
sudo mkdir -p /var/backups/paperless
sudo chown paperless:backup-readers /var/backups/paperless
sudo chmod 2750 /var/backups/paperless

# 2. Symlink the backup service and timer from the repo
sudo -u paperless mkdir -p ~paperless/.config/systemd/user
sudo -u paperless ln -s $REPO/paperless-backup.service ~paperless/.config/systemd/user/paperless-backup.service
sudo -u paperless ln -s $REPO/paperless-backup.timer ~paperless/.config/systemd/user/paperless-backup.timer

# 3. Enable and start the timer
sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user daemon-reload
sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user enable --now paperless-backup.timer
```

### On the remote (backup) machine

```sh
rsync -az backupuser@paperless-host:/var/backups/paperless/ /path/to/local/backup/paperless/
```

## Notes

- Port `8000` is bound to `127.0.0.1` only — place a reverse proxy in front for external access.
- Persistent data on the host: `~paperless/db/` (PostgreSQL), `~paperless/redis/` (broker),
  and `~paperless/{data,media,consume,export}/` (Paperless). Documents live under `media/` —
  they are **not** in the database, hence the backup covers both.
- Drop files into `~paperless/consume/` to have them imported and OCR'd automatically.
- `AutoUpdate=registry` is enabled; activate the timer once to get automatic image updates:
  ```sh
  sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user enable --now podman-auto-update.timer
  ```
- To prune old images automatically, enable the system-wide prune timer (see [image pruning setup](https://github.com/mkoester/quadlet-my-guidelines#image-pruning)). Replace `30` with the desired retention in days:
  ```sh
  sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user enable --now podman-image-prune@30.timer
  ```
