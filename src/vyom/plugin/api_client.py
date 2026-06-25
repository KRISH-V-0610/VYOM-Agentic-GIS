"""
Thin REST client for the VYOM FastAPI server (``python -m vyom.api``).

Deliberately Qt-free and dependency-free — it uses only the Python standard library
(``urllib``), so it imports and unit-tests fine outside QGIS. The dock widget wraps these
calls in a background QThread (see ``vyom_dockwidget.py``) to keep the QGIS UI responsive.

Endpoints mirrored here (see ``vyom/api/app.py``):
  GET  /health
  GET  /events
  GET  /scenes
  POST /query
  GET  /exports/{filename}      (binary PNG — fetched to a temp file)
"""

import html
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class VyomApiError(RuntimeError):
    """Raised on any non-2xx response or transport failure, with a human message."""


class VyomApiClient:
    """Blocking REST client. Call from a worker thread, never the GUI thread."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 300.0):
        # Trailing slash trips urljoin; normalise once here.
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._opener = self._build_opener(self.base_url)

    @staticmethod
    def _build_opener(base_url: str):
        """Build a urllib opener. For loopback hosts, bypass any configured HTTP proxy.

        QGIS users often sit behind a corporate proxy (HTTP(S)_PROXY in the environment),
        but the VYOM API runs on localhost — routing loopback through the proxy fails.
        An empty ProxyHandler forces a direct connection for 127.0.0.1 / localhost.
        """
        host = urllib.parse.urlparse(base_url).hostname or ""
        if host in {"127.0.0.1", "localhost", "::1"}:
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener()

    # ── low-level helpers ────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, params=None, body=None) -> bytes:
        url = self.base_url + path
        if params:
            # Drop None values so optional filters don't become "?x=None".
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = self._extract_detail(exc)
            raise VyomApiError(f"{exc.code} {exc.reason}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise VyomApiError(
                f"Cannot reach VYOM API at {self.base_url} — is it running "
                f"(`python -m vyom.api`)? [{exc.reason}]"
            ) from exc

    @staticmethod
    def _extract_detail(exc: "urllib.error.HTTPError") -> str:
        """Pull FastAPI's {"detail": ...} message out of an error body if present."""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            return payload.get("detail", payload)
        except Exception:
            return exc.reason

    def _get_json(self, path: str, params=None) -> dict:
        return json.loads(self._request("GET", path, params=params).decode("utf-8"))

    # ── public API ───────────────────────────────────────────────────────────────

    def health(self) -> dict:
        """GET /health — DB connectivity + Gemini-key presence."""
        return self._get_json("/health")

    def events(self) -> dict:
        """GET /events — registered disaster events with bbox + primary index."""
        return self._get_json("/events")

    def scenes(self, event_key=None, window_type=None, sensor=None,
               max_cloud=None, limit=100) -> dict:
        """GET /scenes — catalogued scenes (proxies list_scenes)."""
        return self._get_json("/scenes", {
            "event_key": event_key, "window_type": window_type, "sensor": sensor,
            "max_cloud": max_cloud, "limit": limit,
        })

    def query(self, question: str, max_steps: int = 12,
              include_history: bool = False, aoi_geojson: str = None) -> dict:
        """POST /query — run the agent on a natural-language question.

        Returns the QueryResponse dict: answer, tool_calls, coverage_checked,
        steps, stopped (+ history when include_history).

        ``aoi_geojson`` (a JSON-encoded GeoJSON string) is forwarded so a user-drawn
        AOI in QGIS overrides the event's default bounding box.
        """
        body = {"query": question, "max_steps": max_steps,
                "include_history": include_history}
        if aoi_geojson:
            body["aoi_geojson"] = aoi_geojson
        return json.loads(self._request("POST", "/query", body=body).decode("utf-8"))

    def query_stream(self, question: str, max_steps: int = 12,
                     aoi_geojson: str = None, on_event=None) -> dict:
        """POST /query/stream — SSE streaming endpoint.

        Calls ``on_event(event_dict)`` for each SSE event as it arrives.
        Blocks until the stream closes; returns the final ``done`` event dict.
        Falls back gracefully if the server doesn't support streaming.
        """
        body = {"query": question, "max_steps": max_steps, "include_history": False}
        if aoi_geojson:
            body["aoi_geojson"] = aoi_geojson
        data = json.dumps(body).encode("utf-8")
        url = self.base_url + "/query/stream"
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if on_event:
                        on_event(event)
                    if event.get("type") in ("done", "error"):
                        return event
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Server doesn't have the streaming endpoint yet — fall back.
                return self.query(question, max_steps, False, aoi_geojson)
            detail = self._extract_detail(exc)
            raise VyomApiError(f"{exc.code} {exc.reason}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise VyomApiError(
                f"Cannot reach VYOM API at {self.base_url} — is it running? "
                f"[{exc.reason}]"
            ) from exc
        return {}

    def download_export(self, filename: str, dest_dir=None) -> str:
        """GET /exports/{filename} — fetch a PNG to a local file, return its path.

        QGIS loads the file path as a raster layer. Files land in ``dest_dir`` (a temp
        dir by default) so repeated runs don't clobber the server's exports.
        """
        dest_dir = dest_dir or tempfile.gettempdir()
        os.makedirs(dest_dir, exist_ok=True)
        data = self._request("GET", "/exports/" + urllib.parse.quote(filename))
        out_path = os.path.join(dest_dir, filename)
        with open(out_path, "wb") as fh:
            fh.write(data)
        return out_path


