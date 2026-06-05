# Architecture — firestorm-ngfs-data

Companion to `firestorm-goes-fire-data` and `firestorm-lightning-data`; same
bridge pattern (GHA cron → slim JSON in repo → frontend `fetch()` from
`raw.githubusercontent.com`). This doc records the non-obvious bits so a
future session doesn't have to re-derive them.

## Pipeline

```
RealEarth viewer (re-ngfs-pub.ssec.wisc.edu)   [public, handshake-gated]
    │   /api/products?products=NGFS-SCENE-CONUS-EAST   → list of times
    │   /api/shapes?products=…&date=…&time=…           → GeoJSON FeatureCollection
    ▼
fetch_ngfs.py  (GHA runner, ~80s cadence × 4 passes per dispatch ≈ 5 min real cadence)
    │  handshake (PHPSESSID + session.php echo + re-* headers)
    │  fetch most recent 6 frames per satellite (G19 East + G18 West)
    │  type-filter (Wildland Fire only — drop Industrial, Oil/Gas, Volcano)
    │  dedup by FEATURE_TRACKING_ID across frames (keep latest acq)
    │  cross-sat dedup at ~3km grid (keep higher FRP)
    │  slim 60+ properties → 14 fields per detection
    ▼
data/ngfs.json   (committed to repo)
    ▼
FIRESTORM index.html  fetch() → enrich existing incident layer (no new toggle)
```

## The data product

`NGFS-SCENE-CONUS-EAST` and `NGFS-SCENE-CONUS-WEST` — CIMSS/SSEC's Next
Generation Fire System "scene" detections from the GOES ABI sensors,
processed through their ML/rules-based classifier.

**Why SCENE, not EVENTS:** the operator-supplied URL was for the SCENE viewer.
EVENTS turns out to be **stale** in the public catalog (last update
2026-01-27). SCENE is the live feed, ~2-min cadence, with full feature-tracking
metadata. EVENTS may be live behind their keyed API but the public viewer
doesn't expose recent EVENTS data.

**Why CONUS, not Mesoscale or Full-Disk:** CONUS sectors run continuously,
are operationally complete, and match what FIRESTORM cares about. Mesoscale
sectors are limited regions; Full-Disk includes Mexico/Central America fire
activity that mostly noise for our US-focused operators (and the Mexico
oilfield flares are filtered out anyway).

## Type filter

NGFS classifies every detection. We keep wildfires, drop everything else:

| `TYPE_DESCRIPTION` | kept? | why |
|---|---|---|
| `Possible Wildland Fire` | ✅ | core signal |
| `Known Wildland Fire Incident` | ✅ | matched to NIFC IRWIN |
| `Possible Wildland Fire Near a Solar Farm` | ✅ | qualified wildfire |
| `Possible Wildland Fire Near a Persistent Emitter` | ✅ | qualified wildfire |
| `Industrial` | ❌ | refineries, plants |
| `Oil/Gas` | ❌ | flares (lots of these in Tabasco MX) |
| `Likely an Urban Source` | ❌ | building fires, etc. |
| `Volcano` | ❌ | volcanic thermal anomalies |

Filter is in `_slim_feature()` — single place to change.

## Dedup logic

Two layers:

1. **Within-satellite, across-frames:** same `FEATURE_TRACKING_ID` over
   multiple frames → keep the latest `ACQ_DATE_TIME`. Lookback is 6 frames
   (~12 min) which is enough to catch any fire that's still active without
   inflating the JSON with redundant past observations.
2. **Cross-satellite, single-frame:** G19/G18 overlap over the central US,
   so a fire there can show in both. Bucket at 0.03° (~3 km), keep the
   higher-FRP observation. Order-independent (sort by FRP desc first).

## The handshake — what's actually happening

`re-ngfs-pub.ssec.wisc.edu` is a public RealEarth viewer. The `/api/*` endpoints
serve native GeoJSON when called with the right session metadata. Three layers,
all stateless-replicable:

1. **First GET to `/`:** server sets `PHPSESSID` (PHP session) + `SERVERID`
   (load-balancer affinity) cookies.
2. **Client-mint hash echo:** the viewer JS generates a random hex string
   client-side, computes its md5, and calls
   `GET /util/session.php?sh=<hex>&md5=<md5(hex)>`. Server responds with the
   same `<hex>` echoed back. **The hash is not a secret** — its purpose is
   just to make every session unique. We mint our own per run.
