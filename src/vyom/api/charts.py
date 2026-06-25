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
    fig.savefig(path, dpi=90, bbox_inches="tight")
    plt.close(fig)
    return name


def _despine(ax):
    """Light, consistent styling: drop top/right spines, add a faint y-grid."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color("#cccccc")
    ax.spines["bottom"].set_color("#cccccc")
    ax.tick_params(colors="#555555", labelsize=8)
    ax.yaxis.grid(True, color="#e6e6e6", linewidth=0.7)
    ax.set_axisbelow(True)


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

    # Sequential shade by window order so pre→event→post reads left-to-right.
    palette = ["#a6bddb", "#3690c0", "#045a8d", "#016c59", "#810f7c"]
    colors = [palette[i % len(palette)] for i in range(len(values))]

    fig, ax = plt.subplots(figsize=(3.8, 2.5))
    bars = ax.bar(labels, values, color=colors, edgecolor="white", linewidth=0.8)
    ax.set_title(f"{metric.upper()} mean by window", fontsize=10)
    ax.set_ylabel(f"{metric} mean")
    ax.axhline(0, color="#888", linewidth=0.8)
    for b, v in zip(bars, values):
        ax.annotate(f"{v:.3f}", (b.get_x() + b.get_width() / 2, v),
                    ha="center", va="bottom" if v >= 0 else "top", fontsize=8)
    _despine(ax)
    name = f"chart_compare_{metric}_{_hash([labels, values])}.png"
    return _save(fig, out_dir, name)


def _chart_flood_extent(result: dict, out_dir: Path):
    water_pct = _num(result.get("water_area_pct"))
    if water_pct is None:
        return None
    water_pct = max(0.0, min(100.0, water_pct))
    dry_pct = max(0.0, 100.0 - water_pct)
    km2 = _num(result.get("estimated_water_area_km2"))
    scene = result.get("scene_id") or "scene"

    # Donut: water vs dry, with the water % and km² called out in the centre.
    fig, ax = plt.subplots(figsize=(3.3, 2.7))
    ax.pie([water_pct, dry_pct], colors=["#045a8d", "#e6eef3"],
           startangle=90, counterclock=False,
           wedgeprops=dict(width=0.42, edgecolor="white", linewidth=1.5))
    ax.text(0, 0.10, f"{water_pct:.2f}%", ha="center", va="center",
            fontsize=15, fontweight="bold", color="#045a8d")
    ax.text(0, -0.20, "water", ha="center", va="center",
            fontsize=9, color="#666666")
    title = "Flood extent (NDWI)"
    if km2 is not None:
        title += f"\n{km2:g} km² inundated"
    ax.set_title(title, fontsize=10)
    ax.set_aspect("equal")
    name = f"chart_flood_donut_{scene}_{_hash([water_pct, dry_pct, km2])}.png"
    return _save(fig, out_dir, name)


def _chart_compute_change(result: dict, out_dir: Path):
    inc = _num(result.get("pct_increased"))
    dec = _num(result.get("pct_decreased"))
    unc = _num(result.get("pct_unchanged"))
    if inc is None and dec is None and unc is None:
        return None
    inc, dec, unc = (inc or 0.0), (dec or 0.0), (unc or 0.0)
    index = result.get("index") or "index"

    fig, ax = plt.subplots(figsize=(4.0, 1.9))
    segments = [("Increased", inc, "#2166ac"),
                ("Decreased", dec, "#b2182b"),
                ("Unchanged", unc, "#bdbdbd")]
    left = 0.0
    for label, val, color in segments:
        ax.barh(0, val, left=left, color=color, edgecolor="white",
                height=0.5, label=f"{label} ({val:.1f}%)")
        if val >= 7:  # only annotate segments wide enough to hold the text
            ax.text(left + val / 2, 0, f"{val:.0f}%", ha="center", va="center",
                    color="white", fontsize=8, fontweight="bold")
        left += val
    ax.set_xlim(0, max(100.0, left))
    ax.set_ylim(-0.5, 0.5)
    ax.set_yticks([])
    ax.set_xlabel("% of valid pixels")
    ax.set_title(f"{index.upper()} change composition", fontsize=10)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.45),
              ncol=3, fontsize=7, frameon=False)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    name = f"chart_change_stack_{index}_{_hash([inc, dec, unc])}.png"
    return _save(fig, out_dir, name)


def _chart_burn_severity(result: dict, out_dir: Path):
    classes = result.get("classes")
    if not isinstance(classes, list) or not classes:
        return None
    # Drop "unburned" — the interesting story is the burned bands.
    burned = [c for c in classes if c.get("index", 0) >= 1]
    labels = [str(c.get("name", "")).replace("_", "-") for c in burned]
    values = [_num(c.get("pct")) or 0.0 for c in burned]
    if not any(values):
        return None
    colors = ["#ffffb2", "#fecc5c", "#fd8d3c", "#e31a1c"][:len(values)]

    fig, ax = plt.subplots(figsize=(3.6, 2.3))
    bars = ax.bar(labels, values, color=colors, edgecolor="#888", linewidth=0.4)
    km2 = _num(result.get("burned_area_km2"))
    title = "Burn severity (dNBR)"
    if km2 is not None:
        title += f" — {km2:g} km² burned"
    ax.set_title(title)
    ax.set_ylabel("% of valid pixels")
    for b, v in zip(bars, values):
        ax.annotate(f"{v:.1f}%", (b.get_x() + b.get_width() / 2, v),
                    ha="center", va="bottom", fontsize=8)
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right", fontsize=7)
    name = f"chart_burn_{_hash([labels, values, km2])}.png"
    return _save(fig, out_dir, name)


def _chart_compare_events(result: dict, out_dir: Path):
    events = result.get("events")
    metric = result.get("metric") or "metric"
    if not isinstance(events, list) or not events:
        return None
    labels, values = [], []
    for e in events:
        v = _num(e.get("mean"))
        if v is None:
            continue
        labels.append(str(e.get("event_key", "")))
        values.append(v)
    if not values:
        return None

    fig, ax = plt.subplots(figsize=(4.4, 0.6 + 0.45 * len(values)))
    ypos = list(range(len(values)))
    bars = ax.barh(ypos, values, color="#2c7fb8", edgecolor="white", linewidth=0.6)
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels, fontsize=7)
    ax.invert_yaxis()  # first event on top
    ax.set_xlabel(metric)
    ax.set_title(f"{metric} by event ({result.get('window', 'event')})", fontsize=10)
    vmax = max(values) if values else 1.0
    for b, v in zip(bars, values):
        ax.annotate(f"{v:.2f}", (v, b.get_y() + b.get_height() / 2),
                    xytext=(3, 0), textcoords="offset points",
                    ha="left", va="center", fontsize=7)
    ax.set_xlim(min(0, min(values)), vmax * 1.15)
    _despine(ax)
    ax.xaxis.grid(True, color="#e6e6e6", linewidth=0.7)
    ax.yaxis.grid(False)
    name = f"chart_events_hbar_{metric}_{_hash([labels, values])}.png"
    return _save(fig, out_dir, name)


_RENDERERS = {
    "compare_windows": _chart_compare_windows,
    "flood_extent": _chart_flood_extent,
    "compute_change": _chart_compute_change,
    "burn_severity": _chart_burn_severity,
    "compare_events": _chart_compare_events,
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
