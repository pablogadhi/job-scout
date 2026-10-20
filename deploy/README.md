# Deploying job-scout on the home server

One Docker image runs every command; a systemd user timer runs `job-scout run` daily at 08:15
(Guatemala time). A failed or hung run emails you its log; the daily digest, sent even when
there's nothing new, is the "it ran" signal. There is no external
monitor: if the server itself is down, you notice anyway (it's also your DNS and DHCP).

## What's where

Everything lives in your home directory; nothing runs as root.

| Path on the server | What |
| --- | --- |
| `~/job-scout` | this repo (`config/` is mounted read-only into the container) |
| `~/job-scout/data/` | gitignored: database, reports, exports, the claude CLI's home and work dir |
| `~/job-scout/.env` | gitignored secrets and per-machine settings, mode 600 (template: `.env.example`) |
| `~/.config/systemd/user/job-scout*` | the timer and the two services (`deploy/systemd/`), as user units |
| `~/job-scout/instances/<name>/` | gitignored: another instance's `config/`, `data/` and `.env` ([below](#another-instance)) |

Needs Docker with the compose plugin, your user in the `docker` group (the container runs as
UID/GID 1000 and writes to `data/` as that user; rootless Docker would map it to another
UID), and systemd 251 or later (`systemctl --version`), for the `MONITOR_*` variables the
failure email uses. Docker from apt, Docker's packages or the snap all work: the units look
for `docker` in `/usr/local/bin`, `/usr/bin`, `/bin` and `/snap/bin` (their `PATH`).

## First install

```sh
# 1. Code and data directory. The container writes data/ as UID/GID 1000: if `id` shows
#    another UID, export JOB_SCOUT_UID=$(id -u) JOB_SCOUT_GID=$(id -g) before building.
git clone git@github.com:pablogadhi/job-scout.git ~/job-scout
mkdir -p ~/job-scout/data      # before compose runs, or Docker creates it owned by root

# 2. Secrets: uncomment and fill in CLAUDE_CODE_OAUTH_TOKEN and
#    JOB_SCOUT_SCORING__CLI__TOKEN_CREATED (the day you ran `claude setup-token`),
#    ANTHROPIC_API_KEY, JOB_SCOUT_SMTP_TOKEN, JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS, the
#    feedback page's JOB_SCOUT_FEEDBACK_SECRET (`openssl rand -hex 32`) and
#    JOB_SCOUT_FEEDBACK__BASE_URL (see "Feedback page" below), and FINDWORK_API_KEY when
#    you have one
cd ~/job-scout
cp .env.example .env && chmod 600 .env
$EDITOR .env

# 3. Optional: keep your local history (scores, what digests already reported). On your
#    laptop: `uv run --env-file .env job-scout export --sqlite`, then copy the snapshot:
#    scp data/exports/job-scout-<time>.db server:job-scout/data/job-scout.db

# 4. Build the image (the database is created or migrated at the start of every command)
cd ~/job-scout/deploy
docker compose build
docker compose run --rm -T job-scout sources check

# 5. Company boards: the full detection (15-30 minutes; safe to interrupt and re-run)
docker compose run --rm -T job-scout ats detect
```

## Schedule it

User units run while you're logged in; lingering keeps your user manager running from boot,
so the timer fires without a login.

```sh
loginctl enable-linger "$USER"                 # once; may ask for your password
mkdir -p ~/.config/systemd/user
cp ~/job-scout/deploy/systemd/job-scout* ~/.config/systemd/user/
systemd-analyze --user verify ~/.config/systemd/user/job-scout.timer \
  ~/.config/systemd/user/job-scout.service ~/.config/systemd/user/job-scout-failure.service
systemctl --user daemon-reload
systemctl --user enable --now job-scout.timer
systemctl --user list-timers job-scout.timer   # next run: tomorrow 08:15
```

If `journalctl --user` shows nothing, your user can't read its own journal (a volatile
journal): add yourself to the `systemd-journal` group, or the failure email arrives without
the run's log.

## Feedback page

Each digest item gets 👍/👎 links to a small page served by the `feedback` compose service.
The page shows the job and Claude's score with your vote preselected; nothing is saved
until you press Save (add a reason if you like). Your recent judgments then go into the
scoring prompt as examples, and `job-scout feedback report` shows how often Claude agreed.

```sh
# Pick a name for the server in your DNS (e.g. jobscout.lan -> the server's LAN IP), and
# set JOB_SCOUT_FEEDBACK__BASE_URL=http://jobscout.lan:8765 in ~/job-scout/.env.
cd ~/job-scout/deploy
docker compose up -d feedback             # restarts with Docker at boot
docker compose logs feedback              # "feedback page listening on http://0.0.0.0:8765"
curl -s -o /dev/null -w '%{http_code}\n' http://jobscout.lan:8765/f/1   # 403: no signature
```

