"""
Tool registry — the bridge between the Gemini agent and VYOM's two MCP servers.

The agent does NOT speak MCP-over-the-wire. It calls the tool functions directly,
which is exactly what the Phase-3 design enabled by keeping every tool importable as a
plain module-level function. The standalone FastMCP servers still exist for external /
IDE clients; this module reuses the same functions in-process.

Each tool entry carries:
  - fn         : the Python callable
  - declaration: a neutral JSON-schema dict (Gemini / OpenAPI shape) describing it

GeoJSON arguments are declared as JSON STRINGS, not nested objects. Gemini's function
schema support for free-form nested objects is unreliable, so the agent passes GeoJSON
as a JSON string and `coerce_args` parses it back to a dict before dispatch.

Two convenience tools (`list_events`, `get_event_aoi`) are defined here so the agent can
turn a natural-language event name into a concrete AOI polygon without the user supplying
coordinates.
"""

import json

from ..config import load_events_registry
from ..db import postgis_server as pg
from ..db import gis_server as gis


# ── Event helpers (NL event name -> concrete AOI) ────────────────────────────────


def list_events() -> dict:
    """List disaster events registered in VYOM with their AOI bbox and primary index.

    Returns:
        {"count": int, "events": [{key, hazard_type, display_name, bbox,
         event_date, primary_index}, ...]}
    """
    reg = load_events_registry()
    events = [
        {
            "key": e["key"],
            "hazard_type": e.get("hazard_type"),
            "display_name": e.get("display_name"),
            "bbox": e.get("bbox"),
            "event_date": e.get("event_date"),
            "primary_index": e.get("primary_index"),
        }
        for e in reg.values()
    ]
    return {"count": len(events), "events": events}