def best_preprocessed_layer(query_result: dict, base_url: str) -> dict | None:
    """Return a vsicurl layer descriptor for the primary-index COG of the best scene.

    Used when no raster tool was called (catalog-only answer like compare_windows)
    so the map is never empty. Extracts the event_key from tool_call args, then
    the best scene_id from find_best_scene results (or compare_windows results).
    Returns None if no usable scene can be resolved.

    The returned dict has the same shape as georef_layers() entries so the same
    layer-addition code can handle both paths.
    """
    tool_calls = query_result.get("tool_calls") or []

    # 1. Collect event_key and any explicit scene_id from the tool trace.
    event_key = None
    window = "event"
    scene_id = None
    primary_index = None

    for call in tool_calls:
        name = call.get("name", "")
        args = call.get("args") or {}
        result = call.get("result") or {}

        if name in ("compare_windows", "compare_events"):
            event_key = event_key or args.get("event_key") or (
                (args.get("event_keys") or [None])[0])

        if name == "get_event_aoi":
            event_key = event_key or result.get("event_key")
            primary_index = primary_index or result.get("primary_index", "").lower()

        if name == "find_best_scene" and result.get("found"):
            scene = result.get("scene") or {}
            scene_id = scene.get("id")
            event_key = event_key or scene.get("event_key")

    if not event_key:
        return None

    # 2. If no scene_id yet, pick the best from find_best_scene result or list.
    if not scene_id:
        for call in tool_calls:
            if call.get("name") == "find_best_scene":
                sc = (call.get("result") or {}).get("scene") or {}
                scene_id = sc.get("id")
                break

    if not scene_id:
        return None

    # 3. Build the asset URL for the primary index COG via /scenes/{id}/assets.
    index_key = primary_index or "ndwi"
    # Map registry primary_index labels to asset keys.
    _INDEX_MAP = {
        "ndwi": "ndwi", "nbr": "nbr", "ndvi": "ndvi",
        "mndwi": "mndwi", "ndvi-anomaly": "ndvi", "dnbr": "nbr", "dndvi": "ndvi",
    }
    asset_key = _INDEX_MAP.get(index_key, "ndwi")

    try:
        url = base_url.rstrip("/") + f"/scenes/{urllib.parse.quote(scene_id)}/assets"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    assets = data.get("assets") or {}
    asset_url = assets.get(asset_key) or assets.get("ndwi") or assets.get("ndvi")
    if not asset_url:
        return None

    bbox = data.get("bbox_wgs84")  # [minlon, minlat, maxlon, maxlat]
    sensor = data.get("sensor") or ""
    acq = (data.get("acq_datetime") or "")[:10]
    layer_name = f"{asset_key.upper()} {sensor} {acq} (preprocessed)"

    # Build vsicurl URL for streaming the COG without downloading everything.
    vsicurl = "/vsicurl/" + base_url.rstrip("/") + asset_url

    return {
        "filename": None,              # signals vsicurl path, not a download
        "vsicurl_path": vsicurl,
        "crs": "EPSG:4326",
        "bounds_wgs84": bbox,
        "kind": ("flood" if asset_key == "ndwi" else
                 "burn"  if asset_key == "nbr"  else "index"),
        "layer_name": layer_name,
    }


