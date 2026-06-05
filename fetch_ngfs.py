#!/usr/bin/env python3
"""
FIRESTORM NGFS pipeline — pulls live NGFS-SCENE detections from CIMSS/SSEC's
RealEarth viewer (re-ngfs-pub.ssec.wisc.edu) for both GOES-East (CONUS) and
GOES-West (CONUS) sectors, slims them to a compact GeoJSON, and writes
data/ngfs.json for the FIRESTORM frontend to consume.

NGFS = Next Generation Fire System (CIMSS/SSEC, NOAA-funded). Adds two things
beyond our existing GOES ABI L2 FDC pipeline:

  1. ~2-min cadence (vs FDC's 5-min) — earlier detection of new starts.
  2. FEATURE_TRACKING_ID — fire-object continuity across frames. SSEC links
     pixel detections into tracked objects with a stable ID, which is the
     leading indicator for blow-up (object-level growth rate is a much
     cleaner signal than per-frame FRP delta).

Plus, every detection carries KNOWN_INCIDENT_NAME — SSEC's own correlation
against IRWIN / NIFC active incidents. That's free aggregate-to-incident
mapping for us.

Bridge pattern (matches firestorm-goes-fire-data, firestorm-lightning-data):
GHA cron → slim JSON in repo → frontend fetch() from raw.githubusercontent.com.

Auth: replicates the public RealEarth viewer's stateless handshake. No API key,
no JWT, no IP binding. PHPSESSID + SERVERID cookies + a client-mint hash echoed
back via /util/session.php + three re-* headers on each data XHR. CORS is wide
open (access-control-allow-origin: *). The data feed is NOAA-funded public data
processed by CIMSS — we attribute everywhere it surfaces. Polite cadence: every
~5 min, not the 2-min raw cadence (be polite to SSEC; ~5 min is plenty).
"""

import datetime as dt
import gzip
import hashlib
import http.cookiejar
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://re-ngfs-pub.ssec.wisc.edu"
PRODUCTS = [
    ("NGFS-SCENE-CONUS-EAST", "GOES-19"),
    ("NGFS-SCENE-CONUS-WEST", "GOES-18"),
]
LOOKBACK_FRAMES = 6  # ~12 min of detections per satellite (6 frames × ~2 min)
USER_AGENT = (
    "Mozilla/5.0 (compatible; firestorm-ngfs-pipeline/1.0; "
    "+https://github.com/Deasus/firestorm-ngfs-data) "
    "Chrome/120.0 Safari/537.36"
)
OUT = "data/ngfs.json"