- It's plain HTTP on your LAN. The links are signed, so nobody can vote on other jobs by
  editing a link; still, don't publish the port on the internet. From outside, use a VPN
  into your network (Tailscale or WireGuard; Proton VPN can't reach your LAN), then point
  `JOB_SCOUT_FEEDBACK__BASE_URL` at the address you'd use there.
- If your phone runs Proton VPN at home, turn on its "Allow LAN connections" setting.
- Changing the secret invalidates the links in earlier digests.

## Another instance

A second profile (another person's job search) runs from the same checkout and image, with
its own config, database and digest, and its own feedback page if it uses votes. Everything
it owns lives in `~/job-scout/instances/<name>/` (gitignored, never in git), laid out like
the repo root:

| Path | What |
| --- | --- |
| `instances/<name>/config/` | its five config files (`settings.yaml`, `sources.yaml`, `filters.yaml`, `profile.md`, `scoring.yaml`) |
| `instances/<name>/data/` | its database, reports, exports |
| `instances/<name>/.env` | its secrets and recipient, plus the three compose settings below (mode 600) |

Its `.env` starts with the lines that make compose use the instance:

```sh
COMPOSE_PROJECT_NAME=job-scout-<name>          # its own containers and network
JOB_SCOUT_INSTANCE_DIR=../instances/<name>     # relative to deploy/
JOB_SCOUT_FEEDBACK_PORT=8766                   # a host port no other instance uses
```

Every compose command for the instance adds `--env-file ../instances/<name>/.env`. Don't
export those three variables in your shell: shell values win over the file, and would
point the command at the wrong instance. The ngo instance:

```sh
# 1. Copy the instance over from the laptop (it isn't in git), and create its data dir
#    before compose runs (or Docker creates it owned by root)
scp -r instances/ngo server:job-scout/instances/
ssh server 'mkdir -p ~/job-scout/instances/ngo/data && chmod 600 ~/job-scout/instances/ngo/.env'

# 2. Check it (the image is shared: no build needed if yours is current)
cd ~/job-scout/deploy
docker compose --env-file ../instances/ngo/.env run --rm -T job-scout sources check

# 3. Its daily run: the template service plus the instance's own timer (08:30)
cp ~/job-scout/deploy/systemd/job-scout@.service ~/job-scout/deploy/systemd/job-scout@ngo.timer \
  ~/.config/systemd/user/
systemd-analyze --user verify ~/.config/systemd/user/job-scout@ngo.timer \
  ~/.config/systemd/user/job-scout@ngo.service
systemctl --user daemon-reload
systemctl --user enable --now job-scout@ngo.timer
systemctl --user list-timers 'job-scout*'      # yours at 08:15, ngo at 08:30

# 4. Only for an instance that uses votes: its feedback page, on its own port, with
#    JOB_SCOUT_FEEDBACK_SECRET and a matching JOB_SCOUT_FEEDBACK__BASE_URL in its .env.
#    ngo doesn't: without those two its digests carry no 👍/👎 links, so skip this.
docker compose --env-file ../instances/<name>/.env up -d feedback
```

- Your instance's units don't change: `job-scout.service`, `job-scout.timer` and
  `job-scout-failure.service` keep running it from the repo root.
- `job-scout@.service` is the same run for any instance, selected by name
  (`job-scout@ngo.service`); each instance has its own timer file, since a template can't
  give each instance its own time.
- Two instances never fetch at the same time: when both are due at once (the server booted
  after both run times), an instance's run waits for yours to finish (`After=` in the
  template). On a normal day, 08:15 and 08:30, it changes nothing.
- A failed run of any instance is reported by `job-scout-failure.service`, which runs as
  your instance: the failure email goes to you, never to the instance's recipient.
- Logs: `journalctl --user -u job-scout@ngo.service`. Commands:
  `docker compose --env-file ../instances/ngo/.env run --rm -T job-scout <command>`.
- After an update, restart each feedback page on the new image: `docker compose up -d
  feedback` for yours, and the same with the instance's `--env-file` for each instance
  that runs one.

On the laptop, run an instance's commands from its directory: relative paths (`config/`,
`data/`) then point into the instance, and uv still finds the project.

```sh
cd instances/ngo
uv run --env-file .env alembic -c ../../alembic.ini upgrade head
uv run --env-file .env job-scout sources check
uv run --env-file .env job-scout digest --dry-run   # data/reports/digest-preview.html
```

## Check it works (Phase 6 acceptance)

```sh
# A real run now (spends up to 5 Pro prompts, sends the digest):
systemctl --user start job-scout.service
journalctl --user -u job-scout.service -n 50 --no-pager

# The failure email, without breaking anything: a throwaway unit that fails and hands its
# log to the same failure service. You should get "job-scout: daily run failed (exit-code)".
systemd-run --user --unit=job-scout-failtest -p OnFailure=job-scout-failure.service \
  /bin/sh -c 'echo "deliberate failure for the Phase 6 check"; exit 1'
journalctl --user -u job-scout-failure.service -n 20 --no-pager
```

## Everyday

- Logs: `journalctl --user -u job-scout.service` (each run),
  `journalctl --user -u job-scout-failure.service`.
- Run a command: `cd ~/job-scout/deploy && docker compose run --rm -T job-scout <command>`,
  e.g. `status`, `digest --dry-run`, `export --sqlite` (files land in `~/job-scout/data`).
- How Claude's scores compare with your votes:
  `docker compose run --rm -T job-scout feedback report` (`--all` lists every vote).
- The initial backlog on the API (spends, about $1-2 with Haiku 5.5):
  `docker compose run --rm -T job-scout backlog --backend api`.
- Update: `cd ~/job-scout && git pull && cd deploy && docker compose build`,
  then `docker compose up -d feedback` to restart the page on the new image. Database
  migrations run on the next command. Re-copy the units if `deploy/systemd`
  changed, then `systemctl --user daemon-reload`.
- Changing the run time: edit `deploy/systemd/job-scout.timer` *and*
  `schedule.daily_run_time` in `config/settings.yaml` (a test checks they match). For
  another instance: `deploy/systemd/job-scout@<name>.timer` and its own `settings.yaml`.
- Updating the claude CLI: bump `CLAUDE_CODE_VERSION` in `Dockerfile` on purpose (its flags
  can change between releases), rebuild, then check one call:
  `docker compose run --rm -T job-scout score --max-calls 1`.
