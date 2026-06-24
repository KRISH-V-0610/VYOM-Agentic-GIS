"""
Radiometric calibration: DN -> TOA radiance -> TOA reflectance -> DOS surface reflectance.

Pipeline per band (plan Phase 2, steps 3-4), corrected for real data:

  1. DN -> Radiance:   L = Lmin + (Lmax - Lmin) * DN / DN_max
     NOTE: DN_max = 2^BitsPerPixel - 1 = 1023 for this 10-bit data.
     (The plan text said /255 — that is wrong for 10-bit IRS L2 products.)

  2. Radiance -> TOA reflectance:  rho = pi * L * d^2 / (ESUN * cos(theta_s))
     theta_s = 90 - sun_elevation ; d = Earth-Sun distance (AU) for the day.

  3. DOS1 (Dark Object Subtraction): estimate per-band haze from the scene's dark
     object and subtract its path radiance, assuming a 1% dark-object reflectance.

ESUN values are standard published mean exoatmospheric solar irradiances for the
ResourceSat LISS/AWiFS VNIR+SWIR bands, in mW/(cm^2 * um) — consistent units with
the BAND_META Lmax values in mW/(cm^2 * sr * um). DOS introduces ~+/-5-10%
reflectance uncertainty (documented per scene).

GPU: when CuPy is available arrays are processed on the GPU; the public API still
accepts and returns numpy ndarrays — callers do not need to know about GPU.
"""

import math

import numpy as np

try:
    import cupy as cp
    _GPU = cp.is_available()
except ImportError:
    cp = None
    _GPU = False

# ESUN per sensor family, keyed by CANONICAL band name (never physical band number).
# Units: mW / (cm^2 * um). Source: IRS ResourceSat-2/2A calibration literature.
ESUN: dict[str, dict[str, float]] = {
    "LISS3": {"green": 181.0, "red": 156.0, "nir": 104.0, "swir1": 21.6},
    "LISS4": {"green": 185.0, "red": 158.0, "nir": 107.0},
    "AWIFS": {"green": 183.0, "red": 157.0, "nir": 106.0, "swir1": 21.7},
}

DOS_NODATA = 0  # DN=0 is border fill in these products.


def earth_sun_distance(doy: int) -> float:
    """Earth-Sun distance in AU for day-of-year (standard cosine approximation)."""
    return 1.0 - 0.01672 * math.cos(math.radians(0.9856 * (doy - 4)))


def _xp(arr):
    """Return the array namespace — cupy if on GPU, else numpy."""
    if _GPU:
        return cp.get_array_module(arr)
    return np


def _to_gpu(arr: np.ndarray):
    return cp.asarray(arr) if _GPU else arr


def _to_cpu(arr) -> np.ndarray:
    return cp.asnumpy(arr) if (_GPU and isinstance(arr, cp.ndarray)) else arr


def dn_to_radiance(dn: np.ndarray, lmin: float, lmax: float, dn_max: int) -> np.ndarray:
    """DN -> at-sensor spectral radiance (mW/cm^2/sr/um)."""
    xp = _xp(dn)
    return lmin + (lmax - lmin) * (dn.astype("float32") / dn_max)


def radiance_to_toa_reflectance(
    radiance: np.ndarray, esun: float, sun_elevation: float, doy: int
) -> np.ndarray:
    """At-sensor radiance -> TOA reflectance (dimensionless 0..~1)."""
    theta_s = math.radians(90.0 - sun_elevation)
    d = earth_sun_distance(doy)
    return (math.pi * radiance * d * d) / (esun * math.cos(theta_s))


def _dark_object_dn(dn: np.ndarray, percentile: float = 0.1) -> float:
    """Lowest meaningful DN (haze) — low percentile of valid (non-zero) pixels."""
    xp = _xp(dn)
    valid = dn[dn > DOS_NODATA]
    if valid.size == 0:
        return 0.0
    return float(xp.percentile(valid, percentile))


def calibrate_band(
    dn: np.ndarray,
    *,
    canonical_band: str,
    sensor_family: str,
    lmin: float,
    lmax: float,
    dn_max: int,
    sun_elevation: float,
    doy: int,
    apply_dos: bool = True,
) -> np.ndarray:
    """
    Full chain DN -> DOS surface reflectance for one band.

    Accepts numpy input; returns numpy float32 reflectance with nodata pixels
    (DN==0) set to NaN, clipped to [0, 1.5]. Computation runs on GPU when CuPy
    is available.
    """
    esun = ESUN[sensor_family][canonical_band]
    arr = _to_gpu(dn)
    nodata_mask = arr <= DOS_NODATA

    radiance = dn_to_radiance(arr, lmin, lmax, dn_max)

    if apply_dos:
        dark_dn = _dark_object_dn(arr)
        l_haze = dn_to_radiance(
            (_to_gpu(np.array([dark_dn], dtype="float32"))
             if not _GPU else cp.array([dark_dn], dtype="float32")),
            lmin, lmax, dn_max
        )[0]
        d = earth_sun_distance(doy)
        theta_s = math.radians(90.0 - sun_elevation)
        l_one_pct = (0.01 * esun * math.cos(theta_s)) / (math.pi * d * d)
        l_path = max(0.0, float(l_haze) - l_one_pct)
        radiance = radiance - l_path

    refl = radiance_to_toa_reflectance(radiance, esun, sun_elevation, doy)
    xp = _xp(refl)
    refl = xp.clip(refl, 0.0, 1.5).astype("float32")
    refl[nodata_mask] = xp.nan
    return _to_cpu(refl)