def _make_session():
    """Build an opener + perform the cookie + handshake cycle the viewer does."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj)
    )
    opener.addheaders = [
        ("User-Agent", USER_AGENT),
        ("Accept", "*/*"),
        ("Accept-Encoding", "gzip, deflate"),
        ("Accept-Language", "en-US,en;q=0.5"),
    ]
    # 1. Land on the viewer page — server sets PHPSESSID + SERVERID.
    with opener.open(
        f"{BASE}/?products=NGFS-SCENE-CONUS-EAST&view=leaflet", timeout=30
    ) as r:
        r.read()
    phpsessid = next((c.value for c in cj if c.name == "PHPSESSID"), None)
    if not phpsessid:
        raise RuntimeError("RealEarth handshake: no PHPSESSID set on /")

    # 2. Client-mint a session-hash (stateless), md5 it, echo back via
    #    /util/session.php. The viewer source (js/RELoader.js) does the same.
    sh = secrets.token_hex(16)
    md5 = hashlib.md5(sh.encode()).hexdigest()
    with opener.open(
        f"{BASE}/util/session.php?sh={sh}&md5={md5}", timeout=30
    ) as r:
        raw = r.read()
        if r.headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        echoed = raw.decode("utf-8", errors="replace").strip()
    if echoed != sh:
        raise RuntimeError(
            f"RealEarth handshake: server echoed {echoed!r}, expected {sh!r}"
        )
    return opener, sh, phpsessid


def _re_get(opener, sh, phpsessid, path):
    """GET an /api/* path with the three re-* headers; return decoded JSON."""
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={
            "re-session-hash": sh,
            "re-session-id": phpsessid,
            "re-access-key": "",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": USER_AGENT,
            "Referer": f"{BASE}/",
        },
    )
    with opener.open(req, timeout=90) as r:
        raw = r.read()
        if r.headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw)


def _slim_feature(feat, satellite, scan_domain):
    """Project a SSEC NGFS feature down to the fields FIRESTORM actually uses.

    The raw feature has 60+ properties; the frontend cares about position,
    timestamp, FRP/intensity, the SSEC tracking-ID + KNOWN_INCIDENT match,
    and the type/confidence labels. Everything else is upstream metadata
    (LANDFIRE codes, NWS WFO, IMAGE_LINE coords, B5-band variants) — drop.

    Filters out non-wildfire detections (Industrial, Oil/Gas, Volcano,
    Urban Source) — SSEC has its own classifier and we trust it. Keeps
    "Possible Wildland Fire" + "Known Wildland Fire Incident" + the
    Near-Solar-Farm / Near-Persistent-Emitter qualified-wildfire variants.
    """
    p = feat.get("properties") or {}
    type_desc = p.get("TYPE_DESCRIPTION") or ""
    if (
        "Wildland Fire" not in type_desc
        and type_desc != "Known Wildland Fire Incident"
    ):
        return None
    geom = feat.get("geometry") or {}
    coords = geom.get("coordinates")
    if not coords or not isinstance(coords, list) or len(coords) < 2:
        return None
    lng = coords[0]
    lat = coords[1]
    if (
        not isinstance(lng, (int, float))
        or not isinstance(lat, (int, float))
        or abs(lat) > 90
        or abs(lng) > 180
    ):
        return None
    out = {
        "lat": round(lat, 5),
        "lng": round(lng, 5),
        "frp": p.get("FEATURE_FRP", p.get("FRP")),
        "feature_frp": p.get("FEATURE_FRP"),
        "tracking_id": p.get("FEATURE_TRACKING_ID"),
        "acq": p.get("ACQ_DATE_TIME"),
        "type_desc": p.get("TYPE_DESCRIPTION"),
        "confidence": p.get("CONFIDENCE"),
        "sat": satellite,
        "scan_domain": scan_domain,
        "state": p.get("STATE"),
        "county": p.get("COUNTY"),
        "gacc": p.get("GACC_ID"),
        "incident_name": (
            None
            if p.get("KNOWN_INCIDENT_NAME") in ("NULL", None, "")
            else p.get("KNOWN_INCIDENT_NAME")
        ),
        "incident_type": (
            None
            if p.get("KNOWN_INCIDENT_TYPE") in ("NULL", None, "")
            else p.get("KNOWN_INCIDENT_TYPE")
        ),
        "fuel": p.get("FUEL"),
        "land_cover": p.get("LAND_COVER"),
        "version": p.get("VERSION"),
    }
    # Drop nulls to keep the JSON tight.
    return {k: v for k, v in out.items() if v not in (None, "")}


def _fetch_product(opener, sh, phpsessid, product, satellite):
    """Fetch the most-recent N frames for one NGFS-SCENE product."""
    # 1) Get the times list.
    catalog = _re_get(
        opener, sh, phpsessid, f"/api/products?products={product}"
    )
    if not catalog or not isinstance(catalog, list):
        raise RuntimeError(f"products call returned {type(catalog).__name__}")
    times = catalog[0].get("times") or []
    if not times:
        return [], None
    recent = times[-LOOKBACK_FRAMES:]
    # 2) Fetch /api/shapes for each timestamp.
    features = []
    seen_track = {}  # tracking_id → most recent observation (dedup across frames)
    for ts in recent:
        # ts looks like "20260605.145618"
        date_part, time_part = ts.split(".")
        date_iso = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
        time_iso = f"{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}"
        path = (
            f"/api/shapes?products={product}&date={date_iso}"
            f"&time={urllib.parse.quote(time_iso)}"
            f"&bounds=&merge=none&notifications=false"
        )
        try:
            d = _re_get(opener, sh, phpsessid, path)
        except urllib.error.HTTPError as e:
            print(
                f"[{product}] {ts}: HTTPError {e.code}: {e.reason}",
                file=sys.stderr,
            )
            continue
        for feat in d.get("features") or []:
            slim = _slim_feature(feat, satellite, "CONUS")
            if not slim:
                continue
            features.append(slim)
        # be polite — half a second between time-slice fetches
        time.sleep(0.5)

    # 3) Dedup: if the same FEATURE_TRACKING_ID appears in multiple frames, keep
    #    the freshest (last) observation. That gives us per-object current state
    #    without inflating the JSON. (Frontend can still show count of frames
    #    per object via a separate field if we ever want growth rate.)
    deduped = {}
    untracked = []
    for f in features:
        tid = f.get("tracking_id")
        if not tid:
            untracked.append(f)
            continue
        prior = deduped.get(tid)
        # Keep the latest acq timestamp (lexicographic ISO works).
        if prior is None or (f.get("acq") or "") > (prior.get("acq") or ""):
            deduped[tid] = f
    final = list(deduped.values()) + untracked
    return final, recent[-1] if recent else None


def _to_iso(re_timestamp):
    """'20260605.145618' → '2026-06-05T14:56:18Z'."""
    if not re_timestamp:
        return None
    try:
        date_part, time_part = re_timestamp.split(".")
        return (
            f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
            f"T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}Z"
        )
    except Exception:
        return re_timestamp


def main():
    started = time.time()
    opener, sh, phpsessid = _make_session()

    by_sat = {}
    newest = {}
    counts = {}
    for product, satellite in PRODUCTS:
        try:
            feats, last_ts = _fetch_product(
                opener, sh, phpsessid, product, satellite
            )
        except Exception as e:
            print(
                f"[{product}] fetch failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            feats, last_ts = [], None
        by_sat[satellite] = feats
        newest[satellite] = _to_iso(last_ts)
        counts[satellite.lower().replace("-", "")] = len(feats)

    # Cross-sat dedup: a fire near the East/West overlap can show in both
    # satellites. Bucket at ~3 km (0.03°), prefer the higher-FRP observation
    # (matches the FDC pipeline's convention).
    all_feats = []
    for sat, feats in by_sat.items():
        all_feats.extend(feats)
    all_feats.sort(key=lambda f: -(f.get("frp") or 0.0))
    seen = set()
    unique = []
    for f in all_feats:
        key = (round(f["lat"] / 0.03), round(f["lng"] / 0.03))
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    # Object-level summary: how many distinct fire objects (by tracking_id)?
    tracked_objects = len(
        {f.get("tracking_id") for f in unique if f.get("tracking_id")}
    )
    matched_to_irwin = sum(1 for f in unique if f.get("incident_name"))

    out = {
        "schema": "firestorm-ngfs/v1",
        "generated_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newest_frame": newest,
        "lookback_frames": LOOKBACK_FRAMES,
        "counts": {
            "raw_g19": counts.get("goes19", 0),
            "raw_g18": counts.get("goes18", 0),
            "deduped": len(unique),
            "tracked_objects": tracked_objects,
            "matched_to_irwin": matched_to_irwin,
        },
        "source": {
            "provider": "CIMSS/SSEC (University of Wisconsin–Madison)",
            "product_family": "NGFS — Next Generation Fire System",
            "products_used": [p for p, _ in PRODUCTS],
            "host": "re-ngfs-pub.ssec.wisc.edu",
            "attribution": "data: NGFS / CIMSS / NOAA",
        },
        "fetch_seconds": round(time.time() - started, 2),
        "detections": unique,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(
        f"[ngfs] wrote {OUT}: {len(unique)} unique detections "
        f"({tracked_objects} tracked objects, {matched_to_irwin} matched to IRWIN). "
        f"G19={counts.get('goes19',0)} G18={counts.get('goes18',0)}. "
        f"newest_frame_g19={newest.get('GOES-19')} newest_frame_g18={newest.get('GOES-18')}"
    )


if __name__ == "__main__":
    main()
