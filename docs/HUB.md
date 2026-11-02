# Running a team hub

One hub serves one brain for one company, from your own server: a Linux VM with a persistent disk (EC2, Lightsail, Azure, GCE, DigitalOcean, Hetzner, or an office server). Not ECS Fargate, Cloud Run or Azure Container Apps: they have no persistent block disk, and SQLite on a network filesystem isn't safe. A token reads the whole brain unless it names folders (see Folder tokens), so capture only the channels and mailboxes every whole-brain token holder may read, or run a second hub.

## Install

```bash
# On the server, with Docker, DNS for BRAIN_HOST pointed at it, and ports 80 and 443 open.
# No hub image is published. Download the agent-si wheel from the releases page, put it in dist/ and build the image:
mkdir -p dist && cp ~/Downloads/agent_si-<version>-py3-none-any.whl dist/
docker build -f deploy/hub/Dockerfile -t agent-si-hub:<version> .
cd deploy/hub
cp env.example .env             # set BRAIN_HOST, and BRAIN_IMAGE to agent-si-hub:<version>
docker compose run --rm brain brain init brain --dir /data --principal "Alex Example" --timezone Australia/Sydney
docker compose up -d
docker compose exec brain brain hub token --instance /data/brain issue --agent claude --scope write --label "Alex Example laptop, Claude Code"
```

The token is printed once. Give it to its holder for `BRAIN_TOKEN`; only its SHA-256 is kept. The server never runs `brain init` itself: with no brain at `/data/brain` it answers `/healthz` and nothing else, because an empty store that looks healthy is worse than an outage.

The image is built on your own server from a wheel you downloaded. Check the wheel against the `SHA256SUMS` file on the same release page before you build, as `sha256sum -c SHA256SUMS` does.

## Connect a laptop

```bash
export BRAIN_TOKEN=abt_...            # in your shell profile, so a tool started from the Dock sees it too
brain connect claude cursor --url https://brain.example.co/mcp --token-env BRAIN_TOKEN --name brain-work
export BRAIN_INSTANCE=https://brain.example.co
brain read brief                      # and apply, held, approve, refuse, receipt work the same way
```

Your token decides your agent: a packet under another agent's name is refused before it's logged. An approver approves with their own token: `brain held`, then `brain approve <change id>`.

## Tokens

```bash
docker compose exec brain brain hub token --instance /data/brain list
docker compose exec brain brain hub token --instance /data/brain revoke <public id>     # takes effect at once
docker compose exec brain brain hub audit --instance /data/brain --since 2026-10-01 [--agent alex]
```

Tokens last 365 days: issue a new one and revoke the old to rotate. A valid token is never refused by the failed sign-in limit: a flood of bad tokens from one address (every outside client shares one address behind a tunnel) gets 429 only for bad tokens, with no audit row past the limit. Config changes are: edit `brain.toml` on the volume, then `docker compose exec brain brain apply-config --instance /data/brain`.

### Folder tokens

A token can name folders, and then every read it makes shows only what is filed in them:

```bash
docker compose exec brain brain hub token --instance /data/brain issue --agent claude --scope read --folder demo [--folder other] --label "Cloud agent, demo only"
```

Each folder must exist. The token sees each named folder, every folder inside it (worked out on every request, so a folder added later is included), and the records, decisions, actions, checklist items and meetings filed in them, with the people, threads and sources those rows point at. Facts, systems, the write log and any record filed nowhere are never shown, and a reference to a row it can't see reads as empty. `brief` is scoped to the folders and drops the brain-wide lines, `search` and `records_query` find only what is filed there, and `fact_get` is refused with a message naming what to use instead. `/v1/receipts` and `/v1/held` answer 403 to it. A folder token reads only in v1: `--folder` with `--scope write` is refused. `token list` and `/v1/whoami` show `folders` (null for a whole-brain token). A hub made before this release gains the column the first time it reads its tokens, and its tokens stay whole-brain.

What a folder limit doesn't cover: a source cited both from inside the folders and from outside them is shown whole, and the text of a row inside the folders is shown as written, even where it talks about another folder. File and cite with that in mind before you give a folder token to someone who mustn't read the rest.

## Intake

The `fetch` service runs `brain-fetch run` every 30 minutes. Put each collector's credential in `/run/secrets/<collector>` (mode 0600) with a compose `secrets:` entry; `brain-fetch setup <service>` prints what each one needs. `./hub status` shows a stopped loop.

## Backups

The brain backs itself up into the `brain-backups` volume every 15 minutes when anything was written, and once a day regardless, with `hub.sqlite3` (tokens and audit) copied beside each bundle. It keeps every bundle for 24 hours, the newest of each day for 35 days, and every pre-update bundle for 35 days.

**Offsite:** `docker compose --profile offsite up -d` copies them to your own bucket every 15 minutes with `rclone copy --immutable`, so a changed same-name file fails instead of overwriting. Give that rclone remote a credential that can list and put objects and never delete (on S3: no `DeleteObject`, and `--s3-no-check-bucket` is already set). Turn bucket versioning on. Object lock is your choice: it also blocks redaction until its retention ends, so write down which you picked. Prune with the bucket's lifecycle rule, or with `./hub prune-offsite --remote <remote:path>` from an admin machine that holds a delete-capable credential the server never sees.

| Event | Recovery point | Recovery time |
|---|---|---|
| Bad update or corrupt store, disk intact | At most 15 minutes | About 5 minutes (unverified) |
| Server or disk lost | At most about 30 minutes, offsite | 15 to 30 minutes with the steps below (unverified) |

## Restore

```bash
docker compose stop brain fetch
docker compose run --rm brain sh -c 'mv /data/brain /data/broken-$(date -u +%Y%m%dT%H%M%SZ)'
docker compose run --rm brain brain restore /backups/<bundle>.tar.gz --dir /data/brain
docker compose run --rm brain sh -c 'cp /backups/<bundle>.hub.sqlite3 /data/brain/hub.sqlite3'
docker compose up -d
```

On a new server, copy the bundle and its `.hub.sqlite3` down from the bucket into the `brain-backups` volume first. Collectors catch up from the cursors in the bundle; agents resend anything newer, and identical `change_id`s are safe.

## Update

Nothing here fetches a release. Download the new agent-si wheel from the releases page, build a new image from it as in Install (a new `<version>` tag), then:

```bash
./hub update --image agent-si-hub:<new version>
```

`./hub update` with no `--image` refuses and says so. The command stops writes, takes a `pre-update` backup, switches `.env` to the new image (keeping the old in `.env.previous`), migrates, checks, starts, waits for `/readyz` and runs an MCP smoke test. On any failure it restores the pre-update bundle with the old image and exits non-zero with the reason. A defect found later: put the old image back from `.env.previous` and restore the pre-update bundle as above.

## Redact

```bash
docker compose exec brain brain redact <source id> --reason "..." --yes --instance /data/brain --backups /backups
./hub prune-offsite --remote <remote:path> --before <the redaction's UTC stamp, YYYYMMDDTHHMMSSZ>   # from the admin machine
```

The first replaces every older bundle in `/backups`; the second deletes every offsite bundle made before the redaction. With versioning on, expire the old versions too (a lifecycle rule on noncurrent versions). See [REDACTION.md](REDACTION.md) for every other place a copy can live.