3. **Per-request headers:** every `/api/shapes`, `/api/products` call sends:
   - `re-session-hash`: same hex from step 2
   - `re-session-id`: PHPSESSID cookie value
   - `re-access-key`: empty string (the public viewer doesn't use access keys)

CORS is `access-control-allow-origin: *` — they're plainly OK with browser
JS reading this from any origin. The 500s the prior probe hit were from
**missing the three headers**, not from anti-bot defenses.

Source: `js/RELoader.js` on the viewer page, plus headless Playwright XHR
interception (probe captured 2026-06-05).

## Cadence — beating GHA schedule throttling

GHA `schedule` fires ~hourly on free runners regardless of `*/5`. So the
workflow runs an internal loop (fetch → ~80s sleep → fetch, ×4 ≈ 5 min,
committing each pass) and re-dispatches itself via `gh workflow run` at the
end. `*/5` schedule is the dead-man restart only. `concurrency:
cancel-in-progress: false` prevents pile-up if schedule and self-dispatch
overlap. Public repo → unlimited free minutes.

This pattern was proven on `firestorm-lightning-data` 2026-05-31 and
`firestorm-goes-fire-data` 2026-05-31.

## Frontend integration (FIRESTORM index.html — v2_209+)

**Standing rule (operator, 2026-06-05):** NO new layer toggle. NGFS data
enriches the existing incident layer.

Three render modes inside the existing flow:

1. **Detection has `incident_name`** (SSEC matched it to an IRWIN incident) →
   the incident's popup gets an "NGFS LIVE" section with FRP, tracking ID,
   age, satellite. No new icon — same incident pin as before.
2. **No `incident_name` but within ~5km of an IRWIN-known fire** (we
   spatial-join against `incidents[]` ourselves as a backup) → same as above.
3. **No match at all** (NGFS sees something IRWIN doesn't) → render as a
   sub-bucket inside the active incidents list with a distinct dotted-ring
   icon + "PRE-IRWIN · NGFS" label. Operator sees the leading indicator
   without confusing it for a confirmed fire.

Sentinel correlation: object-growth (cross-frame FRP delta on the same
`tracking_id`) above threshold → CRITICAL ALERT in existing Sentinel panel.

AI tool exposure: existing `query_fires` / `query_active_incidents` tools
get NGFS context layered in transparently — when the AI summarizes a state's
fire activity, NGFS counts surface as part of "fires under detection."

## Caveats / gotchas

- **Cloud blocks detection.** Same as FDC — absent under cloud ≠ "no fire."
- **2 km coarse pixel.** A NGFS detection is an area, not a point. Don't
  imply pixel-precision in operator-facing copy.
- **G18 (West) is sparse.** Daytime CONUS-East is in G19's prime view; G18
  carries fewer detections. `counts.g18: 0` is normal, not a bug.
- **`tracking_id` does NOT mean confirmed fire.** SSEC's classifier marks
  every detection with an ID even if it's only been seen once. Object-growth
  signal needs at least 3+ frames of the same ID.
- **`incident_name` accuracy.** SSEC's IRWIN match is best-effort spatial.
  When we see a name, it's almost always right; when we don't see a name,
  there might still be an IRWIN incident nearby (we spatial-join ourselves
  as backup).
- **Polar regions / disk-edge.** `SATELLITE_ZENITH_ANGLE` past ~60° is
  unreliable. We don't currently filter on this — no operator complaints
  yet, revisit if false positives surface near 50°N+.
- **Bucket rotation gotcha.** G19=East, G18=West *today*. NOAA will
  eventually rotate G19→G20 (early 2030s); update `PRODUCTS` then.
- **SSEC handshake change.** If they tighten the gate (require referrer,
  add captcha, IP-bind sessions), the pipeline will start returning empty
  feature collections or 4xx. Pause the workflow, re-probe with Playwright,
  patch `_make_session()`. The frontend doesn't change because the schema
  is ours.

## Why this is ethically OK

CIMSS/SSEC put the data behind a public viewer with `access-control-allow-origin: *`
and a stateless handshake — the same posture as a public REST API. The data
itself is NOAA-funded public-domain research output. We:

- Stay below the raw 2-min cadence (5-min poll is plenty).
- Attribute everywhere it surfaces (badge, popup, AI brief).
- Have already emailed SSEC asking for the documented RealEarth API path.
  When they grant it, we swap the handshake for that path.

If SSEC ever tells us to stop, we stop. The Track B fallback (FDC + our own
object-tracking algorithm) is mid-design and would replace this within
~2 weeks.
