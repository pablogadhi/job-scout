# Deploying job-scout on the home server

One Docker image runs every command; systemd runs `job-scout run` daily at 08:15
(Guatemala time). A failed or hung run emails you its log; the daily digest, sent even when
there's nothing new, is the "it ran" signal (DECISIONS.md P6-U1). There is no external
monitor: if the server itself is down, you notice anyway (it's also your DNS and DHCP).

## What's where

| Path on the server | What |
| --- | --- |
| `/opt/job-scout` | this repo (`config/` is mounted read-only into the container) |
| `/var/lib/job-scout` | database, reports, exports, the claude CLI's home and work dir |
| `/etc/job-scout/job-scout.env` | secrets, mode 600 (template: `deploy/job-scout.env.example`) |
| `/etc/systemd/system/job-scout*` | the timer and the two services (`deploy/systemd/`) |

Needs Docker with the compose plugin, and systemd 251 or later (`systemctl --version`), for
the `MONITOR_*` variables the failure email uses.

## First install

```sh
# 1. Code and data directory (the container runs as UID/GID 1000 by default;
#    build with JOB_SCOUT_UID=... JOB_SCOUT_GID=... to use another user)
sudo git clone <your remote> /opt/job-scout
sudo mkdir -p /var/lib/job-scout && sudo chown 1000:1000 /var/lib/job-scout

# 2. Secrets: fill in CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, JOB_SCOUT_SMTP_TOKEN,
#    JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS (and FINDWORK_API_KEY when you have one)
sudo install -d -m 700 /etc/job-scout
sudo install -m 600 /opt/job-scout/deploy/job-scout.env.example /etc/job-scout/job-scout.env
sudoedit /etc/job-scout/job-scout.env

# 3. In /opt/job-scout/config/settings.yaml, set scoring.cli.token_created to the date you
#    ran `claude setup-token`, so the digest warns 30 days before the token expires.

# 4. Optional: keep your local history (scores, what digests already reported). On your
#    laptop: `uv run --env-file .env job-scout export --sqlite`, then copy the snapshot:
#    scp data/exports/job-scout-<time>.db server:/tmp/ &&
#    sudo install -o 1000 -g 1000 -m 600 /tmp/job-scout-<time>.db /var/lib/job-scout/job-scout.db

# 5. Build the image (the database is created or migrated at the start of every command)
cd /opt/job-scout/deploy
sudo docker compose build
sudo docker compose run --rm -T job-scout sources check

# 6. Company boards: the full detection (15-30 minutes; safe to interrupt and re-run)
sudo docker compose run --rm -T job-scout ats detect
```

## Schedule it

```sh
sudo cp /opt/job-scout/deploy/systemd/job-scout* /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/job-scout.timer \
  /etc/systemd/system/job-scout.service /etc/systemd/system/job-scout-failure.service
sudo systemctl daemon-reload
sudo systemctl enable --now job-scout.timer
systemctl list-timers job-scout.timer          # next run: tomorrow 08:15
```

## Check it works (Phase 6 acceptance)

```sh
# A real run now (spends up to 5 Pro prompts, sends the digest):
sudo systemctl start job-scout.service
journalctl -u job-scout.service -n 50 --no-pager

# The failure email, without breaking anything: a throwaway unit that fails and hands its
# log to the same failure service. You should get "job-scout: daily run failed (exit-code)".
sudo systemd-run --unit=job-scout-failtest -p OnFailure=job-scout-failure.service \
  /bin/sh -c 'echo "deliberate failure for the Phase 6 check"; exit 1'
journalctl -u job-scout-failure.service -n 20 --no-pager
```

## Everyday

- Logs: `journalctl -u job-scout.service` (each run), `journalctl -u job-scout-failure.service`.
- Run a command: `cd /opt/job-scout/deploy && sudo docker compose run --rm -T job-scout <command>`,
  e.g. `status`, `digest --dry-run`, `export --sqlite` (files land in `/var/lib/job-scout`).
- The initial backlog on the API (spends, about $1-2 with Haiku 5.5):
  `sudo docker compose run --rm -T job-scout backlog --backend api`.
- Update: `cd /opt/job-scout && sudo git pull && cd deploy && sudo docker compose build`.
  Database migrations run on the next command. Re-copy the units if `deploy/systemd`
  changed, then `sudo systemctl daemon-reload`.
- Changing the run time: edit `deploy/systemd/job-scout.timer` *and*
  `schedule.daily_run_time` in `config/settings.yaml` (a test checks they match).
- Updating the claude CLI: bump `CLAUDE_CODE_VERSION` in `Dockerfile` on purpose (its flags
  can change between releases), rebuild, then check one call:
  `sudo docker compose run --rm -T job-scout score --max-calls 1`.
