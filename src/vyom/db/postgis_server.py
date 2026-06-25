"""
PostGIS MCP server — the lightweight, catalog-only half of VYOM's tool layer.

STRICT IMPORT RULE (Phase 3):
    Only psycopg2, json, hashlib, datetime, os — plus the FastMCP framework itself.
    NEVER import rasterio, numpy, scipy, geopandas, or shapely here. This server must
    cold-start in under 2 seconds, which is only possible if no heavy geo/raster
    libraries are loaded. Pixel work lives in the separate gis_server.py.

This file deliberately does NOT import vyom.config, because config pulls in pyyaml and
python-dotenv. Instead it reads DB_URL using only `os` (env first, then a manual .env
parse), keeping the dependency surface exactly as small as the rule allows.

Tools are defined as plain module-level functions and then registered with the MCP
server, so they remain directly importable and unit-testable without a running server:

    from vyom.db.postgis_server import check_coverage
    check_coverage({"type": "Polygon", "coordinates": [...]})
"""

import json
import os

import psycopg2
from fastmcp import FastMCP

# Bumped whenever preprocessing changes — must match config.PIPELINE_VERSION. Kept as a
# literal here (not imported) to honour the strict-import rule.
PIPELINE_VERSION = "v1.0-dos"

mcp = FastMCP("vyom-postgis")


# ── DB connection (psycopg2 + os only) ──────────────────────────────────────────


def _project_root() -> str:
    # this file: <root>/src/vyom/db/postgis_server.py  →  up 4 levels to <root>
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))


