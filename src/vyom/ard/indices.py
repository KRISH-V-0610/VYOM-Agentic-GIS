"""
Spectral index computation and cloud masking, operating on reflectance arrays.

All indices use NaN-safe division so nodata pixels propagate as NaN rather than
producing spurious values. Index sign conventions follow the plan:
  - NDWI (McFeeters, green-NIR): WATER is POSITIVE  -> flood threshold NDWI > 0.3
  - NDVI: vegetation positive
  - NBR:  needs SWIR (LISS3/AWiFS only)
  - MNDWI: needs SWIR (LISS3/AWiFS only)

GPU: when CuPy is available, index math runs on GPU. Inputs and outputs are numpy
ndarrays — callers do not need to know about GPU.
"""

import numpy as np

try:
    import cupy as cp
    _GPU = cp.is_available()
except ImportError:
    cp = None
    _GPU = False


def _to_gpu(arr: np.ndarray):
    return cp.asarray(arr) if _GPU else arr


def _to_cpu(arr) -> np.ndarray:
    return cp.asnumpy(arr) if (_GPU and isinstance(arr, cp.ndarray)) else arr


def _xp(arr):
    if _GPU:
        return cp.get_array_module(arr)
    return np


def _safe_ratio(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a - b) / (a + b), NaN-safe. Runs on GPU when available."""
    a, b = _to_gpu(a), _to_gpu(b)
    xp = _xp(a)
    num = a - b
    den = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        out = xp.where(den != 0, num / den, xp.nan)
    return _to_cpu(out.astype("float32"))


def ndvi(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    return _safe_ratio(nir, red)


def ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """McFeeters NDWI — water positive."""
    return _safe_ratio(green, nir)


def nbr(nir: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """Normalized Burn Ratio — requires SWIR."""
    return _safe_ratio(nir, swir1)


def mndwi(green: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """Modified NDWI (Xu) — requires SWIR."""
    return _safe_ratio(green, swir1)


def cloud_mask(green: np.ndarray, nir: np.ndarray,
               brightness_thresh: float = 0.35, nir_thresh: float = 0.30) -> np.ndarray:
    """
    Simple brightness + NIR threshold cloud mask (plan Phase 2, step 5).

    Clouds are bright across visible and NIR. Returns uint8: 1=cloud, 0=clear.
    NaN (nodata) pixels are treated as clear (0) so they don't inflate cloud %.
    """
    g = _to_gpu(np.nan_to_num(green, nan=0.0))
    n = _to_gpu(np.nan_to_num(nir, nan=0.0))
    mask = (g > brightness_thresh) & (n > nir_thresh)
    return _to_cpu(mask.astype("uint8"))


def cloud_fraction(mask: np.ndarray, valid: np.ndarray) -> float:
    """Cloud pixels as a fraction (0-100) of valid (non-nodata) pixels."""
    valid_count = int(valid.sum())
    if valid_count == 0:
        return 0.0
    return round(100.0 * float(mask[valid].sum()) / valid_count, 2)


def index_metrics(name: str, arr: np.ndarray) -> dict[str, float]:
    """
    Summary statistics for one index array, NaN-safe. Returns a dict of
    {metric_name: value} ready to cache, e.g. {'ndwi_mean': .., 'ndwi_std': ..}.
    """
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {}
    metrics = {
        f"{name}_mean": float(np.mean(finite)),
        f"{name}_std": float(np.std(finite)),
        f"{name}_min": float(np.min(finite)),
        f"{name}_max": float(np.max(finite)),
    }
    if name == "ndwi":
        water = float((finite > 0.3).sum()) / finite.size * 100.0
        metrics["water_area_pct"] = round(water, 3)
    return metrics