def export_filenames(query_result: dict) -> list:
    """Extract export_png output filenames from a /query result's tool_calls.

    The agent's export_png tool returns {"output_path": ".../scene_product_hash.png"}.
    We only need the basename to fetch it back via /exports/{filename}.

    Retained for back-compat; new code should prefer ``georef_layers`` (which yields
    the georeferenced .tif outputs) and ``chart_filenames``.
    """
    names = []
    for call in query_result.get("tool_calls") or []:
        if call.get("name") != "export_png":
            continue
        result = call.get("result") or {}
        path = result.get("output_path")
        if path:
            names.append(os.path.basename(path))
    return names


def _kind_for(call_name: str, result: dict) -> str:
    """Classify a georeferenced result into a styling kind for the QGIS renderer."""
    if call_name == "flood_extent":
        return "flood"
    if call_name == "burn_severity":
        return "burn"
    if call_name == "compute_change":
        return "change"
    if call_name == "export_png":
        return "rgb" if result.get("product") == "false_color" else "index"
    return "index"


def georef_layers(query_result: dict) -> list:
    """Extract georeferenced raster layers from a /query result's tool_calls.

    Any tool result carrying a ``geotiff_path`` (flood_extent, compute_change,
    export_png) becomes a layer descriptor:

        {
          "filename": str,
          "crs": str|None,
          "bounds_wgs84": list|None,
          "kind": str,            # "rgb" | "flood" | "index" | "change"
          "layer_name": str,      # human-readable QGIS panel name
        }

    For compute_change the layer_name is "Δ{INDEX} {SENSOR} {date_a}→{date_b}",
    built from scene_a_meta / scene_b_meta that compute_change now returns.
    """
    layers = []
    for call in query_result.get("tool_calls") or []:
        result = call.get("result") or {}
        if not isinstance(result, dict):
            continue
        path = result.get("geotiff_path")
        if not path:
            continue
        kind = _kind_for(call.get("name"), result)

        # Prefer an explicit layer_name from the tool; fall back to a filename stem.
        layer_name = result.get("layer_name")
        if not layer_name:
            stem = os.path.splitext(os.path.basename(path))[0]
            layer_name = stem[:40]

        layers.append({
            "filename": os.path.basename(path),
            "crs": result.get("crs"),
            "bounds_wgs84": result.get("bounds_wgs84"),
            "kind": kind,
            "layer_name": layer_name,
        })
    return layers


def chart_filenames(query_result: dict) -> list:
    """Return the server-rendered chart PNG filenames from a /query result."""
    return list(query_result.get("charts") or [])


