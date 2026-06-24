"""
Server-side chart rendering for VYOM query responses.

Charts are produced **deterministically** from the agent's ``tool_calls`` after
``agent.run()`` — they do NOT depend on whether the LLM chose to format anything.
Each recognised tool result yields a small matplotlib PNG written to
``data/exports/`` with a content-hashed filename (no timestamps / randomness), so
repeated identical queries reuse the same file and the plugin can fetch it via
``GET /exports/{filename}``.

Recognised tools:
  • compare_windows → grouped bar of the metric mean across pre/event/post windows
  • flood_extent    → water vs dry % bar (with km² annotation)
  • compute_change  → bar of pct_increased / pct_decreased / pct_unchanged

This module imports matplotlib with the Agg backend — no display needed.
"""

import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _hash(payload) -> str:
    """Stable short hash of a JSON-serialisable payload for deterministic filenames."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.md5(blob).hexdigest()[:10]


def _num(v):
    """Coerce to float or return None (handles None / strings gracefully)."""
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _save(fig, out_dir: Path, name: str) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return name


def _chart_compare_windows(result: dict, out_dir: Path):
    # compare_windows returns ``windows`` as a dict: {window_name: {"mean": ...} | None}
    windows = result.get("windows")
    metric = result.get("metric") or "metric"
    if not isinstance(windows, dict) or not windows:
        return None

    labels, values = [], []
    for label, stats in windows.items():
        if not isinstance(stats, dict):
            continue
        val = _num(stats.get("mean"))
        if val is None:
            val = _num(stats.get("value"))
        if val is None:
            continue
        labels.append(str(label))
        values.append(val)

    if not values:
        return None

    fig, ax = plt.subplots(figsize=(5, 3.2))
    bars = ax.bar(labels, values, color="#2c7fb8")
    ax.set_title(f"{metric.upper()} mean by window")
    ax.set_ylabel(f"{metric} mean")
    ax.axhline(0, color="#888", linewidth=0.8)
    for b, v in zip(bars, values):
        ax.annotate(f"{v:.3f}", (b.get_x() + b.get_width() / 2, v),
                    ha="center", va="bottom" if v >= 0 else "top", fontsize=8)
    name = f"chart_compare_{metric}_{_hash([labels, values])}.png"
    return _save(fig, out_dir, name)


def _chart_flood_extent(result: dict, out_dir: Path):
    water_pct = _num(result.get("water_area_pct"))
    if water_pct is None:
        return None
    dry_pct = max(0.0, 100.0 - water_pct)
    km2 = _num(result.get("estimated_water_area_km2"))
    scene = result.get("scene_id") or "scene"

    fig, ax = plt.subplots(figsize=(4.5, 3.2))
    bars = ax.bar(["Water", "Dry"], [water_pct, dry_pct],
                  color=["#045a8d", "#bdbdbd"])
    title = "Flood extent (NDWI)"
    if km2 is not None:
        title += f" — {km2:g} km² water"
    ax.set_title(title)
    ax.set_ylabel("% of valid pixels")
    ax.set_ylim(0, 100)
    for b, v in zip(bars, [water_pct, dry_pct]):
        ax.annotate(f"{v:.2f}%", (b.get_x() + b.get_width() / 2, v),
                    ha="center", va="bottom", fontsize=8)
    name = f"chart_flood_{scene}_{_hash([water_pct, dry_pct, km2])}.png"
    return _save(fig, out_dir, name)


def _chart_compute_change(result: dict, out_dir: Path):
    inc = _num(result.get("pct_increased"))
    dec = _num(result.get("pct_decreased"))
    unc = _num(result.get("pct_unchanged"))
    if inc is None and dec is None and unc is None:
        return None
    inc, dec, unc = (inc or 0.0), (dec or 0.0), (unc or 0.0)
    index = result.get("index") or "index"

    fig, ax = plt.subplots(figsize=(4.8, 3.2))
    bars = ax.bar(["Increased", "Decreased", "Unchanged"], [inc, dec, unc],
                  color=["#2166ac", "#b2182b", "#bdbdbd"])
    ax.set_title(f"{index.upper()} change distribution")
    ax.set_ylabel("% of valid pixels")
    ax.set_ylim(0, 100)
    for b, v in zip(bars, [inc, dec, unc]):
        ax.annotate(f"{v:.1f}%", (b.get_x() + b.get_width() / 2, v),
                    ha="center", va="bottom", fontsize=8)
    name = f"chart_change_{index}_{_hash([inc, dec, unc])}.png"
    return _save(fig, out_dir, name)


_RENDERERS = {
    "compare_windows": _chart_compare_windows,
    "flood_extent": _chart_flood_extent,
    "compute_change": _chart_compute_change,
}


def render_charts(tool_calls, out_dir) -> list:
    """Render charts for every recognised tool call. Returns a list of filenames.

    Args:
        tool_calls: the ``tool_calls`` list from a /query result (each item has
            ``name`` and ``result``).
        out_dir: directory to write PNGs into (data/exports).

    Filenames are content-hashed and de-duplicated so a result that produced the
    same chart twice only lists it once.
    """
    out_dir = Path(out_dir)
    names = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        renderer = _RENDERERS.get(call.get("name"))
        if renderer is None:
            continue
        result = call.get("result")
        if not isinstance(result, dict) or "error" in result:
            continue
        try:
            name = renderer(result, out_dir)
        except Exception:
            name = None  # one bad chart must never sink the whole response
        if name and name not in names:
            names.append(name)
    return names