def _load_db_url() -> str:
    """Resolve DB_URL from the environment, falling back to a manual .env parse.

    Uses only `os` — no python-dotenv — so it complies with the strict import rule.
    """
    url = os.getenv("DB_URL")
    if not url:
        env_path = os.path.join(_project_root(), ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    if key.strip() == "DB_URL":
                        url = val.strip().strip('"').strip("'")
                        break
    if not url or "<" in url:
        raise RuntimeError(
            "DB_URL not found. Set it in the environment or in <project_root>/.env "
            "(it must not still be a template containing '<')."
        )
    return url


def _connect():
    return psycopg2.connect(_load_db_url())


# ── GeoJSON helpers (pure-python, no shapely) ────────────────────────────────────


def _to_geometry(aoi_geojson: dict) -> dict:
    """Normalise a GeoJSON input down to a bare geometry dict.

    Accepts a raw geometry, a Feature, or a FeatureCollection (first feature used).
    Raises ValueError on anything unusable so callers fail loudly rather than silently
    querying an empty AOI.
    """
    if not isinstance(aoi_geojson, dict):
        raise ValueError("aoi_geojson must be a GeoJSON dict")

    gtype = aoi_geojson.get("type")
    if gtype == "FeatureCollection":
        features = aoi_geojson.get("features") or []
        if not features:
            raise ValueError("FeatureCollection has no features")
        return _to_geometry(features[0])
    if gtype == "Feature":
        geom = aoi_geojson.get("geometry")
        if not geom:
            raise ValueError("Feature has no geometry")
        return geom
    if gtype in ("Polygon", "MultiPolygon", "Point", "LineString",
                 "MultiPoint", "MultiLineString", "GeometryCollection"):
        return aoi_geojson
    raise ValueError(f"Unsupported GeoJSON type: {gtype!r}")


# ── TOOLS ────────────────────────────────────────────────────────────────────────


def check_coverage(aoi_geojson: dict) -> dict:
    """Honest 'do we have any data here?' check — the mandatory first agent call.

    Args:
        aoi_geojson: GeoJSON geometry / Feature / FeatureCollection for the AOI (WGS84).

    Returns:
        {
          "has_data": bool,
          "scene_count": int,
          "event_count": int,        # distinct events intersecting the AOI
          "date_range": [iso, iso] or None,
          "sensors": [str, ...],     # distinct sensors with coverage
          "events": [str, ...]       # distinct event_keys with coverage
        }
    """
    geom = _to_geometry(aoi_geojson)
    geom_json = json.dumps(geom)

    sql = """
        SELECT
            COUNT(*)                       AS scene_count,
            COUNT(DISTINCT event_key)      AS event_count,
            MIN(acq_datetime)              AS first_acq,
            MAX(acq_datetime)              AS last_acq,
            ARRAY_AGG(DISTINCT sensor)     FILTER (WHERE sensor IS NOT NULL)     AS sensors,
            ARRAY_AGG(DISTINCT event_key)  FILTER (WHERE event_key IS NOT NULL)  AS events
        FROM scenes
        WHERE ST_Intersects(
            geometry,
            ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)
        )
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (geom_json,))
        scene_count, event_count, first_acq, last_acq, sensors, events = cur.fetchone()

    scene_count = scene_count or 0
    date_range = (
        [first_acq.isoformat(), last_acq.isoformat()]
        if first_acq and last_acq
        else None
    )
    return {
        "has_data": scene_count > 0,
        "scene_count": scene_count,
        "event_count": event_count or 0,
        "date_range": date_range,
        "sensors": sorted(sensors) if sensors else [],
        "events": sorted(events) if events else [],
    }


def _aoi_hash(cur, geom_json: str) -> str:
    """Compute the scene_metrics.aoi_hash for an AOI — MD5(ST_AsText(aoi_geom)).

    Matches the schema's documented hash definition so a hash computed here lines up
    with whatever the heavy gis_server caches for the same AOI geometry.
    """
    cur.execute(
        "SELECT md5(ST_AsText(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))",
        (geom_json,),
    )
    return cur.fetchone()[0]


def _row_to_scene(row, cols) -> dict:
    """Map a DB row to a JSON-friendly scene dict (datetimes → isoformat)."""
    out = dict(zip(cols, row))
    if out.get("acq_datetime") is not None:
        out["acq_datetime"] = out["acq_datetime"].isoformat()
    if out.get("ingested_at") is not None:
        out["ingested_at"] = out["ingested_at"].isoformat()
    # assets is JSONB → psycopg2 returns it as a dict; expose available band keys.
    assets = out.pop("assets", None)
    if isinstance(assets, dict):
        out["available_assets"] = sorted(assets.keys())
    return out


# ── TOOL: list_scenes ─────────────────────────────────────────────────────────────


def list_scenes(
    event_key: str = None,
    window_type: str = None,
    sensor: str = None,
    max_cloud: float = None,
    limit: int = 100,
) -> dict:
    """List catalogued scenes with optional filters — the agent's 'what scenes exist?' tool.

    Args:
        event_key: restrict to one event (e.g. 'kerala_periyar_2018').
        window_type: 'pre_event' | 'event' | 'post_event' | 'annual'.
        sensor: short sensor label ('LISS3', 'LISS4', 'AWiFS').
        max_cloud: keep only scenes with cloud_cover <= this percent.
        limit: max rows (default 100).

    Returns:
        {"count": int, "scenes": [{id, collection, satellite, sensor, acq_datetime,
         cloud_cover, gsd_m, event_key, window_type, processing_level,
         pipeline_version, available_assets}, ...]}
        Ordered by acquisition time ascending.
    """
    cols = [
        "id", "collection", "satellite", "sensor", "acq_datetime", "cloud_cover",
        "gsd_m", "event_key", "window_type", "processing_level", "pipeline_version",
        "assets",
    ]
    where, params = [], []
    if event_key:
        where.append("event_key = %s")
        params.append(event_key)
    if window_type:
        where.append("window_type = %s")
        params.append(window_type)
    if sensor:
        where.append("sensor = %s")
        params.append(sensor)
    if max_cloud is not None:
        where.append("cloud_cover <= %s")
        params.append(max_cloud)

    sql = f"SELECT {', '.join(cols)} FROM scenes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY acq_datetime ASC LIMIT %s"
    params.append(int(limit))

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    scenes = [_row_to_scene(r, cols) for r in rows]
    return {"count": len(scenes), "scenes": scenes}


# ── TOOL: get_scene_metrics ───────────────────────────────────────────────────────


def get_scene_metrics(
    scene_id: str,
    aoi_geojson: dict = None,
    metrics: list = None,
) -> dict:
    """Fetch cached index metrics for one scene — turns pixel content into an SQL lookup.

    Args:
        scene_id: the scene to read metrics for.
        aoi_geojson: optional AOI. If omitted, returns whole-scene metrics
            (aoi_hash='full_scene'). If given, returns metrics cached for that
            exact AOI (hash = MD5(ST_AsText(aoi_geom))).
        metrics: optional list of metric names to filter (e.g. ['ndwi_mean',
            'water_area_pct']). If omitted, returns all cached metrics.

    Returns:
        {"scene_id": str, "aoi_hash": str, "pipeline_version": str|None,
         "metrics": {name: value, ...}, "found": bool}
    """
    with _connect() as conn, conn.cursor() as cur:
        if aoi_geojson is None:
            aoi_hash = "full_scene"
        else:
            geom = _to_geometry(aoi_geojson)
            aoi_hash = _aoi_hash(cur, json.dumps(geom))

        sql = (
            "SELECT metric, value, pipeline_version FROM scene_metrics "
            "WHERE scene_id = %s AND aoi_hash = %s"
        )
        params = [scene_id, aoi_hash]
        if metrics:
            sql += " AND metric = ANY(%s)"
            params.append(list(metrics))

        cur.execute(sql, params)
        rows = cur.fetchall()

    metric_map = {m: v for m, v, _ in rows}
    pipeline_version = rows[0][2] if rows else None
    return {
        "scene_id": scene_id,
        "aoi_hash": aoi_hash,
        "pipeline_version": pipeline_version,
        "metrics": metric_map,
        "found": bool(metric_map),
    }


# ── TOOL: scenes_by_date_range ────────────────────────────────────────────────────


def scenes_by_date_range(
    start_date: str,
    end_date: str,
    aoi_geojson: dict = None,
    event_key: str = None,
    limit: int = 100,
) -> dict:
    """Find scenes acquired within a date window, optionally intersecting an AOI.

    Args:
        start_date: ISO date/datetime, inclusive lower bound (e.g. '2018-08-01').
        end_date: ISO date/datetime, inclusive upper bound.
        aoi_geojson: optional AOI geometry/Feature/FeatureCollection (WGS84).
        event_key: optional event filter.
        limit: max rows (default 100).

    Returns:
        {"count": int, "date_range": [start, end], "scenes": [...]}
        Same scene shape as list_scenes, ordered by acquisition time ascending.
    """
    cols = [
        "id", "collection", "satellite", "sensor", "acq_datetime", "cloud_cover",
        "gsd_m", "event_key", "window_type", "processing_level", "pipeline_version",
        "assets",
    ]
    where = ["acq_datetime >= %s::timestamptz", "acq_datetime <= %s::timestamptz"]
    params = [start_date, end_date]

    if event_key:
        where.append("event_key = %s")
        params.append(event_key)
    if aoi_geojson is not None:
        geom = _to_geometry(aoi_geojson)
        where.append(
            "ST_Intersects(geometry, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))"
        )
        params.append(json.dumps(geom))

    sql = (
        f"SELECT {', '.join(cols)} FROM scenes WHERE "
        + " AND ".join(where)
        + " ORDER BY acq_datetime ASC LIMIT %s"
    )
    params.append(int(limit))

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    scenes = [_row_to_scene(r, cols) for r in rows]
    return {"count": len(scenes), "date_range": [start_date, end_date], "scenes": scenes}


# ── TOOL: compare_windows ─────────────────────────────────────────────────────────


def compare_windows(
    event_key: str,
    metric: str,
    aoi_geojson: dict = None,
    windows: list = None,
) -> dict:
    """Compare a cached metric across an event's temporal windows — change detection.

    Aggregates one metric (e.g. 'water_area_pct' for floods, 'nbr_mean' for burn
    scars) per window, so the agent can answer "how much did water grow from
    pre-event to event?" without touching pixels.

    Args:
        event_key: event to compare (e.g. 'kerala_periyar_2018').
        metric: metric name to aggregate (e.g. 'water_area_pct', 'ndwi_mean').
        aoi_geojson: optional AOI; defaults to whole-scene metrics ('full_scene').
        windows: window order to report; defaults to
            ['pre_event', 'event', 'post_event'].

    Returns:
        {
          "event_key": str, "metric": str, "aoi_hash": str,
          "windows": {window: {"mean": float, "min": float, "max": float,
                               "scene_count": int}},
          "deltas": {"event_vs_pre_event": float, "post_event_vs_pre_event": float, ...},
          "interpretation": str
        }
        Windows with no cached data are reported as null.
    """
    window_order = windows or ["pre_event", "event", "post_event"]

    with _connect() as conn, conn.cursor() as cur:
        if aoi_geojson is None:
            aoi_hash = "full_scene"
        else:
            geom = _to_geometry(aoi_geojson)
            aoi_hash = _aoi_hash(cur, json.dumps(geom))

        cur.execute(
            """
            SELECT s.window_type,
                   AVG(m.value), MIN(m.value), MAX(m.value), COUNT(*)
            FROM scene_metrics m
            JOIN scenes s ON s.id = m.scene_id
            WHERE s.event_key = %s AND m.metric = %s AND m.aoi_hash = %s
            GROUP BY s.window_type
            """,
            (event_key, metric, aoi_hash),
        )
        agg = {
            w: {
                "mean": round(float(avg), 6),
                "min": round(float(mn), 6),
                "max": round(float(mx), 6),
                "scene_count": int(n),
            }
            for w, avg, mn, mx, n in cur.fetchall()
        }

    windows_out = {w: agg.get(w) for w in window_order}

    # Deltas vs the baseline (first window in the requested order, usually pre_event).
    baseline = window_order[0]
    deltas = {}
    base_val = windows_out.get(baseline)
    if base_val is not None:
        for w in window_order[1:]:
            cur_val = windows_out.get(w)
            if cur_val is not None:
                deltas[f"{w}_vs_{baseline}"] = round(
                    cur_val["mean"] - base_val["mean"], 6
                )

    interp = _interpret_delta(metric, baseline, deltas)
    return {
        "event_key": event_key,
        "metric": metric,
        "aoi_hash": aoi_hash,
        "windows": windows_out,
        "deltas": deltas,
        "interpretation": interp,
    }


def _interpret_delta(metric: str, baseline: str, deltas: dict) -> str:
    """Plain-language read on the largest delta, honest about missing data."""
    if not deltas:
        return f"Not enough cached '{metric}' data across windows to compare."
    key, val = max(deltas.items(), key=lambda kv: abs(kv[1]))
    direction = "increased" if val > 0 else "decreased" if val < 0 else "did not change"
    return f"{metric} {direction} by {abs(val)} from {baseline} ({key})."


# ── TOOL: find_scene_pairs ────────────────────────────────────────────────────────


def find_scene_pairs(
    event_key: str,
    window_a: str = "pre_event",
    window_b: str = "post_event",
    same_sensor: bool = True,
    aoi_geojson: dict = None,
    limit: int = 5,
) -> dict:
    """Find scene pairs that physically overlap between two temporal windows.

    Call this BEFORE compute_change to guarantee the two scene IDs you pick will
    actually share valid pixels. Pairs are ranked by lowest combined cloud cover
    (best first), then by largest overlap area.

    Args:
        event_key: event to search (e.g. 'kerala_periyar_2018').
        window_a: first window — usually 'pre_event' or 'event'.
        window_b: second window — usually 'post_event' or 'event'.
        same_sensor: if True (default) only return pairs from the same sensor
            (LISS3↔LISS3 or LISS4↔LISS4). Pixel-wise change maps require this.
        aoi_geojson: optional AOI; pairs must intersect both the AOI and each other.
        limit: max pairs to return (default 5).

    Returns:
        {
          "count": int,
          "pairs": [{
            "scene_a_id": str, "scene_b_id": str, "sensor": str,
            "date_a": str, "date_b": str,
            "cloud_a": float, "cloud_b": float, "combined_cloud": float,
            "overlap_km2": float
          }, ...],
          "best_pair": {"scene_a_id": str, "scene_b_id": str} | null,
          "note": str   # set when no pairs found with same_sensor=True
        }
    """
    join_extra = "AND a.sensor = b.sensor" if same_sensor else ""

    aoi_clause = ""
    aoi_params: list = []
    if aoi_geojson is not None:
        geom = _to_geometry(aoi_geojson)
        geom_json = json.dumps(geom)
        aoi_clause = (
            "AND ST_Intersects(a.geometry, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))"
            " AND ST_Intersects(b.geometry, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))"
        )
        aoi_params = [geom_json, geom_json]

    sql = f"""
        SELECT
            a.id                                                   AS scene_a_id,
            b.id                                                   AS scene_b_id,
            a.sensor                                               AS sensor,
            a.acq_datetime::date                                   AS date_a,
            b.acq_datetime::date                                   AS date_b,
            COALESCE(a.cloud_cover, 100)                           AS cloud_a,
            COALESCE(b.cloud_cover, 100)                           AS cloud_b,
            COALESCE(a.cloud_cover, 100) + COALESCE(b.cloud_cover, 100)
                                                                   AS combined_cloud,
            ROUND(
                ST_Area(ST_Intersection(a.geometry, b.geometry)::geography) / 1e6
            )                                                      AS overlap_km2
        FROM scenes a
        JOIN scenes b ON (
            b.event_key = %s
            AND b.window_type = %s
            AND ST_Intersects(a.geometry, b.geometry)
            {join_extra}
        )
        WHERE a.event_key = %s
          AND a.window_type = %s
          {aoi_clause}
        ORDER BY combined_cloud ASC, overlap_km2 DESC
        LIMIT %s
    """
    # Param order must match %s positions in SQL:
    # JOIN b.event_key, b.window_type → WHERE a.event_key, a.window_type → aoi × 2 → limit
    params = [event_key, window_b, event_key, window_a] + aoi_params + [int(limit)]

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()

        # AOI area for overlap fraction — single extra query, only when AOI supplied.
        aoi_area_km2 = None
        if aoi_geojson is not None and rows:
            geom_json = json.dumps(_to_geometry(aoi_geojson))
            cur.execute(
                "SELECT ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)::geography)/1e6",
                (geom_json,),
            )
            aoi_area_km2 = float(cur.fetchone()[0])

    pairs = []
    for row in rows:
        r = dict(zip(cols, row))
        for f in ("date_a", "date_b"):
            if r.get(f) is not None:
                r[f] = r[f].isoformat()
        for f in ("cloud_a", "cloud_b", "combined_cloud", "overlap_km2"):
            if r.get(f) is not None:
                r[f] = float(r[f])

        # Overlap fraction + quality label.
        okm2 = r.get("overlap_km2")
        if okm2 is not None and aoi_area_km2 and aoi_area_km2 > 0:
            frac = min(okm2 / aoi_area_km2, 1.0)
            r["aoi_area_km2"] = round(aoi_area_km2, 1)
            r["overlap_pct"] = round(frac * 100, 1)
            r["overlap_quality"] = (
                "good" if frac >= 0.5 else "moderate" if frac >= 0.2 else "poor"
            )
        else:
            r["overlap_pct"] = None
            r["overlap_quality"] = None

        pairs.append(r)

    note = None
    if not pairs and same_sensor:
        note = (
            f"No same-sensor overlapping pairs found between {window_a} and {window_b} "
            f"for {event_key}. Try same_sensor=false to allow cross-sensor pairs "
            f"(note: cross-sensor pixel change maps are unreliable due to GSD mismatch)."
        )

    best = pairs[0] if pairs else None
    return {
        "count": len(pairs),
        "pairs": pairs,
        "best_pair": (
            {
                "scene_a_id": best["scene_a_id"],
                "scene_b_id": best["scene_b_id"],
                "sensor": best["sensor"],
                "date_a": best["date_a"],
                "date_b": best["date_b"],
                "cloud_a": best["cloud_a"],
                "cloud_b": best["cloud_b"],
                "overlap_km2": best["overlap_km2"],
                "overlap_pct": best["overlap_pct"],
                "overlap_quality": best["overlap_quality"],
            }
            if best else None
        ),
        "note": note,
    }


# ── TOOL: compare_events ──────────────────────────────────────────────────────────


def compare_events(
    event_keys: list,
    metric: str,
    window: str = "event",
) -> dict:
    """Rank multiple events by one cached metric in a chosen window — cross-event compare.

    Answers "which flood was worse, 2019 or 2022 Assam?" in a single deterministic call
    instead of the agent juggling several compare_windows results. Uses full-scene
    cached metrics (aoi_hash='full_scene').

    Args:
        event_keys: events to compare (e.g. ['assam_brahmaputra_2019',
            'assam_brahmaputra_2022']).
        metric: cached metric to rank on (e.g. 'water_area_pct', 'ndvi_mean').
        window: window to evaluate per event (default 'event'); e.g. 'event',
            'post_event', 'pre_event'.

    Returns:
        {
          "metric": str, "window": str,
          "events": [{"event_key", "mean", "min", "max", "scene_count"} ...]  # ranked desc by mean
          "ranking": [event_key, ...],   # highest mean first
          "interpretation": str
        }
        Events with no cached data for that metric/window are returned with null stats.
    """
    if not event_keys:
        return {"metric": metric, "window": window, "events": [], "ranking": [],
                "interpretation": "No event_keys supplied."}

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.event_key,
                   AVG(m.value), MIN(m.value), MAX(m.value), COUNT(*)
            FROM scene_metrics m
            JOIN scenes s ON s.id = m.scene_id
            WHERE s.event_key = ANY(%s)
              AND m.metric = %s
              AND m.aoi_hash = 'full_scene'
              AND s.window_type = %s
            GROUP BY s.event_key
            """,
            (list(event_keys), metric, window),
        )
        agg = {
            ek: {
                "mean": round(float(avg), 6),
                "min": round(float(mn), 6),
                "max": round(float(mx), 6),
                "scene_count": int(n),
            }
            for ek, avg, mn, mx, n in cur.fetchall()
        }

    # Preserve every requested event; null stats where no data was cached.
    events = []
    for ek in event_keys:
        stats = agg.get(ek)
        events.append({"event_key": ek, **(stats or {
            "mean": None, "min": None, "max": None, "scene_count": 0})})

    # Rank by mean (events with data only), highest first.
    with_data = [e for e in events if e["mean"] is not None]
    with_data.sort(key=lambda e: e["mean"], reverse=True)
    events_sorted = with_data + [e for e in events if e["mean"] is None]
    ranking = [e["event_key"] for e in with_data]

    if not with_data:
        interp = f"No cached '{metric}' data in the '{window}' window for these events."
    elif len(with_data) == 1:
        interp = (f"Only {with_data[0]['event_key']} has cached '{metric}' data "
                  f"(mean {with_data[0]['mean']}) in the '{window}' window.")
    else:
        top, bottom = with_data[0], with_data[-1]
        interp = (f"{top['event_key']} has the highest {metric} "
                  f"({top['mean']}) and {bottom['event_key']} the lowest "
                  f"({bottom['mean']}) in the '{window}' window.")

    return {
        "metric": metric,
        "window": window,
        "events": events_sorted,
        "ranking": ranking,
        "interpretation": interp,
    }


