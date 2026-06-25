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
        "description": (
            "List all disaster events registered in VYOM (key, hazard type, "
            "AOI bbox, date, primary index). Call this to discover what "
            "events exist when the user names a place/disaster. "
            "No arguments needed — call with empty args {}."
        ),
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
        "name": "find_scene_pairs",
        "description": (
            "Find scene pairs that physically overlap between two temporal windows "
            "(e.g. pre_event vs post_event). CALL THIS before compute_change to get "
            "scene IDs that are guaranteed to share valid pixels. Pairs are ranked by "
            "lowest combined cloud cover. Returns best_pair for convenience. "
            "If no same-sensor pairs exist the note field explains why and suggests "
            "falling back to compare_windows."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "event_key": {"type": "STRING"},
                "window_a": {"type": "STRING",
                             "description": "First window — default pre_event."},
                "window_b": {"type": "STRING",
                             "description": "Second window — default post_event."},
                "same_sensor": {"type": "BOOLEAN",
                                "description": "Require same sensor (default true). "
                                               "Set false only if no same-sensor pairs exist."},
                "aoi_geojson": _AOI_STR,
                "limit": {"type": "INTEGER", "description": "Max pairs (default 5)."},
            },
            "required": ["event_key"],
        },
    },
    {
        "name": "compare_events",
        "description": (
            "Rank MULTIPLE events by one cached metric in a window — the cross-event "
            "tool. Use for 'which flood was worse, 2019 or 2022 Assam?'. One call "
            "instead of several compare_windows. Returns events ranked by mean + a "
            "ranking list + interpretation."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "event_keys": {"type": "ARRAY", "items": {"type": "STRING"},
                               "description": "Two or more registered event keys."},
                "metric": {"type": "STRING",
                           "description": "Cached metric, e.g. water_area_pct, ndvi_mean."},
                "window": {"type": "STRING",
                           "description": "Window to evaluate per event (default event)."},
            },
            "required": ["event_keys", "metric"],
        },
    },
    {
        "name": "find_best_scene",
        "description": (
            "Pick the single lowest-cloud scene in an event/window — single-scene "
            "selector. Use BEFORE flood_extent / export_png / burn_severity to choose "
            "a clean target scene instead of guessing from list_scenes."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "event_key": {"type": "STRING"},
                "window_type": {"type": "STRING",
                                "description": "pre_event | event | post_event | annual"},
                "sensor": {"type": "STRING", "description": "LISS3 | LISS4 | AWiFS"},
                "max_cloud": {"type": "NUMBER", "description": "Optional cloud_cover cap."},
            },
            "required": ["event_key"],
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
        "name": "burn_severity",
        "description": (
            "Classified burn-severity map from dNBR between a PRE-fire and POST-fire "
            "scene — the primary WILDFIRE tool (analog of flood_extent). Returns burned "
            "area per USGS severity class (unburned/low/moderate_low/moderate_high/high) "
            "+ total burned km² + a styled GeoTIFF. HEAVY: reads rasters. Requires NBR "
            "(SWIR) — works on LISS3/AWiFS, NOT LISS4. Pair scenes with find_scene_pairs."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "scene_id_a": {"type": "STRING", "description": "PRE-fire baseline scene."},
                "scene_id_b": {"type": "STRING", "description": "POST-fire scene."},
                "aoi_geojson": _AOI_STR,
            },
            "required": ["scene_id_a", "scene_id_b"],
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
    "find_scene_pairs": pg.find_scene_pairs,
    "compare_events": pg.compare_events,
    "find_best_scene": pg.find_best_scene,
    "compute_change": gis.compute_change,
    "burn_severity": gis.burn_severity,
    "clip_to_aoi": gis.clip_to_aoi,
    "flood_extent": gis.flood_extent,
    "export_png": gis.export_png,
}

# Tools that read pixels (slow) — surfaced to the orchestrator for logging/telemetry.
HEAVY_TOOLS = {"compute_change", "burn_severity", "clip_to_aoi", "flood_extent", "export_png"}

# Stamp additionalProperties:false on every declaration so strict-mode APIs
# (and models that respect the schema) know extra keys are not allowed.
for _d in DECLARATIONS:
    _p = _d.get("parameters")
    if isinstance(_p, dict):
        _p["additionalProperties"] = False

# Params that arrive as JSON strings from Gemini and must be parsed to dicts.
_JSON_PARAMS = {"aoi_geojson"}

# Schema-aware whitelist: tool_name -> set of declared parameter names.
# Built once at import time from DECLARATIONS so coerce_args can silently drop
# any arg the tool never declared (phantom keys, hallucinated names, empty-string
# keys from no-arg tools) without raising — eliminating unnecessary retries.
_DECLARED_PARAMS: dict[str, set[str]] = {
    decl["name"]: set((decl.get("parameters") or {}).get("properties") or {})
    for decl in DECLARATIONS
}


def coerce_args(name: str, args: dict) -> dict:
    """Normalise model-supplied args before dispatch.

    - Drop undeclared args (phantom keys, hallucinated names, LLM quirks like
      {"": {}}) — silently, so no retry is needed.
    - Drop null / empty-string values so Python defaults apply.
    - JSON-string GeoJSON params -> dict.
    """
    declared = _DECLARED_PARAMS.get(name)  # None only for unknown tools
    out = {}
    for k, v in (args or {}).items():
        # Drop empty-string keys and null/empty values.
        if not k or v is None or v == "":
            continue
        # Drop args the tool never declared (hallucinated / phantom params).
        if declared is not None and k not in declared:
            continue
        if k in _JSON_PARAMS and isinstance(v, str):
            parsed = None
            # LLMs sometimes add stray trailing braces — try progressive stripping.
            for attempt in (v, v.rstrip("}") + "}", v.rstrip("}")):
                try:
                    parsed = json.loads(attempt)
                    break
                except json.JSONDecodeError:
                    continue
            if parsed is None:
                raise ValueError(f"{name}: {k} is not valid JSON GeoJSON")
            v = parsed
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
