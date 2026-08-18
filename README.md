# quadlet-paperless

Quadlet setup for [Paperless-ngx](https://docs.paperless-ngx.com/) — self-hosted document management with OCR (`ghcr.io/paperless-ngx/paperless-ngx:latest`). Full stack: app + PostgreSQL + Redis + Gotenberg + Tika (office-document conversion & text extraction).

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
| `scripts/taxonomy-audit.py` | Audit tags, document types & correspondents → local-LLM merge/delete suggestions ([below](#taxonomy-audit-tags-document-types--correspondents)) |
| `scripts/taxonomy-audit.env.template` | Credentials template for the audit script (Paperless + Ollama) |

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

> **Migrating from an existing install?** Do **not** run step 7 against an empty database yet — follow the [Migration](#migration-from-an-older-install) section below instead, which pins the app to your current version, restores your data, then upgrades.

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
| `PAPERLESS_OCR_LANGUAGE` / `PAPERLESS_OCR_LANGUAGES` | `deu+eng` / `deu eng` | Languages *used* for OCR (`+`-joined) / data packs *installed* (space-sep; base image ships only `eng`) — adjust to your documents |
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

Strategy: **logical DB dump + media/data copy**, version-pinned to avoid schema breakage. Because a logical `pg_dump`/`pg_restore` is used, the target PostgreSQL major (18 here) need **not** match the source — only the Paperless **app** version must match during the restore.

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

## Taxonomy audit (tags, document types & correspondents)

An overgrown taxonomy (e.g. 800+ tags, 270+ document types, or many redundant correspondents on ~1400 docs) is a curation problem, not a search problem — so it needs no RAG, just the flat list of items with per-item document counts, which the Paperless-ngx REST API exposes directly (`/api/tags/`, `/api/document_types/`, and `/api/correspondents/` each return `document_count`), fed once to a local LLM.

`scripts/taxonomy-audit.py` does exactly that — **stdlib only, no pip install**, runnable from any host that can reach Paperless and Ollama (it does not touch the containers). For each resource it:

1. pulls every item + `document_count` (paginated),
2. prints stats + **unused** (0 docs) and **low-use** items + local near-duplicate groups (German-aware singular/plural/umlaut folding) — no LLM needed for these,
3. for **correspondents**, additionally prints **shared-first-word groups** — advisory clusters like `Allianz` / `Allianz Lebensversicherungs-AG` / `Allianz Lebensversicherungs-Aktiengesellschaft` that whole-name folding can't catch (may include genuinely distinct entities, so review before merging),
4. asks an Ollama model for **merge groups**, **delete candidates**, and **keep** notes.

The LLM prompts deliberately reuse the **conventions** from the `paperless-ai-next` tagging system prompt so the cleanup targets the same shape the tagger produces and the taxonomy stays stable instead of the long tail regrowing: shortest-form **sender** correspondents (never the recipient/account holder; no generic-category or bare-person entries), **singular** thematic tags with no names/numbers/dates, and a fixed set of broad **document-type** base classes (no English, no `…schreiben` compounds, no slash-combined names). This is a two-way loop — a recurring merge/delete here signals a gap to fold back into the tagger prompt. Only the conventions are shared, not that prompt's per-document JSON schema / date rules; keep the `RESOURCES` hints in `taxonomy-audit.py` aligned when the tagger prompt changes (last synced with `paperless-ai-next` README commit *Harden paperless-ai prompt*).

By default it is **read-only** (analysis) — it only prints suggestions.

```sh
cp scripts/taxonomy-audit.env.template scripts/taxonomy-audit.env   # fill in Paperless + Ollama creds
scripts/taxonomy-audit.py                            # analyse all three resources (read-only)
scripts/taxonomy-audit.py --resource correspondents  # one resource only
scripts/taxonomy-audit.py --no-llm                   # local analysis only (no Ollama call/creds)
scripts/taxonomy-audit.py --self-test
```

### Applying merges (`--apply`)

`--apply` turns the suggestions into action. It asks the model for a machine-readable merge/delete plan, **prints the full resolved plan first** (numbered `M1`, `M2`, `D1`, …), and only then walks you through it **one change at a time** (`y`/`N`/`q`):

- **Merge** — reassigns every document from the redundant items onto a canonical one via `POST /api/documents/bulk_edit/` (`set_correspondent` / `set_document_type` / `modify_tags`), then **deletes each emptied duplicate only after re-checking its `document_count` is 0** (never deletes an item that still has documents). The canonical is used as-is when it's an **existing** item (small tags fold into a big one, e.g. `Bankgebühren → Bank`); if it's a new name, the surviving item is renamed to it.
- **Delete** — removes a genuinely redundant item (its documents keep all other metadata).

Always preview first with `--dry-run` — it resolves and prints the exact actions (which items merge into which, how many docs move) **without any writes**:

```sh
scripts/taxonomy-audit.py --resource correspondents --apply --dry-run   # preview, no changes
scripts/taxonomy-audit.py --resource correspondents --apply             # interactive, WRITES
```

Merges/deletes are irreversible, so start with one resource, keep a fresh DB backup (`paperless-backup.service`), and lean on `--dry-run` before the real run. Merging is reassign-then-delete because Paperless-ngx has no native object-merge endpoint.

#### Editable plans (`--plan` / `--apply-plan`)

For a messy taxonomy — especially **document types**, where a 12B model's free-form grouping is often semantically off (`Bestellung` folded into `Brief`, `Payslip` into `Zeugnis`) — correct the plan by hand before anything is written:

```sh
scripts/taxonomy-audit.py --resource document-types --plan   # → document-types_20260713_143022.plan
P=document-types_20260713_143022.plan
# edit $P: delete a bad group, drop a type from a group, move a line, retarget a MERGE
scripts/taxonomy-audit.py --apply-plan $P --dry-run  # preview (resource read from the file)
scripts/taxonomy-audit.py --apply-plan $P            # apply
```

`--plan` takes an **optional** filename: bare `--plan` writes `<resource>_<YYYYmmdd_HHMMSS>.plan` for each selected resource (so `--plan` with no `--resource` generates all three — each independently, so one model failure doesn't affect the others — and successive runs don't clobber), and it **prompts before overwriting**. Pass an explicit name (`--plan dt.plan`) only with a single `--resource`.

The plan file is a simple commented text format (`MERGE <survivor>` with indented member types, `DELETE <name>`; `#` comments, blank lines ignored) — no YAML dependency. It carries a `# resource: <key>` marker, so **`--apply-plan` needs no `--resource`** (pass one only to override). Since the file is already curated, **`--apply-plan` confirms once ("Proceed?") and then applies the whole plan** — it re-resolves against the live taxonomy first (unknown names are reported and skipped) and prints the full plan; use `--dry-run` to preview without writing. (`--apply`, the uncurated model-plan path, still confirms each action unless `--yes`.) `--apply / --plan / --apply-plan` are mutually exclusive.

Two knobs reduce model sloppiness up front: the plan prompt is **conservative** (only merge same-kind types; keep `Rechnung`/`Abrechnung`, `Vertrag`/`Versicherungsschein`, `Antrag`/`Formular` separate) and runs at a low **`OLLAMA_TEMPERATURE`** (default 0.2). For a big cleanup, a larger model (27B+) for the plan step groups noticeably better than 12B.

Credentials live in `scripts/taxonomy-audit.env` (gitignored; real env vars override it): `PAPERLESS_URL` + `PAPERLESS_TOKEN` (Paperless → Settings → create token) and `OLLAMA_HOST` + `OLLAMA_MODEL`. Optional `OLLAMA_NUM_CTX` (default 16384; each taxonomy goes in one prompt, so raise it for very large sets — more VRAM), `OLLAMA_TEMPERATURE` (default 0.2), and `SINGLETON_THRESHOLD` (default 2). The script hits the GPU box's native `/api/generate`, so `num_ctx` **is** honored here (unlike the OpenAI-compatible endpoint the `paperless-ai-next` tagging model uses). The `gemma4-paperless` model referenced in the template is defined in `quadlet-paperless-ai-next`.

Run `scripts/taxonomy-audit.py --help` for the full flag/env reference.

### Troubleshooting

- **`--verbose`** logs every request's **full URL**, HTTP status, and result `count` to stderr — the quickest way to see what the API is actually returning. All error messages now include the fully resolved URL too.
- **`PAPERLESS_URL` with a trailing `/api`** (the old paperless-ai format) is auto-stripped, so `https://host` and `https://host/api/` both resolve to `…/api/tags/` (not `…/api/api/…`).
- **`No <items> returned` despite having many:** the request reached Paperless (HTTP 200) but saw zero objects — almost always a **token-permissions** issue. Paperless tokens are per-user and the API only returns objects that user owns or may view; use a **superuser** token. (A wrong URL or token fails outright with a 4xx/connection error instead.)
- **`empty response (done_reason='length')`:** the prompt filled the whole context window, so the model had no room to generate. Large taxonomies (e.g. 900+ German tags, which tokenize densely) are now **auto-chunked** for plan generation — the tail is split into context-fitting batches, each including the high-count "anchor" items as merge targets, and the per-chunk plans are combined. If a single chunk still overflows, raise `OLLAMA_NUM_CTX` (VRAM permitting). A truly empty reply with `done_reason='load'` instead means the model failed to load / ran out of VRAM — check `ollama ps` and the Ollama logs, or lower `OLLAMA_NUM_CTX`.
- **Ollama `Connection timed out`** is a **network** problem, not auth — nothing answered at `OLLAMA_HOST` from where the script runs. A bad token would return an HTTP error instead. A link-local/private address (e.g. `169.254.x.x`) that works for the server-side `paperless-ai-next` container often does **not** route from a workstation — use the GPU box's LAN address or run the script on the server. If your Ollama sits behind an auth proxy, set `OLLAMA_API_KEY` (sent as a bearer token; native Ollama ignores it).
- Behind Caddy, Paperless emits paginated `next` links as `http://`; the script rewrites each page back onto `PAPERLESS_URL`'s scheme/host so pagination stays authenticated over HTTPS.

## Reverse proxy (Caddy)

```
@paperless host paperless.my_domain.tld
handle @paperless {
    reverse_proxy localhost:8000
}
```

Add a DNS A/CNAME record for `paperless.my_domain.tld` pointing to your server.

## UID verification

The containers assume: app **1000**, postgres **999**, redis **999**. Verify before starting (and re-chown the matching bind dir if a value differs):

```sh
podman inspect ghcr.io/paperless-ngx/paperless-ngx:latest --format '{{.Config.User}}'
podman inspect docker.io/library/postgres:18 --format '{{.Config.User}}'
podman inspect docker.io/library/redis:8 --format '{{.Config.User}}'
# e.g. if postgres differs: sudo -u paperless podman unshare chown -R <uid>:<gid> ~paperless/db
```

Gotenberg and Tika are stateless (no bind mounts), so their UID does not need mapping.

## Backup

`paperless-backup.service` runs `pg_dump` inside the DB container and mirrors the `media/` and `data/` directories to `/var/backups/paperless/`. A remote machine pulls via `rsync` over SSH using the shared `backupuser`. See the [general backup setup](https://github.com/mkoester/quadlet-my-guidelines#backup) for the one-time server-wide setup (group, backup user, SSH key).

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
- Persistent data on the host: `~paperless/db/` (PostgreSQL), `~paperless/redis/` (broker), and `~paperless/{data,media,consume,export}/` (Paperless). Documents live under `media/` — they are **not** in the database, hence the backup covers both.
- Drop files into `~paperless/consume/` to have them imported and OCR'd automatically.
- `AutoUpdate=registry` is enabled; activate the timer once to get automatic image updates:
  ```sh
  sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user enable --now podman-auto-update.timer
  ```
- To prune old images automatically, enable the system-wide prune timer (see [image pruning setup](https://github.com/mkoester/quadlet-my-guidelines#image-pruning)). Replace `30` with the desired retention in days:
  ```sh
  sudo -u paperless XDG_RUNTIME_DIR=/run/user/$(id -u paperless) systemctl --user enable --now podman-image-prune@30.timer
  ```
