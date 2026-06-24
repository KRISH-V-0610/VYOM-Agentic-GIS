"""
Parse ISRO/NRSC BAND_META.txt into a typed dict.

The file is a flat `Key= Value` list (note the inconsistent spacing around `=`).
We extract only the fields the ARD pipeline needs and build a WGS84 footprint
polygon from the product corner coordinates.
"""

from datetime import datetime, timezone
from pathlib import Path


def _parse_raw(path: Path) -> dict[str, str]:
    """Read the flat key=value file into a string->string dict."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            out[key.strip()] = val.strip()
    return out


def _f(raw: dict, key: str) -> float | None:
    val = raw.get(key, "").strip()
    if not val or val == "-NA-":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _parse_datetime(raw: dict) -> datetime:
    """SceneCenterTime like '18-NOV-2017 05:26:39.660053' (UTC for IRS)."""
    val = raw.get("SceneCenterTime", "").strip()
    # Drop fractional seconds for robust parsing, treat as UTC.
    val_clean = val.split(".")[0]
    dt = datetime.strptime(val_clean, "%d-%b-%Y %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


def _footprint_wkt(raw: dict) -> str:
    """Build a WGS84 POLYGON from the four product corners (UL,UR,LR,LL)."""
    corners = [
        (raw["ProdULLon"], raw["ProdULLat"]),
        (raw["ProdURLon"], raw["ProdURLat"]),
        (raw["ProdLRLon"], raw["ProdLRLat"]),
        (raw["ProdLLLon"], raw["ProdLLLat"]),
        (raw["ProdULLon"], raw["ProdULLat"]),  # close ring
    ]
    pts = ", ".join(f"{float(lon)} {float(lat)}" for lon, lat in corners)
    return f"POLYGON(({pts}))"


def parse_band_meta(path: str | Path) -> dict:
    """
    Parse BAND_META.txt -> dict with:
      acq_datetime, sun_elevation, sun_azimuth, bits_per_pixel, dn_max,
      utm_zone, satellite, sensor_code, lmin/lmax per band number,
      footprint_wkt, cloud_percent, raw (the full string dict).
    """
    path = Path(path)
    raw = _parse_raw(path)

    bits = int(_f(raw, "BitsPerPixel") or 10)
    dn_max = (2 ** bits) - 1

    # Radiance calibration coefficients, keyed by physical band number (B2..B5).
    lmin: dict[str, float] = {}
    lmax: dict[str, float] = {}
    for bn in ("B2", "B3", "B4", "B5"):
        lo = _f(raw, f"{bn}_Lmin")
        hi = _f(raw, f"{bn}_Lmax")
        if hi is not None:
            lmin[bn] = lo if lo is not None else 0.0
            lmax[bn] = hi

    return {
        "acq_datetime": _parse_datetime(raw),
        "sun_elevation": _f(raw, "SunElevationAtCenter"),
        "sun_azimuth": _f(raw, "SunAzimuthAtCenter"),
        "bits_per_pixel": bits,
        "dn_max": dn_max,
        "utm_zone": int(_f(raw, "ZoneNo") or 0),
        "satellite": raw.get("SatID", "").strip(),       # e.g. 'IRS-R2A'
        "sensor_code": raw.get("Sensor", "").strip(),    # e.g. 'L3', 'L4', 'AWF'
        "lmin": lmin,
        "lmax": lmax,
        "footprint_wkt": _footprint_wkt(raw),
        "cloud_percent": _f(raw, "CloudPercent"),
        "raw": raw,
    }