# ── TOOL: find_best_scene ───────────────────────────────────────────────────────


def find_best_scene(
    event_key: str,
    window_type: str = None,
    sensor: str = None,
    max_cloud: float = None,
) -> dict:
    """Pick the single lowest-cloud scene in an event/window — single-scene selector.

    The single-scene analog of find_scene_pairs. Use it to choose a concrete target
    scene for flood_extent / export_png / burn_severity instead of guessing from a
    list_scenes dump.

    Args:
        event_key: event to search (e.g. 'kerala_periyar_2018').
        window_type: optional window filter ('pre_event'|'event'|'post_event'|'annual').
        sensor: optional sensor filter ('LISS3'|'LISS4'|'AWiFS').
        max_cloud: optional cap on cloud_cover percent.

    Returns:
        {"found": bool, "scene": {id, sensor, satellite, acq_datetime, cloud_cover,
         gsd_m, window_type, available_assets} | null, "note": str|None}
        The chosen scene has the lowest cloud_cover (NULLs sorted last), ties broken
        by most recent acquisition.
    """
    cols = [
        "id", "collection", "satellite", "sensor", "acq_datetime", "cloud_cover",
        "gsd_m", "event_key", "window_type", "processing_level", "pipeline_version",
        "assets",
    ]
    where, params = ["event_key = %s"], [event_key]
    if window_type:
        where.append("window_type = %s")
        params.append(window_type)
    if sensor:
        where.append("sensor = %s")
        params.append(sensor)
    if max_cloud is not None:
        where.append("cloud_cover <= %s")
        params.append(max_cloud)

    sql = (
        f"SELECT {', '.join(cols)} FROM scenes WHERE "
        + " AND ".join(where)
        # NULLS LAST so a scene with a known low cloud beats an unknown one; then newest.
        + " ORDER BY cloud_cover ASC NULLS LAST, acq_datetime DESC LIMIT 1"
    )

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()

    if row is None:
        return {
            "found": False,
            "scene": None,
            "note": (f"No scenes match event={event_key!r}"
                     + (f", window={window_type!r}" if window_type else "")
                     + (f", sensor={sensor!r}" if sensor else "")
                     + (f", max_cloud={max_cloud}" if max_cloud is not None else "")
                     + "."),
        }
    return {"found": True, "scene": _row_to_scene(row, cols), "note": None}


# Register tools without rebinding the module-level names, so they stay directly
# importable/callable for the Phase-3 isolation gate.
mcp.tool()(check_coverage)
mcp.tool()(list_scenes)
mcp.tool()(get_scene_metrics)
mcp.tool()(scenes_by_date_range)
mcp.tool()(compare_windows)
mcp.tool()(find_scene_pairs)
mcp.tool()(compare_events)
mcp.tool()(find_best_scene)


if __name__ == "__main__":
    mcp.run()
