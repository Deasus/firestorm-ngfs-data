# firestorm-ngfs-data

NGFS (Next Generation Fire System) detections for FIRESTORM, mirrored from
CIMSS/SSEC's RealEarth viewer (`re-ngfs-pub.ssec.wisc.edu`) into a single
slim JSON in this repo. Public read; FIRESTORM frontend pulls from
`raw.githubusercontent.com`.

**Data:** [`data/ngfs.json`](data/ngfs.json) — most recent ~12 minutes of
NGFS-SCENE-CONUS-EAST + NGFS-SCENE-CONUS-WEST detections, deduped across
satellites, slimmed to the fields FIRESTORM needs.

**Cadence:** every ~5 min via GitHub Actions self-re-dispatching loop. NGFS
itself updates every ~2 min, but we deliberately stay below that to be polite
to SSEC.

**Attribution:** *data: NGFS / CIMSS / NOAA*. NGFS is NOAA-funded research from
the Cooperative Institute for Meteorological Satellite Studies (CIMSS) at the
Space Science and Engineering Center (SSEC), University of Wisconsin–Madison.

---

## What NGFS adds vs FIRESTORM's existing fire pipelines

| Pipeline | What it gives | Cadence | Object tracking |
|---|---|---|---|
| `firestorm-lightning-data` (GLM) | Lightning flashes (ignition source) | ~5 min | n/a |
| `firestorm-goes-fire-data` (FDC) | Per-pixel fire detections, FRP | ~5 min | No — per-frame only |
| `firestorm-ngfs-data` (this) | Fire-OBJECT detections, FRP, IRWIN match | ~2 min | **Yes — `FEATURE_TRACKING_ID`** |
| FIRMS / VIIRS (consumed live) | High-res polar-orbit detections | ~3 hr / pass | No |

The single most important thing NGFS adds is **fire-object continuity**.
SSEC's classifier links pixel detections across frames into tracked objects
with a stable ID — which is the leading indicator for blow-up (object-level
growth rate is a much cleaner signal than per-frame FRP delta).

NGFS also ships a free **IRWIN correlation**: every detection has
`KNOWN_INCIDENT_NAME` if SSEC's classifier matched it to an active NIFC fire.
That's done before we ever see the data, no spatial-join cost on our side.

---

## Output schema

```jsonc
{
  "schema": "firestorm-ngfs/v1",
  "generated_utc": "2026-06-05T14:58:12Z",
  "newest_frame": {
    "GOES-19": "2026-06-05T14:56:18Z",
    "GOES-18": "2026-06-05T14:56:19Z"
  },
  "lookback_frames": 6,
  "counts": {
    "raw_g19": 58, "raw_g18": 5, "deduped": 62,
    "tracked_objects": 62, "matched_to_irwin": 2
  },
  "source": {
    "provider": "CIMSS/SSEC (University of Wisconsin–Madison)",
    "product_family": "NGFS — Next Generation Fire System",
    "products_used": ["NGFS-SCENE-CONUS-EAST", "NGFS-SCENE-CONUS-WEST"],
    "host": "re-ngfs-pub.ssec.wisc.edu",
    "attribution": "data: NGFS / CIMSS / NOAA"
  },
  "fetch_seconds": 8.4,
  "detections": [
    {
      "lat": 42.7536,
      "lng": -123.4919,
      "frp": 165.07,            // FEATURE_FRP if present, else FRP (MW)
      "feature_frp": 165.07,    // SSEC's tracked-feature aggregate FRP
      "tracking_id": "ID-2026-06-05T13:46:18.000Z_0007",
      "acq": "2026-06-05T14:56:18.000Z",
      "type_desc": "Known Wildland Fire Incident",
      "confidence": "nominal",
      "sat": "GOES-19",
      "scan_domain": "CONUS",
      "state": "OR", "county": "Douglas County", "gacc": "USNWCC",
      "incident_name": "Kent Creek",       // null if no IRWIN match
      "incident_type": "WF",                // WF, RX, etc.
      "fuel": "FBFM10:38,FBFM2:25,...",     // LANDFIRE fuel-model breakdown
      "land_cover": "Trees:62,Shrubs:23,...",
      "version": "NGFSv3.8.0"
    }
  ]
}
```

---

## How the handshake works

The viewer at `re-ngfs-pub.ssec.wisc.edu` is a public RealEarth instance. Its
`/api/shapes` endpoint returns NGFS detections as native GeoJSON, but it
expects three things present on each request:

1. `PHPSESSID` + `SERVERID` cookies — server-set on first GET to `/`.
2. A client-mint session-hash echoed back via `/util/session.php?sh=<hex>&md5=<md5(hex)>`.
   The viewer's own JS (`js/RELoader.js`) does the same — the hash is
   generated client-side, not from a secret.
3. Three custom headers on every data XHR: `re-session-hash`, `re-session-id`
   (= PHPSESSID value), `re-access-key` (empty string for the public viewer).

CORS is wide open (`access-control-allow-origin: *`), so this is plainly a
session-tracking mechanism, not a deny gate. Implementation in
[`fetch_ngfs.py`](fetch_ngfs.py) — single function, ~30 lines, stdlib only.

---

## Maintenance

- **No secrets, no API keys, no env vars.** stdlib only.
- **Polite cadence.** Don't drop the loop sleep below ~80 s.
- **Track A pending.** We've emailed SSEC asking for their documented RealEarth
  API path with a key. If they grant it, swap the handshake for that path
  (cleaner, future-proof against any handshake change). Email + plan live in the FIRESTORM main-app repo (private).
- **If the handshake breaks** (SSEC config-pushes a stricter gate): pause the
  workflow, capture viewer traffic with Playwright again, update
  `_make_session()`. Frontend stays unchanged because data shape is ours, not
  SSEC's.
- **Bucket rotation gotcha (cross-project lesson):** GOES-19 = East, GOES-18 = West
  *today*. If NOAA rotates G19→G20 (early 2030s), update `PRODUCTS` accordingly.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the deeper non-obvious bits.
