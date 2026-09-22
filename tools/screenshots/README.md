# Regenerating the README's images

`docs/images/dashboard.png` and `docs/images/issue-journey.gif` are captured from a real
dashboard serving **fabricated data**. Nothing in them is anyone's repository: the store is a
throwaway, the repository is `acme/frontend` — the placeholder the README and
`docs/toolchains.md` already use — and every issue, run, cost and token figure is invented in
`seed.py`.

Regenerate them when the dashboard's layout changes, or the images quietly stop describing it.

## Once per machine

Playwright's browser, about 150 MB into `~/.cache/ms-playwright`. No root, and nothing enters
this project's dependencies:

```bash
uv run --with playwright playwright install chromium
```

## Every time

```bash
# 1. a throwaway store on an ephemeral port, and thirty days of invented history
docker compose --profile test up -d --wait test-db
export DATABASE_URL="postgresql://issuebot@127.0.0.1:$(docker compose port test-db 5432 | cut -d: -f2)/issuebot"
uv run python tools/screenshots/seed.py history

# 2. serve it
ISSUEBOT_WEB_PASSWORD=screenshot uv run issuebot web --port 8099 &

# 3. capture both images
uv run --with playwright --with pillow python tools/screenshots/capture.py \
  --password screenshot --dsn "$DATABASE_URL"

# 4. throw the store away -- `rm -sf test-db`, never `compose down`, which is project-wide
kill %1
docker compose rm -sf test-db
```

`capture.py` drives `seed.py` itself for each of the four journey stages, so step 1 is the only
seeding you do by hand.

## The size limit is the constraint

`check-added-large-files` runs with its default, so **each file must stay under 500 KB**. The
capture prints both sizes and exits non-zero if either is over, rather than leaving a commit to
be rejected later. If a redesign pushes the GIF over, turn `--width` (900 by default) down
before `--colours` (64): the board is flat colour, so it quantises well and loses more to
resampling than to palette.

Raising the hook's `maxkb` to fit a picture would weaken a guard that covers the whole
repository. Shrink the picture instead.
