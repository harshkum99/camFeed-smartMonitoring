# Smart Cam Monitoring

An AI layer over CCTV cameras a customer already owns. Ask questions of the footage in plain
English, get answers grounded in evidence frames, and run real-time rules that do not bury the
operator in false alarms.

Design notes live in [`docs/`](docs/) — start with [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
system shape and [IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) for what is built and what
is next.

## Running it

Needs Postgres 17 with `pgvector`, Python 3.12+, ffmpeg, and Node 20+ for the console.

```bash
# Python side
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Database + a synthetic factory to develop against
createdb smartcam_dev
for f in db/migrations/*.sql; do psql -v ON_ERROR_STOP=1 -d smartcam_dev -f "$f"; done
.venv/bin/python scripts/seed_demo.py --dsn "dbname=smartcam_dev" --days 3

# Console
cd console && npm install && npm run build && cd ..

# Serve API + console together
.venv/bin/uvicorn smartcam.api.app:app --port 8420
```

Then open <http://localhost:8420>.

For console development with hot reload, run `npm run dev` in `console/` alongside the uvicorn
process — Vite proxies `/api` to port 8420, so fetch paths are identical in dev and production.

### Optional

| Variable | Effect |
|---|---|
| `SMARTCAM_GEMINI_KEY` | Use Gemini to interpret questions. Without it, an offline stub handles a fixed set of question shapes and honestly declines the rest. |
| `SMARTCAM_DSN` | Database connection string (default `dbname=smartcam_dev`). |

## Demos

```bash
.venv/bin/python scripts/demo_ask.py      # plain-English questions, including honest refusals
.venv/bin/python scripts/demo_rules.py    # rule authoring, then a shift with noise through it
.venv/bin/python scripts/demo_queries.py  # the query layer without the language model
```

## Site survey

`smartcam-survey` is a read-only tool that finds cameras on a customer LAN and grades what each
one can actually support. It changes nothing on their equipment.

```bash
.venv/bin/smartcam-survey run --cidr 192.168.1.0/24 --channels 16 --user admin --out survey
```

## Tests

```bash
.venv/bin/pytest                                        # unit + integration
psql -v ON_ERROR_STOP=1 -d smartcam_ci -f db/tests/guards.sql   # database guard rails
.venv/bin/python scripts/license_gate.py                # no AGPL / non-commercial dependencies
```

The licence gate is not optional housekeeping: Ultralytics' AGPL reaches SaaS deployment and
trained weights, and it arrives as a transitive dependency of half the computer-vision tutorials
on the internet. It runs first in CI so that a licence problem goes red before the tests do.