def _bbox_to_polygon(bbox: list) -> dict:
    """[min_lon, min_lat, max_lon, max_lat] -> GeoJSON Polygon (closed ring)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return {
        "type": "Polygon",
        "coordinates": [[
            [min_lon, min_lat],
            [max_lon, min_lat],
            [max_lon, max_lat],
            [min_lon, max_lat],
            [min_lon, min_lat],
        ]],
    }


def get_event_aoi(event_key: str) -> dict:
    """Resolve a registered event to a GeoJSON AOI polygon from its bbox.

    Use this to obtain the `aoi_geojson` argument for spatial tools when the user
    refers to an event by name rather than supplying coordinates.

    Args:
        event_key: registered event key (e.g. 'kerala_periyar_2018').

    Returns:
        {"event_key": str, "hazard_type": str, "event_date": str,
         "primary_index": str, "bbox": [...], "aoi_geojson": {GeoJSON Polygon}}
    """
    reg = load_events_registry()
    if event_key not in reg:
        return {
            "error": f"Unknown event_key {event_key!r}.",
            "available_events": sorted(reg.keys()),
        }
    e = reg[event_key]
    return {
        "event_key": event_key,
        "hazard_type": e.get("hazard_type"),
        "event_date": e.get("event_date"),
        "primary_index": e.get("primary_index"),
        "bbox": e.get("bbox"),
        "aoi_geojson": _bbox_to_polygon(e["bbox"]),
    }


# ── Neutral JSON-schema declarations (Gemini/OpenAPI shape) ───────────────────────
# GeoJSON params are STRING (JSON-encoded) — see module docstring.

_AOI_STR = {
    "type": "STRING",
    "description": "AOI as a JSON-encoded GeoJSON geometry/Feature/FeatureCollection "
                   "(WGS84). Obtain it from get_event_aoi when the user names an event.",
}

DECLARATIONS = [
    {
        "name": "list_events",
        "description": "List all disaster events registered in VYOM (key, hazard type, "
                       "AOI bbox, date, primary index). Call this to discover what "
                       "events exist when the user names a place/disaster.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "get_event_aoi",
        "description": "Resolve a registered event_key to a GeoJSON AOI polygon (from its "
                       "bbox). Use the returned aoi_geojson string for spatial tools.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "event_key": {"type": "STRING", "description": "Registered event key."},
            },
            "required": ["event_key"],
        },
    },
    {
        "name": "check_coverage",
        "description": "MANDATORY FIRST CALL for any spatial question. Honest "
                       "'do we have data here?' check: returns scene_count, events, "
                       "sensors and date range intersecting the AOI.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"aoi_geojson": _AOI_STR},
            "required": ["aoi_geojson"],
        },
    },
    {
        "name": "list_scenes",
        "description": "List catalogued scenes with optional filters. Returns scene "
                       "metadata including available_assets (which COGs exist per scene).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "event_key": {"type": "STRING"},
                "window_type": {"type": "STRING",
                                "description": "pre_event | event | post_event | annual"},
                "sensor": {"type": "STRING", "description": "LISS3 | LISS4 | AWiFS"},
                "max_cloud": {"type": "NUMBER", "description": "Max cloud_cover percent."},
                "limit": {"type": "INTEGER", "description": "Max rows (default 100)."},
            },
        },
    },
    {
        "name": "get_scene_metrics",
        "description": "Fetch CACHED index metrics for one scene (full-scene, or for an "
                       "AOI if aoi_geojson given). Cheap SQL lookup — no pixel work.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "scene_id": {"type": "STRING"},
                "aoi_geojson": _AOI_STR,
                "metrics": {"type": "ARRAY", "items": {"type": "STRING"},
                            "description": "Optional metric names to filter."},
            },
            "required": ["scene_id"],
        },
    },
    {
        "name": "scenes_by_date_range",
        "description": "Find scenes acquired within a date window, optionally intersecting "
                       "an AOI and/or filtered by event.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "start_date": {"type": "STRING", "description": "ISO date, inclusive."},
                "end_date": {"type": "STRING", "description": "ISO date, inclusive."},
                "aoi_geojson": _AOI_STR,
                "event_key": {"type": "STRING"},
                "limit": {"type": "INTEGER"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "compare_windows",
        "description": "Compare a cached metric across an event's temporal windows "
                       "(pre/event/post) with deltas + plain-language interpretation. "
                       "Best for 'how much did X change?' questions using cached data.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "event_key": {"type": "STRING"},
                "metric": {"type": "STRING",
                           "description": "e.g. water_area_pct, ndwi_mean, nbr_mean."},
                "aoi_geojson": _AOI_STR,
                "windows": {"type": "ARRAY", "items": {"type": "STRING"},
                            "description": "Window order; default pre_event,event,post_event."},
            },
            "required": ["event_key", "metric"],
        },
    },
    {
        "name": "compute_change",
        "description": "Pixel-wise index change between two scenes (delta = B − A). "
                       "HEAVY: reads rasters. Use for direct two-scene comparison "
                       "(e.g. pre-event scene A vs event scene B).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "scene_id_a": {"type": "STRING", "description": "Baseline scene."},
                "scene_id_b": {"type": "STRING", "description": "Comparison scene."},
                "index": {"type": "STRING", "description": "ndvi | ndwi | nbr | mndwi."},
                "aoi_geojson": _AOI_STR,
            },
            "required": ["scene_id_a", "scene_id_b", "index"],
        },
    },
    {
        "name": "clip_to_aoi",
        "description": "Clip one scene's COG asset to an AOI and compute summary stats. "
                       "HEAVY: reads rasters. Caches stats for later cheap lookup.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "scene_id": {"type": "STRING"},
                "asset": {"type": "STRING",
                          "description": "ndwi|ndvi|nbr|mndwi|green|red|nir|swir1|cloud_mask"},
                "aoi_geojson": _AOI_STR,
            },
            "required": ["scene_id", "asset", "aoi_geojson"],
        },
    },
    {
        "name": "flood_extent",
        "description": "Binary flood map from NDWI threshold over a scene — the primary "
                       "flood tool. Returns water pixels, % and estimated km². HEAVY: "
                       "reads rasters. Caches results.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "scene_id": {"type": "STRING"},
                "aoi_geojson": _AOI_STR,
                "threshold": {"type": "NUMBER",
                              "description": "NDWI water threshold (default 0.3)."},
            },
            "required": ["scene_id"],
        },
    },
    {
        "name": "export_png",
        "description": "Render a scene product as a PNG thumbnail (false_color, or an "
                       "index map). HEAVY: reads rasters. Returns the saved file path.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "scene_id": {"type": "STRING"},
                "product": {"type": "STRING",
                            "description": "false_color | ndvi | ndwi | nbr | mndwi"},
                "aoi_geojson": _AOI_STR,
            },
            "required": ["scene_id"],
        },
    },
]

# name -> python callable
REGISTRY = {
    "list_events": list_events,
    "get_event_aoi": get_event_aoi,
    "check_coverage": pg.check_coverage,
    "list_scenes": pg.list_scenes,
    "get_scene_metrics": pg.get_scene_metrics,
    "scenes_by_date_range": pg.scenes_by_date_range,
    "compare_windows": pg.compare_windows,
    "compute_change": gis.compute_change,
    "clip_to_aoi": gis.clip_to_aoi,
    "flood_extent": gis.flood_extent,
    "export_png": gis.export_png,
}

# Tools that read pixels (slow) — surfaced to the orchestrator for logging/telemetry.
HEAVY_TOOLS = {"compute_change", "clip_to_aoi", "flood_extent", "export_png"}

# Params that arrive as JSON strings from Gemini and must be parsed to dicts.
_JSON_PARAMS = {"aoi_geojson"}


def coerce_args(name: str, args: dict) -> dict:
    """Normalise model-supplied args before dispatch.

    - JSON-string GeoJSON params -> dict.
    - Drop null/empty-string optional args so Python defaults apply.
    """
    out = {}
    for k, v in (args or {}).items():
        if v is None or v == "":
            continue
        if k in _JSON_PARAMS and isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name}: {k} is not valid JSON GeoJSON: {exc}") from exc
        out[k] = v
    return out


def dispatch(name: str, args: dict) -> dict:
    """Execute a registered tool by name. Returns its dict result.

    Errors are caught and returned as {"error": ...} so the agent loop can see the
    failure and recover rather than crashing.
    """
    fn = REGISTRY.get(name)
    if fn is None:
        return {"error": f"Unknown tool {name!r}.", "available": sorted(REGISTRY)}
    try:
        return fn(**coerce_args(name, args))
    except Exception as exc:  # surfaced to the model as a tool error
        return {"error": f"{type(exc).__name__}: {exc}"}