# ── Qt-free HTML rendering (unit-testable without QGIS) ────────────────────────


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _fmt(value) -> str:
    """Format a numeric value compactly; pass through non-numbers as escaped strings."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return str(value)
    return _esc(value)


def _table_compare_windows(result: dict) -> str:
    windows = result.get("windows")
    if not isinstance(windows, dict) or not windows:
        return ""
    metric = _esc(result.get("metric") or "metric")
    rows = [f"<b>compare_windows — {metric}</b>",
            "<table border=1 cellspacing=0 cellpadding=3>",
            "<tr><td><b>window</b></td><td><b>mean</b></td>"
            "<td><b>min</b></td><td><b>max</b></td><td><b>scenes</b></td></tr>"]
    for name, stats in windows.items():
        if not isinstance(stats, dict):
            rows.append(f"<tr><td>{_esc(name)}</td><td colspan=4>(no data)</td></tr>")
            continue
        rows.append(
            f"<tr><td>{_esc(name)}</td><td>{_fmt(stats.get('mean'))}</td>"
            f"<td>{_fmt(stats.get('min'))}</td><td>{_fmt(stats.get('max'))}</td>"
            f"<td>{_fmt(stats.get('scene_count'))}</td></tr>")
    rows.append("</table>")
    deltas = result.get("deltas") or {}
    if deltas:
        rows.append("<b>deltas:</b> " + ", ".join(
            f"{_esc(k)}={_fmt(v)}" for k, v in deltas.items()))
    interp = result.get("interpretation")
    if interp:
        rows.append(f"<i>{_esc(interp)}</i>")
    return "<br>".join(rows)


def _table_flood_extent(result: dict) -> str:
    keys = ["water_area_pct", "estimated_water_area_km2", "water_pixels",
            "valid_pixels", "threshold", "gsd_m"]
    rows = ["<b>flood_extent</b>", "<table border=1 cellspacing=0 cellpadding=3>"]
    for k in keys:
        if k in result and result[k] is not None:
            rows.append(f"<tr><td>{_esc(k)}</td><td>{_fmt(result[k])}</td></tr>")
    rows.append("</table>")
    return "<br>".join(rows)


def _table_compute_change(result: dict) -> str:
    keys = ["index", "mean_change", "std_change", "pct_increased",
            "pct_decreased", "pct_unchanged", "valid_pixel_pairs"]
    rows = ["<b>compute_change</b>", "<table border=1 cellspacing=0 cellpadding=3>"]
    for k in keys:
        if k in result and result[k] is not None:
            rows.append(f"<tr><td>{_esc(k)}</td><td>{_fmt(result[k])}</td></tr>")
    rows.append("</table>")
    return "<br>".join(rows)


_TABLE_RENDERERS = {
    "compare_windows": _table_compare_windows,
    "flood_extent": _table_flood_extent,
    "compute_change": _table_compute_change,
}


def build_result_html(query_result: dict) -> str:
    """Build a Qt-rich-text HTML fragment of result tables from the tool_calls.

    Uses only the Qt rich-text subset (``<table border=1>``, ``<b>``, ``<i>``) so it
    renders inside a QTextBrowser without external CSS. Returns "" if nothing to show.
    """
    blocks = []
    for call in query_result.get("tool_calls") or []:
        renderer = _TABLE_RENDERERS.get(call.get("name"))
        if renderer is None:
            continue
        result = call.get("result")
        if not isinstance(result, dict) or "error" in result:
            continue
        block = renderer(result)
        if block:
            blocks.append(block)
    return "<br><br>".join(blocks)


# ── Layer explanations (what a map layer represents, in plain language) ──────────

# kind → (title, HTML body). Shown in the chat when a raster layer lands on the map.
_LAYER_EXPLANATIONS = {
    "flood": (
        "🗺️ Flood layer added to the map",
        "<b>Blue</b> = water detected (NDWI &gt; threshold). Transparent = dry land. "
        "The blue extent is the inundated area. Zoom into river channels and low-lying "
        "basins — villages inside the blue patches were likely under water on this date.",
    ),
    "change": (
        "🗺️ Change map added to the map",
        "<b>Blue</b> = index increased here (e.g. new flooding / water gain). "
        "<b>Red</b> = index decreased (water receded / land exposed / vegetation lost). "
        "White = no change. Large blue patches after a flood show where water spread.",
    ),
    "burn": (
        "🗺️ Burn-severity layer added to the map",
        "USGS dNBR classes — pale <b>yellow</b> = low severity, <b>orange</b> = "
        "moderate, deep <b>red</b> = high severity (most biomass lost). Transparent = "
        "unburned. The coloured extent is the fire-affected area on this date.",
    ),
    "index": (
        "🗺️ Index layer added to the map",
        "Color scale: deep <b>blue</b> (+1) = open water; <b>yellow</b> (0) = "
        "transitional / moist soil; deep <b>red</b> (−1) = dry land or vegetation. "
        "Use the QGIS Identify tool to read the exact value of any pixel.",
    ),
    "rgb": (
        "🗺️ False-colour composite added to the map",
        "This is <b>not</b> a natural-colour photo (NIR/Red/Green). <b>Red</b> = healthy "
        "vegetation; dark blue/black = open water; bright white/cyan = cloud. Use it to "
        "visually assess vegetation health and water around the event date.",
    ),
}


def layer_explanation(kind: str):
    """Return (title, html_body) explaining a map layer of the given kind, or None."""
    return _LAYER_EXPLANATIONS.get(kind)


# ── Confidence badge (scientific-integrity signal for an answer) ─────────────────


def _max_cloud_in(query_result: dict):
    """Highest cloud_cover seen across tool results, or None if none reported."""
    clouds = []
    for call in query_result.get("tool_calls") or []:
        res = call.get("result")
        if not isinstance(res, dict):
            continue
        for key in ("cloud_cover", "cloud_cover_pct", "cloud_cover_percent"):
            v = res.get(key)
            if isinstance(v, (int, float)):
                clouds.append(float(v))
        # compare_windows nests stats per window
        windows = res.get("windows")
        if isinstance(windows, dict):
            for stats in windows.values():
                if isinstance(stats, dict):
                    v = stats.get("cloud_cover") or stats.get("cloud_cover_pct")
                    if isinstance(v, (int, float)):
                        clouds.append(float(v))
    return max(clouds) if clouds else None


# Tools that make data claims — confidence only applies when at least one ran.
_DATA_TOOLS = {
    "compare_windows", "list_scenes", "get_scene_metrics", "scenes_by_date_range",
    "flood_extent", "compute_change", "clip_to_aoi", "export_png",
}


def confidence_badge(query_result: dict):
    """Classify an answer's trustworthiness into (level, color, reason).

    Returns None when no data tools ran (e.g. greeting / conversational reply) —
    the caller should skip the badge entirely in that case.

    When data tools did run, returns (level, color, reason):
      level ∈ {"high","medium","low"};  color is a hex CSS string.
    """
    tool_calls = query_result.get("tool_calls") or []

    # No data tools ran → this is a conversational reply, no badge needed.
    data_calls = [c for c in tool_calls if c.get("name") in _DATA_TOOLS]
    if not data_calls:
        return None

    # At least one data tool ran — evaluate quality.
    if not query_result.get("coverage_checked"):
        return ("low", "#c0392b",
                "Coverage was not verified — treat this answer with caution.")

    for call in data_calls:
        res = call.get("result")
        if isinstance(res, dict) and "error" in res:
            return ("medium", "#e67e22",
                    "A tool returned an error; the answer may be partial.")

    # Spatial overlap quality from find_scene_pairs degrades confidence.
    for call in (query_result.get("tool_calls") or []):
        if call.get("name") == "find_scene_pairs":
            best = (call.get("result") or {}).get("best_pair") or {}
            quality = best.get("overlap_quality")
            pct = best.get("overlap_pct")
            if quality == "poor":
                pct_str = f"{pct:.0f}%" if pct is not None else "very little"
                return ("low", "#c0392b",
                        f"Scene overlap covers only {pct_str} of the AOI — "
                        f"change map is based on a small fraction of your area.")
            if quality == "moderate":
                pct_str = f"{pct:.0f}%" if pct is not None else "partial"
                return ("medium", "#e67e22",
                        f"Scene overlap covers {pct_str} of the AOI — "
                        f"results are representative but not complete.")
            break  # "good" overlap — fall through to cloud check

    cloud = _max_cloud_in(query_result)
    if cloud is not None and cloud >= 50:
        return ("low", "#c0392b",
                f"High cloud cover (~{cloud:.0f}%) — optical analysis is unreliable.")
    if cloud is not None and cloud >= 20:
        return ("medium", "#e67e22",
                f"Moderate cloud cover (~{cloud:.0f}%) — interpret with some caution.")
    return ("high", "#27ae60",
            "Coverage verified, cloud cover low, and scene overlap good.")


# ── Query templates per hazard type ─────────────────────────────────────────────

_TEMPLATES = {
    "flood": [
        "How did water area change across the pre-event, event, and post-event "
        "windows in {name}?",
        "Calculate the flood extent for the {name} event scene and tell me the "
        "inundated area in km².",
        "Show the NDWI change map between the pre-event and post-event scenes for {name}.",
        "Which scene in the {name} event window has the lowest cloud cover?",
        "Export a false-colour image of the {name} post-event scene.",
    ],
    "wildfire": [
        "How did NBR change before and after the {name} fire?",
        "Show the burn-severity (NBR change) map for {name}.",
        "Compare vegetation health (NDVI) before and after {name}.",
    ],
    "drought": [
        "How did NDVI change during the {name} drought period?",
        "Compare vegetation health before and during {name}.",
    ],
    "landslide": [
        "Show vegetation loss (NDVI change) between the pre- and post-event scenes "
        "for {name}.",
        "Compute the change in NDVI for {name} to map the landslide scar.",
    ],
}

_GENERIC_TEMPLATES = [
    "What data does VYOM have for {name}?",
    "List the scenes available for {name}.",
]


def query_templates(hazard_type: str, event_name: str) -> list:
    """Return example queries for a hazard, with the event name filled in."""
    base = _TEMPLATES.get((hazard_type or "").lower(), [])
    name = event_name or "this event"
    return [t.format(name=name) for t in (base + _GENERIC_TEMPLATES)]
