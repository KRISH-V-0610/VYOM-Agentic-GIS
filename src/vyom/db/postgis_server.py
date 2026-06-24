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


# Register tools without rebinding the module-level names, so they stay directly
# importable/callable for the Phase-3 isolation gate.
mcp.tool()(check_coverage)
mcp.tool()(list_scenes)
mcp.tool()(get_scene_metrics)
mcp.tool()(scenes_by_date_range)
mcp.tool()(compare_windows)


if __name__ == "__main__":
    mcp.run()
