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

## Importing recorded footage

`smartcam-import` reads a folder of exported footage — a customer's DVR dump, or a public dataset
standing in for one — and reports what it actually holds before anything is ingested.

```bash
.venv/bin/smartcam-import ./footage --tz Asia/Kolkata
```

`--tz` is the timezone the **recorder** was set to, not yours. No export declares an offset, and a
wrong one shifts every answer by hours without any visible symptom.

The output that matters is the gap list: windows with no footage, which the product must refuse to
answer about rather than reporting as "nothing happened". See [docs/DATASETS.md](docs/DATASETS.md)
for which public datasets work as a DVR stand-in and, more importantly, which do not.

## Analysing recorded footage (first real slice)

The index can be built from real video rather than from the synthetic seeder. The worked example
uses public MEVA footage (CC BY 4.0) — see [docs/DATASETS.md](docs/DATASETS.md).

```bash
pip install -e ".[ingest]"          # onnxruntime, numpy, scipy, pillow
psql -d smartcam_dev -f db/migrations/005_recorded_ingest.sql

# The detector file is declared, with its sha256 and licence evidence, in third_party.toml.
# Download it to var/models/dfine_s_coco/model.onnx; the loader refuses any other file.

smartcam-ingest run  <footage dir> --site meva --tz America/New_York --convention meva
smartcam-ingest score --site meva --annotations <MEVA KPF dir> --write-capabilities
python scripts/demo_meva.py

SMARTCAM_TENANT=<printed by run> SMARTCAM_SITE=<printed by run> \
  .venv/bin/uvicorn smartcam.api.app:app --port 8420
```

About 4 minutes of CPU per 5-minute 1080p clip at 5 fps on an M2 laptop. `--tz` is required and
must be the recorder's timezone; a wrong one moves every track by hours with no visible symptom.
Keyframes and run logs go to `var/smartcam` (override with `SMARTCAM_DATA_DIR`); source footage is
only ever read.

Any screen or slide showing MEVA footage must carry its attribution, which the console prints in
its footer. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Evidence bundles and the section 63 certificate draft

From any answer with evidence frames, the console's **Prepare evidence bundle** (or the CLI) packs
the original recordings behind it — each re-verified against the SHA-256 taken when it was imported
— with the frames, index records, a hash report, a Merkle root, and a pre-filled draft of the
Bharatiya Sakshya Adhiniyam 2023 section 63(4) Schedule certificate for the person in charge of the
recorder and an expert to review and sign. The product never signs and never claims admissibility.

```bash
smartcam-evidence create --site meva --purpose "Complaint 42, stairwell" \
  --question "How many people were at Admin G329 between 11:55 and 12:00 today?"
smartcam-evidence verify var/smartcam/bundles/<tenant>/<site>/<bundle>.zip --dsn dbname=smartcam_dev
```

Original recordings are looked up under `SMARTCAM_FOOTAGE_DIR` (default `var/footage`). A bundle
can be checked without any of this software: `shasum -a 256 -c SHA256SUMS` and
`python3 verify_bundle.py .` inside the extracted folder. See
[docs/research/bsa-s63-certificate.md](docs/research/bsa-s63-certificate.md) for what the law asks
for and what the generator may and may not fill in.

## Tests

```bash
.venv/bin/pytest                                        # unit + integration
psql -v ON_ERROR_STOP=1 -d smartcam_ci -f db/tests/guards.sql   # database guard rails
.venv/bin/python scripts/license_gate.py                # no AGPL / non-commercial dependencies
```

The licence gate is not optional housekeeping: Ultralytics' AGPL reaches SaaS deployment and
trained weights, and it arrives as a transitive dependency of half the computer-vision tutorials
on the internet. It runs first in CI so that a licence problem goes red before the tests do.

## Deploying the console to Vercel

Vercel hosts the console. It does **not** host the API — that needs Postgres and long-lived
connections, so it stays on a real server (the Indian cloud node), which also keeps camera data
in-country for the DPDP posture.

So the deployment is a split, and both halves need configuring or the console will load and every
request will fail:

1. **On Vercel**, set a build-time environment variable:

   ```
   VITE_API_BASE = https://api.your-domain.com
   ```

   `vercel.json` already sets the build command, the output directory, and the single-page
   rewrites, so a bookmarked `/alerts` does not 404.

2. **On the API server**, allow the Vercel origin:

   ```
   SMARTCAM_ALLOWED_ORIGINS=https://your-app.vercel.app,https://console.your-domain.com
   ```

   An explicit list, never `*` — this API answers questions about a customer's premises, and a
   wildcard would let any page a logged-in operator visits query it from their browser.

With `VITE_API_BASE` unset, the console uses relative paths and FastAPI serves it from
`console/dist` on the same origin. That is the simpler deployment and the one the air-gapped SKU
uses; Vercel is for demos and for putting the console on a CDN.
