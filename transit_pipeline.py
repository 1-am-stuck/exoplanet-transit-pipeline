#!/usr/bin/env python3
"""Tabletop transit photometry in one file.

Three stages, one CSV contract (timestamp_ms, raw_adc):

  1. simulate  — virtual ESP32 / ADS1115 rig (or paste a Tinkercad serial log)
  2. extract   — remove the slow baseline, fold on the known period, measure Rp/R*
  3. mast      — optional Kepler/TESS download, exported into that same CSV

    python3 transit_pipeline.py
    python3 transit_pipeline.py demo kepler
    python3 transit_pipeline.py extract kepler
    python3 transit_pipeline.py extract tinkercad_telemetry.csv 10 4
    python3 transit_pipeline.py tinkercad

The orbital period is an input (stepper or servo setting on the rig, catalog
period for a real planet). This file does not try to discover it.
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Any, Iterable

import numpy as np


# ==========================================
# STAGE 1 — VIRTUAL RIG
# ==========================================
RIG_CONFIG = {
    # Timing & Sampling
    "SAMPLING_RATE_HZ": 50.0,       # ESP32 loop speed (samples per second)
    "RUN_DURATION_SEC": 180.0,      # Total recording time (3 minutes = 6 full orbits)
    "ORBITAL_PERIOD_SEC": 30.0,     # Stepper motor revolution period
    "TRANSIT_DURATION_SEC": 3.2,    # Time the bead takes to cross the LED diffuser

    # Orbit & Physical Geometry
    "RADIUS_RATIO": 0.15,           # Rp / R* (0.15 gives a 2.25% transit depth)
    "IMPACT_PARAMETER": 0.0,        # 0.0 = central U-shape; > (1-p) = grazing V-shape

    # Detector & Light Source Telemetry
    "ADC_BASELINE": 62000.0,        # Unobstructed light level in 16-bit counts
    "NOISE_SIGMA": 75.0,            # Standard deviation of Gaussian sensor noise
    "THERMAL_DRIFT_MAG": 1500.0,    # Total ADC count loss from LED heating over run
    "THERMAL_TAU_SEC": 90.0,        # Thermal time constant for exponential decay

    # Output File
    "OUTPUT_FILENAME": "tabletop_telemetry.csv",
    "RANDOM_SEED": 42,
}


# ==========================================
# STAGE 2 — EXTRACTION
# ==========================================
# One line picks the demonstration. "tabletop" is the simulated rig.
# "kepler" is HAT-P-7b, already saved in kepler_hat_p_7.csv.
# The period for each file lives next to its name in SAMPLES.
SAMPLE = "kepler"

SAMPLES = {
    "tabletop": {
        "filename": "tabletop_telemetry.csv",
        "period_sec": 30.0,
        "window_sec": 12.0,
        "description": "Simulated rig, six 30 s orbits, Rp/R* = 0.15",
    },
    "kepler": {
        "filename": "kepler_hat_p_7.csv",
        "period_sec": 190489.14,  # HAT-P-7b, 2.204735 days
        "window_sec": 12.0,
        "description": "Kepler quarters 0–3, HAT-P-7b",
    },
}

EXTRACT_CONFIG = {
    "INPUT_FILENAME": SAMPLES[SAMPLE]["filename"],

    # Known orbital period. Must match the rig, the Tinkercad sketch, or the
    # catalog period once real photometry is dropped in.
    "ORBITAL_PERIOD_SEC": SAMPLES[SAMPLE]["period_sec"],

    # Moving-median window. Longer than a transit so the dip is ignored,
    # shorter than the LED thermal drift so the baseline still follows it.
    "DETREND_WINDOW_SEC": 12.0,

    # How many robust-sigma below the continuum a sample must sit
    # before the second median pass treats it as part of a transit.
    "CLIP_SIGMA": 5.0,

    # Bins across one orbit after folding. 180 bins at 30 s is 0.17 s each.
    "PHASE_BINS": 180,

    "DETRENDED_FILENAME": "detrended_telemetry.csv",
    "FOLDED_FILENAME": "folded_lightcurve.csv",
    "PLOT_FILENAME": "folded_lightcurve.png",
}


def compute_overlap_area(z: np.ndarray, _r_star: float, r_planet: float) -> np.ndarray:
    """Exact circle-circle intersection area. z and r_planet are in units of the star radius."""
    p = r_planet
    area = np.zeros_like(z)

    no_overlap = z >= (1.0 + p)
    full_overlap = z <= (1.0 - p)
    area[full_overlap] = np.pi * (p ** 2)

    partial = (~no_overlap) & (~full_overlap)
    zp = z[partial]

    arg_star = np.clip((zp**2 + 1.0 - p**2) / (2.0 * zp), -1.0, 1.0)
    arg_planet = np.clip((zp**2 + p**2 - 1.0) / (2.0 * zp * p), -1.0, 1.0)

    alpha = 2.0 * np.arccos(arg_star)
    beta = 2.0 * np.arccos(arg_planet)

    area_star_seg = 0.5 * (alpha - np.sin(alpha))
    area_planet_seg = 0.5 * (p ** 2) * (beta - np.sin(beta))

    area[partial] = area_star_seg + area_planet_seg
    return area


def generate_telemetry(config: dict) -> str:
    """Write a synthetic SD-card log: timestamp_ms, raw_adc."""
    np.random.seed(config["RANDOM_SEED"])

    fs = config["SAMPLING_RATE_HZ"]
    duration = config["RUN_DURATION_SEC"]
    total_samples = int(duration * fs)

    t_sec = np.linspace(0.0, duration, total_samples, endpoint=False)
    timestamp_ms = (t_sec * 1000.0).astype(np.int64)

    f_baseline = config["ADC_BASELINE"] - config["THERMAL_DRIFT_MAG"] * (
        1.0 - np.exp(-t_sec / config["THERMAL_TAU_SEC"])
    )

    period = config["ORBITAL_PERIOD_SEC"]
    t_dur = config["TRANSIT_DURATION_SEC"]
    p = config["RADIUS_RATIO"]
    b = config["IMPACT_PARAMETER"]

    # Center transits at T = 0.5*P, 1.5*P, 2.5*P...
    phase_normalized = (t_sec % period) / period
    delta_phase = np.abs(phase_normalized - 0.5)
    t_from_mid = delta_phase * period

    chord_half_width = np.sqrt(np.maximum((1.0 + p)**2 - b**2, 1e-9))
    velocity = (2.0 * chord_half_width) / t_dur
    z = np.sqrt((velocity * t_from_mid)**2 + b**2)

    overlap_area = compute_overlap_area(z, 1.0, p)
    blocked_fraction = overlap_area / np.pi

    clean_flux = f_baseline * (1.0 - blocked_fraction)
    noise = np.random.normal(0.0, config["NOISE_SIGMA"], size=total_samples)
    raw_adc = np.clip(np.round(clean_flux + noise), 0, 65535).astype(np.int32)

    filename = output_path(config["OUTPUT_FILENAME"])
    with open(filename, mode="w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ms", "raw_adc"])
        for ts, adc in zip(timestamp_ms, raw_adc):
            writer.writerow([ts, adc])

    theoretical_depth = (p ** 2) * 100.0
    measured_drop = config["ADC_BASELINE"] * (p ** 2)
    snr_single_sample = measured_drop / config["NOISE_SIGMA"]

    print(f"Generated {total_samples} samples -> {filename}")
    print(f"Total orbits captured: {duration / period:.1f}")
    print(f"Theoretical transit depth: {theoretical_depth:.3f}% ({measured_drop:.1f} ADC counts)")
    print(f"Single-sample SNR: {snr_single_sample:.2f}")
    return filename


def load_telemetry(filename: str):
    """Read timestamp_ms, raw_adc. Sort by time so a shuffled log still folds."""
    timestamps = []
    flux = []
    with open(filename, newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        missing = [c for c in ("timestamp_ms", "raw_adc") if c not in fields]
        if missing:
            found = ", ".join(fields) if fields else "(none)"
            raise SystemExit(
                f"{filename} is missing {missing}. Expected columns "
                f"timestamp_ms, raw_adc (found: {found})."
            )
        for line_no, row in enumerate(reader, start=2):
            try:
                timestamps.append(float(row["timestamp_ms"]))
                flux.append(float(row["raw_adc"]))
            except (TypeError, ValueError) as exc:
                raise SystemExit(
                    f"{filename}:{line_no} has a non-numeric timestamp or flux"
                ) from exc
    if len(timestamps) < 10:
        raise SystemExit(f"{filename} needs at least 10 samples to fold")
    timestamp_ms = np.asarray(timestamps, dtype=float)
    raw_adc = np.asarray(flux, dtype=float)
    order = np.argsort(timestamp_ms, kind="mergesort")
    return timestamp_ms[order], raw_adc[order]


def _odd_sample_window(dt_sec: float, window_sec: float) -> int:
    samples = int(round(window_sec / dt_sec))
    samples = max(samples, 3)
    if samples % 2 == 0:
        samples += 1
    return samples


def _fill_gaps(trend: np.ndarray, fallback: float) -> np.ndarray:
    """Interpolate across windows that had no usable samples."""
    good = np.isfinite(trend)
    if not np.any(good):
        return np.full_like(trend, fallback)
    if np.all(good):
        return trend
    idx = np.arange(trend.size)
    trend = trend.copy()
    trend[~good] = np.interp(idx[~good], idx[good], trend[good])
    return trend


def _uniform_moving_median(flux: np.ndarray, window: int, exclude: np.ndarray | None) -> np.ndarray:
    y = flux.astype(float, copy=True)
    if exclude is not None:
        y[exclude] = np.nan
    pad = window // 2
    padded = np.pad(y, pad, mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(padded, window)
    with np.errstate(all="ignore"):
        trend = np.nanmedian(view, axis=1)
    fallback = np.nanmedian(flux) if np.any(np.isfinite(flux)) else 0.0
    return _fill_gaps(trend, float(fallback))


def _gapped_moving_median(time_sec: np.ndarray, flux: np.ndarray,
                          window_sec: float, exclude: np.ndarray | None) -> np.ndarray:
    """Time-based median for logs with dropouts (Kepler quarters, SD-card gaps)."""
    y = flux.astype(float, copy=True)
    if exclude is not None:
        y[exclude] = np.nan
    half = window_sec / 2.0
    n = y.size
    trend = np.empty(n, dtype=float)
    left = 0
    right = 0
    for i in range(n):
        t = time_sec[i]
        while left < n and time_sec[left] < t - half:
            left += 1
        while right < n and time_sec[right] <= t + half:
            right += 1
        chunk = y[left:right]
        finite = chunk[np.isfinite(chunk)]
        trend[i] = np.median(finite) if finite.size else np.nan
    fallback = np.nanmedian(flux) if np.any(np.isfinite(flux)) else 0.0
    return _fill_gaps(trend, float(fallback))


def moving_median(time_sec: np.ndarray, flux: np.ndarray, window_sec: float,
                  exclude: np.ndarray | None = None) -> np.ndarray:
    """Median filter. `exclude` marks samples that must not pull the baseline down."""
    dt = np.diff(time_sec)
    uniform = dt.size > 0 and np.allclose(dt, np.median(dt), rtol=1e-3, atol=1e-6)
    if uniform:
        window = _odd_sample_window(float(np.median(dt)), window_sec)
        window = min(window, flux.size if flux.size % 2 else flux.size - 1)
        window = max(window, 3)
        return _uniform_moving_median(flux, window, exclude)
    return _gapped_moving_median(time_sec, flux, window_sec, exclude)


def detrend_flux(time_sec: np.ndarray, flux: np.ndarray, window_sec: float,
                 clip_sigma: float):
    """
    Two-pass moving median.

    Pass 1 follows the slow drift and is only used to find the dips.
    Pass 2 recomputes the baseline with those dips masked, so the filter
    cannot fill the transit back in. Division removes a multiplicative
    baseline: flux = baseline(t) * (1 - blocked fraction).
    """
    rough = np.maximum(moving_median(time_sec, flux, window_sec), 1e-6)
    norm = flux / rough
    continuum = np.median(norm)
    sigma = 1.4826 * np.median(np.abs(norm - continuum))
    sigma = max(float(sigma), 1e-8)
    exclude = norm < (continuum - clip_sigma * sigma)
    if np.mean(exclude) > 0.45:
        exclude = np.zeros(flux.shape, dtype=bool)
    trend = np.maximum(moving_median(time_sec, flux, window_sec, exclude), 1e-6)
    return trend, flux / trend, exclude, sigma


def fold_light_curve(time_sec: np.ndarray, flux: np.ndarray, period_sec: float,
                     n_bins: int):
    """Stack every orbit on phase in [0, 1) by modulo of the known period."""
    phase = np.mod(time_sec, period_sec) / period_sec
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(phase, edges) - 1, 0, n_bins - 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    means = np.full(n_bins, np.nan)
    stds = np.full(n_bins, np.nan)
    counts = np.zeros(n_bins, dtype=int)
    for i in range(n_bins):
        sel = idx == i
        counts[i] = int(np.count_nonzero(sel))
        if counts[i] == 0:
            continue
        sample = flux[sel]
        means[i] = np.mean(sample)
        stds[i] = np.std(sample)
    return phase, centers, means, stds, counts


def _longest_circular_run(mask: np.ndarray):
    """Longest contiguous True stretch on a ring. Returns (start, length)."""
    n = mask.size
    if n == 0 or not np.any(mask):
        return 0, 0
    doubled = np.concatenate([mask, mask])
    best_len = 0
    best_start = 0
    run = 0
    run_start = 0
    for i, flag in enumerate(doubled):
        if flag:
            if run == 0:
                run_start = i
            run += 1
            if run > best_len and run <= n:
                best_len = run
                best_start = run_start
        else:
            run = 0
    return best_start % n, best_len


def measure_transit(centers: np.ndarray, means: np.ndarray, counts: np.ndarray,
                    period_sec: float):
    """
    Depth is the blocked fraction of the stellar disk. For a central transit
    of a uniform star that fraction equals (Rp / R*)^2. A V-shaped (grazing)
    dip never reaches that full area, so the radius from its depth is a lower limit.
    """
    valid = counts > 0
    if np.count_nonzero(valid) < 8:
        raise SystemExit("Not enough filled phase bins to measure a transit")

    y = means.copy()
    quiet = valid & np.isfinite(y)
    continuum = float(np.median(y[quiet]))
    sigma = 1.4826 * float(np.median(np.abs(y[quiet] - continuum)))
    sigma = max(sigma, 1e-8)

    tip = float(np.nanmin(y[quiet]))
    depth_guess = continuum - tip
    detected = depth_guess > 5.0 * sigma
    result = {
        "detected": detected,
        "continuum": continuum,
        "sigma": sigma,
        "depth": 0.0,
        "radius_ratio": float("nan"),
        "radius_sigma": float("nan"),
        "duration_sec": float("nan"),
        "shape": "none",
        "lower_limit": False,
    }
    if not detected:
        return result

    def _width(frac: float) -> int:
        mask = quiet & (y < continuum - frac * depth_guess)
        _, length = _longest_circular_run(mask)
        return length

    w50 = _width(0.50)
    w90 = _width(0.90)
    grazing = w50 > 0 and (w90 / w50) < 0.70

    bottom = quiet & (y < continuum - 0.85 * depth_guess)
    if not np.any(bottom):
        result["detected"] = False
        return result
    floor = float(np.median(y[bottom]))
    depth_counts = max(continuum - floor, 0.0)
    depth = depth_counts / continuum if continuum != 0 else 0.0
    radius_ratio = float(np.sqrt(max(depth, 0.0)))

    floor_scatter = 1.4826 * float(np.median(np.abs(y[bottom] - floor)))
    n_bottom = max(int(np.count_nonzero(bottom)), 1)
    depth_sigma = (floor_scatter / np.sqrt(n_bottom)) / abs(continuum)
    radius_sigma = (0.5 * depth_sigma / radius_ratio) if radius_ratio > 0 else float("nan")

    in_transit = quiet & (y < continuum - 0.5 * depth_guess)
    outside = quiet & ~in_transit
    outside_level = float(np.median(y[outside])) if np.any(outside) else continuum
    outside_sigma = 1.4826 * float(np.median(np.abs(y[outside] - outside_level))) if np.any(outside) else sigma
    contact_level = continuum - max(3.0 * outside_sigma, 0.08 * depth_guess)
    in_contact = quiet & (y < contact_level)
    _, run_len = _longest_circular_run(in_contact)
    duration_sec = run_len * (period_sec / centers.size)

    result.update({
        "continuum": continuum,
        "depth": float(depth),
        "radius_ratio": radius_ratio,
        "radius_sigma": float(radius_sigma),
        "duration_sec": float(duration_sec),
        "shape": "grazing V-shape" if grazing else "central U-shape",
        "lower_limit": grazing,
        "flatness": float(w90 / w50) if w50 else float("nan"),
    })
    return result


def write_detrended(filename: str, timestamp_ms: np.ndarray, raw_adc: np.ndarray,
                    trend: np.ndarray, detrended: np.ndarray) -> None:
    with open(filename, mode="w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ms", "raw_adc", "trend_adc", "detrended_flux"])
        for ts, adc, tr, df in zip(timestamp_ms, raw_adc, trend, detrended):
            writer.writerow([f"{ts:.3f}", f"{adc:.3f}", f"{tr:.3f}", f"{df:.6f}"])


def write_folded(filename: str, centers: np.ndarray, means: np.ndarray,
                 stds: np.ndarray, counts: np.ndarray) -> None:
    with open(filename, mode="w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["phase", "flux", "flux_std", "n_samples"])
        for phase, flux, std, n in zip(centers, means, stds, counts):
            if n == 0 or not np.isfinite(flux):
                continue
            writer.writerow([f"{phase:.6f}", f"{flux:.6f}", f"{std:.6f}", int(n)])


def save_plot(filename: str, time_sec: np.ndarray, raw_adc: np.ndarray,
              trend: np.ndarray, phase: np.ndarray, detrended: np.ndarray,
              centers: np.ndarray, means: np.ndarray, result: dict) -> bool:
    """Save the raw drift and the stacked dip. Returns False if matplotlib is missing."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed; skipped the folded-curve plot")
        return False

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), constrained_layout=True)

    axes[0].plot(time_sec, raw_adc, color="0.65", lw=0.6, label="raw ADC")
    axes[0].plot(time_sec, trend, color="#c45c26", lw=1.4, label="moving-median baseline")
    axes[0].set_xlabel("Time (s)")
    axes[0].set_ylabel("Flux (ADC counts)")
    axes[0].set_title("Detrend: thermal drift removed, transits left in place")
    axes[0].legend(frameon=False, loc="lower left")

    axes[1].scatter(phase, detrended, s=4, c="0.75", linewidths=0, label="folded samples")
    ok = np.isfinite(means)
    axes[1].plot(centers[ok], means[ok], color="#1f4e79", lw=1.8, label="binned mean")
    if result["detected"]:
        depth = result["depth"]
        axes[1].axhline(result["continuum"], color="0.4", ls="--", lw=0.8)
        axes[1].axhline(result["continuum"] * (1.0 - depth), color="#1f4e79",
                        ls="--", lw=0.8)
        label = "lower limit" if result["lower_limit"] else "Rp/R*"
        title = (
            f"Phase-folded light curve — {result['shape']}, "
            f"{label} = {result['radius_ratio']:.3f}"
        )
    else:
        title = "Phase-folded light curve — no transit detected"
    axes[1].set_xlim(0.0, 1.0)
    axes[1].set_xlabel("Orbital phase")
    axes[1].set_ylabel("Detrended flux")
    axes[1].set_title(title)
    axes[1].legend(frameon=False, loc="lower left")

    fig.savefig(filename, dpi=140)
    plt.close(fig)
    return True


def output_path(filename: str) -> str:
    if os.path.isabs(filename):
        return filename
    return os.path.abspath(filename)


def input_path(filename: str) -> str:
    if os.path.isabs(filename):
        return filename
    from_cwd = os.path.abspath(filename)
    if os.path.exists(from_cwd):
        return from_cwd
    beside_script = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    )
    if os.path.exists(beside_script):
        return beside_script
    return from_cwd


def run_extraction(config: dict) -> dict:
    config["INPUT_FILENAME"] = input_path(config["INPUT_FILENAME"])
    if not os.path.exists(config["INPUT_FILENAME"]):
        raise SystemExit(
            f"No telemetry file at {config['INPUT_FILENAME']}. "
            "Run `python3 transit_pipeline.py` first, or pass a Tinkercad CSV."
        )
    for key in ("DETRENDED_FILENAME", "FOLDED_FILENAME", "PLOT_FILENAME"):
        config[key] = output_path(config[key])

    timestamp_ms, raw_adc = load_telemetry(config["INPUT_FILENAME"])
    time_sec = timestamp_ms / 1000.0
    period = float(config["ORBITAL_PERIOD_SEC"])
    if period <= 0:
        raise SystemExit("ORBITAL_PERIOD_SEC must be positive")

    window_sec = float(config["DETREND_WINDOW_SEC"])
    if time_sec.size > 2:
        cadence = float(np.median(np.diff(time_sec)))
        # A 12 s window is right for the 50 Hz rig. Kepler/TESS cadences are
        # minutes, so fall back to a fraction of the orbit instead of a window
        # that is shorter than one sample and would eat the dip.
        if cadence > 0 and window_sec < 5.0 * cadence:
            window_sec = 0.40 * period
            print(
                f"Sampling cadence is {cadence:.1f} s, so the {config['DETREND_WINDOW_SEC']:.1f} s "
                f"rig window is too short. Using {window_sec:.1f} s (0.4 periods) instead."
            )

    trend, detrended, exclude, _ = detrend_flux(
        time_sec, raw_adc, window_sec, float(config["CLIP_SIGMA"])
    )
    # 180 bins is right for the 50 Hz rig (~8 samples per bin per orbit).
    # A short Tinkercad capture leaves many of those bins empty, which splits
    # the dip and makes a U look like a one-bin spike. Shrink until a typical
    # bin still has a handful of samples.
    n_bins = int(config["PHASE_BINS"])
    if timestamp_ms.size / n_bins < 4:
        n_bins = max(24, int(timestamp_ms.size / 4))
        print(
            f"Using {n_bins} phase bins instead of {int(config['PHASE_BINS'])} "
            "so the short log still fills each bin."
        )
    phase, centers, means, stds, counts = fold_light_curve(
        time_sec, detrended, period, n_bins
    )
    result = measure_transit(centers, means, counts, period)

    write_detrended(config["DETRENDED_FILENAME"], timestamp_ms, raw_adc, trend, detrended)
    write_folded(config["FOLDED_FILENAME"], centers, means, stds, counts)
    plotted = save_plot(
        config["PLOT_FILENAME"], time_sec, raw_adc, trend, phase, detrended,
        centers, means, result,
    )

    span = float(time_sec[-1] - time_sec[0]) if time_sec.size > 1 else 0.0
    orbits = span / period if period else 0.0
    print(f"Loaded {timestamp_ms.size} samples <- {config['INPUT_FILENAME']}")
    print(f"Span: {span:.1f} s | orbits stacked: {orbits:.2f} | period: {period:.3f} s")
    print(
        f"Detrend window: {window_sec:.1f} s "
        f"| samples masked as in-transit: {int(np.count_nonzero(exclude))}"
    )
    if not result["detected"]:
        print("No transit rose above the folded noise floor")
    else:
        depth_pct = 100.0 * result["depth"]
        radius = result["radius_ratio"]
        radius_err = result["radius_sigma"]
        limit = " (lower limit, planet is not fully silhouetted)" if result["lower_limit"] else ""
        print(f"Transit shape: {result['shape']}")
        print(f"Recovered transit depth: {depth_pct:.3f}%")
        print(f"Recovered radius ratio Rp/R*: {radius:.4f} ± {radius_err:.4f}{limit}")
        print(f"Transit duration: {result['duration_sec']:.2f} s")
    print(f"Wrote {config['DETRENDED_FILENAME']}")
    print(f"Wrote {config['FOLDED_FILENAME']}")
    if plotted:
        print(f"Wrote {config['PLOT_FILENAME']}")
    return result


# ==========================================
# STAGE 3 — MAST / KEPLER / TESS
# ==========================================
def _require_lightkurve():
    try:
        import lightkurve as lk
    except ImportError as exc:
        raise SystemExit(
            "lightkurve is required for MAST downloads. Install it with: "
            "python3 -m pip install lightkurve"
        ) from exc
    return lk


def _as_arrays(lightcurves: Iterable[Any], flux_column: str) -> tuple[np.ndarray, np.ndarray]:
    times: list[np.ndarray] = []
    fluxes: list[np.ndarray] = []

    for lightcurve in lightcurves:
        time = np.asarray(lightcurve.time.value, dtype=float)
        if flux_column == "sap_flux":
            flux = np.asarray(lightcurve.sap_flux.value, dtype=float)
        elif flux_column == "flux":
            flux = np.asarray(lightcurve.flux.value, dtype=float)
        else:
            raise ValueError("flux_column must be 'sap_flux' or 'flux'")

        if time.shape != flux.shape:
            raise ValueError("MAST product has mismatched time and flux arrays")
        finite = np.isfinite(time) & np.isfinite(flux)
        if np.any(finite):
            times.append(time[finite])
            fluxes.append(flux[finite])

    if not times:
        raise ValueError("MAST products contained no finite time/flux samples")

    time_days = np.concatenate(times)
    flux = np.concatenate(fluxes)
    order = np.argsort(time_days, kind="mergesort")
    time_days = time_days[order]
    flux = flux[order]
    # First finite cadence is t = 0. Gaps stay gaps; nothing is interpolated.
    return (time_days - time_days[0]) * 86400000.0, flux


def write_part_schema(filename: str | Path, timestamp_ms: np.ndarray,
                      raw_adc: np.ndarray) -> None:
    """Write exactly timestamp_ms, raw_adc."""
    if timestamp_ms.size != raw_adc.size or timestamp_ms.size == 0:
        raise ValueError("timestamp and flux arrays must be non-empty and equal length")
    with Path(filename).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_ms", "raw_adc"])
        for timestamp, flux in zip(timestamp_ms, raw_adc):
            writer.writerow([f"{timestamp:.3f}", f"{flux:.6f}"])


def download_and_export(target: str, mission: str, cadence: str,
                        output: str | Path, download_dir: str | Path | None = None,
                        flux_column: str = "sap_flux") -> int:
    """Search MAST, download FITS products, and export their raw flux arrays."""
    lk = _require_lightkurve()
    search = lk.search_lightcurve(target, mission=mission, cadence=cadence)
    if len(search) == 0:
        raise SystemExit(f"No {mission} light-curve products found for {target!r}")

    kwargs = {"quality_bitmask": "default"}
    if download_dir is not None:
        kwargs["download_dir"] = str(download_dir)
    products = search.download_all(**kwargs)
    # LightCurveCollection implements __getitem__ but not __iter__, so it is
    # not a typing.Iterable. The light curves live in the list `.data`.
    lightcurves = getattr(products, "data", None)
    if not isinstance(lightcurves, list) or len(lightcurves) == 0:
        raise SystemExit(f"MAST returned no downloadable products for {target!r}")

    timestamp_ms, raw_adc = _as_arrays(lightcurves, flux_column)
    write_part_schema(output, timestamp_ms, raw_adc)
    print(f"Downloaded {len(lightcurves)} FITS light curves from MAST")
    print(f"Exported {raw_adc.size} finite samples to {Path(output)}")
    print("Schema: timestamp_ms,raw_adc")
    return raw_adc.size


# ==========================================
# TINKERCAD SAMPLE
# Period, depth, and CSV columns match EXTRACT_CONFIG when you pass period=10.
# ==========================================
TINKERCAD_SKETCH = r"""#include <Servo.h>

// Tinkercad tabletop transit. Serial lines are the Python CSV contract:
//   timestamp_ms,raw_adc
//
// Tinkercad does not shine the LED onto the sensor, so this sketch applies
// the dip itself when the servo (the planet) crosses mid-orbit. The
// potentiometer on A0 is the star brightness. Leave it still during the run.
//
// Orbit = 10 s, sample rate = 10 Hz, transit = 1.2 s, Rp/R* = 0.15 (2.25% deep).
// After you save the serial log:
//   python3 transit_pipeline.py extract tinkercad_telemetry.csv 10 4

const unsigned long SAMPLE_INTERVAL_MS = 100;
const unsigned long ORBITAL_PERIOD_MS = 10000;
const unsigned long TRANSIT_DURATION_MS = 1200;
const float RADIUS_RATIO = 0.15;

const int LED_PIN = 6;
const int SERVO_PIN = 9;
const int DETECTOR_PIN = A0;

Servo planet;
unsigned long lastSample = 0;

float transitFactor(unsigned long tMs) {
  unsigned long phaseMs = tMs % ORBITAL_PERIOD_MS;
  long fromMid = (long)phaseMs - (long)(ORBITAL_PERIOD_MS / 2);
  if (fromMid < 0) fromMid = -fromMid;

  float half = TRANSIT_DURATION_MS / 2.0;
  if (fromMid >= half) return 1.0;

  // Flat bottom, short ramps: a central U, not a V.
  float depth = RADIUS_RATIO * RADIUS_RATIO;
  float edge = 0.20 * half;
  float blocked = depth;
  if (fromMid > half - edge) {
    blocked = depth * (half - fromMid) / edge;
  }
  if (blocked < 0) blocked = 0;
  return 1.0 - blocked;
}

void setup() {
  Serial.begin(9600);
  pinMode(LED_PIN, OUTPUT);
  analogWrite(LED_PIN, 220);
  planet.attach(SERVO_PIN);
  Serial.println("timestamp_ms,raw_adc");
}

void loop() {
  unsigned long now = millis();
  float phase = (now % ORBITAL_PERIOD_MS) / (float)ORBITAL_PERIOD_MS;
  planet.write((int)(phase * 180.0));

  if (now - lastSample < SAMPLE_INTERVAL_MS) return;
  lastSample = now;

  int baseline = analogRead(DETECTOR_PIN);
  if (baseline < 50) baseline = 800;
  // Keep this small. A few counts of noise is enough to look like a sensor,
  // and still leaves a flat-bottomed U after folding four orbits.
  int noise = random(-2, 3);
  int adc = (int)(baseline * transitFactor(now) + 0.5) + noise;
  if (adc < 0) adc = 0;
  if (adc > 1023) adc = 1023;

  Serial.print(now);
  Serial.print(",");
  Serial.println(adc);
}
"""


TINKERCAD_GUIDE = """
Tinkercad Circuits sample
-------------------------
Tinkercad runs Arduino C++, not this Python file. Build the circuit, paste the
sketch, copy the Serial Monitor into tinkercad_telemetry.csv, then run:

    python3 transit_pipeline.py extract tinkercad_telemetry.csv 10 4

The 10 is the orbit period in seconds. The 4 is the detrend window in seconds.
Both must match the sketch (ORBITAL_PERIOD_MS = 10000).

Parts (Tinkercad default drawer)
  1. Arduino Uno R3
  2. Servo (the planet; one sweep = one orbit)
  3. Red LED (the star; it stays on)
  4. 220 Ω resistor (click the resistor and type 220)
  5. Potentiometer (the detector / star brightness)
  6. A breadboard, if you want one. Direct wires also work.

Wiring
  Servo orange (signal)  -> Arduino D9
  Servo red              -> Arduino 5V
  Servo brown/black      -> Arduino GND

  Arduino D6 -> 220 Ω resistor -> LED anode (bent / longer lead)
  LED cathode (flat side) -> Arduino GND

  Potentiometer pin 1    -> Arduino 5V
  Potentiometer pin 2 (wiper, the middle pin) -> Arduino A0
  Potentiometer pin 3    -> Arduino GND

Why a potentiometer, not a photoresistor: Tinkercad does not cast LED light
onto a photoresistor, and a servo arm does not block that light. The sketch
reads A0 as the unobstructed star, then multiplies in a 2.25% dip while the
servo passes 90° (mid-orbit). Turn the knob until Serial numbers sit around
600–900, then do not touch it. Moving it mid-run looks like a fake drift.

Code
  Open the circuit's code panel. If it says Blocks, switch to Text.
  Delete the starter sketch and paste the sketch below.
  Simulation speed can stay on the normal setting. Baud rate is 9600.

Run
  1. Start Simulation.
  2. Open Serial Monitor before you expect the header. If you missed
     "timestamp_ms,raw_adc", type that line yourself at the top of the file.
  3. Wait until the timestamp column passes 40000 (four orbits of 10 s).
     Tinkercad millis() is simulated time, so watch the numbers, not the clock.
     Dips are centered at 5000, 15000, 25000, and 35000. Stop after the
     reading has climbed back to the bright level — around 42000 is a clean
     end. Stopping in the middle of a dip clips that orbit and shallows the fold.
  4. Stop Simulation. Copy the monitor text into tinkercad_telemetry.csv
     next to transit_pipeline.py. Keep only the header and the number lines.
  5. python3 transit_pipeline.py extract tinkercad_telemetry.csv 10 4

You should get a central U-shape, depth near 2.25%, Rp/R* near 0.15, and a
duration near 1.1 s (the sketch's chord is 1.2 s; binning shortens it slightly).
A shorter capture (under ~3 orbits, or timestamps that never reach 30000)
often prints "No transit rose above the folded noise floor".

The LED and the servo are the visible rig. The dip is applied in the sketch
because the simulator has no optical path. A real bench version replaces
transitFactor() with a phototransistor aimed at the LED and a bead on the servo.
"""


def print_tinkercad_guide() -> None:
    print(TINKERCAD_GUIDE)
    print(TINKERCAD_SKETCH)


def sample_config(name: str) -> dict:
    """Copy EXTRACT_CONFIG and point it at a named demonstration sample."""
    key = name.strip().lower()
    if key not in SAMPLES:
        known = ", ".join(SAMPLES)
        raise SystemExit(f"Unknown sample {name!r}. Choices: {known}")
    spec = SAMPLES[key]
    config = dict(EXTRACT_CONFIG)
    config["INPUT_FILENAME"] = spec["filename"]
    config["ORBITAL_PERIOD_SEC"] = spec["period_sec"]
    config["DETREND_WINDOW_SEC"] = spec["window_sec"]
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate, download, or measure a transit light curve (timestamp_ms, raw_adc)."
    )
    sub = parser.add_subparsers(dest="command")

    demo = sub.add_parser("demo", help="Measure a named sample (default: the SAMPLE line)")
    demo.add_argument("sample", nargs="?", choices=tuple(SAMPLES),
                      help="tabletop or kepler. Defaults to the SAMPLE line in this file.")
    sub.add_parser("simulate", help="Write tabletop_telemetry.csv only")
    sub.add_parser("tinkercad", help="Print the Tinkercad wiring guide and Arduino sketch")

    extract = sub.add_parser("extract", help="Detrend, fold, and measure a CSV log")
    extract.add_argument("csv", nargs="?",
                         help="A CSV path, or a sample name: tabletop, kepler")
    extract.add_argument("period", nargs="?", type=float, help="Orbital period in seconds")
    extract.add_argument("window", nargs="?", type=float, help="Detrend window in seconds")

    mast = sub.add_parser("mast", help="Download a Kepler/TESS light curve and optionally measure it")
    mast.add_argument("--target", default="HAT-P-7")
    mast.add_argument("--mission", choices=("Kepler", "TESS"), default="Kepler")
    mast.add_argument("--cadence", default="long")
    mast.add_argument("--output", default="mast_lightcurve.csv")
    mast.add_argument("--download-dir", default=None)
    mast.add_argument("--flux-column", choices=("sap_flux", "flux"), default="sap_flux")
    mast.add_argument("--period", type=float, default=None,
                      help="Catalog period in seconds. If set, extraction runs on the download.")
    mast.add_argument("--window", type=float, default=None,
                      help="Optional detrend window in seconds")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    command = args.command or "demo"

    if command == "tinkercad":
        print_tinkercad_guide()
        return

    if command == "simulate":
        generate_telemetry(dict(RIG_CONFIG))
        return

    if command == "extract":
        named = (args.csv or "").strip().lower()
        config = sample_config(named) if named in SAMPLES else dict(EXTRACT_CONFIG)
        if named and named not in SAMPLES:
            config["INPUT_FILENAME"] = args.csv
        if getattr(args, "period", None) is not None:
            config["ORBITAL_PERIOD_SEC"] = args.period
        if getattr(args, "window", None) is not None:
            config["DETREND_WINDOW_SEC"] = args.window
        run_extraction(config)
        return

    if command == "mast":
        download_and_export(
            args.target, args.mission, args.cadence, args.output,
            args.download_dir, args.flux_column,
        )
        if args.period is not None:
            config = dict(EXTRACT_CONFIG)
            config["INPUT_FILENAME"] = args.output
            config["ORBITAL_PERIOD_SEC"] = args.period
            if args.window is not None:
                config["DETREND_WINDOW_SEC"] = args.window
            print()
            run_extraction(config)
        else:
            print("Pass --period SECONDS to fold this file (HAT-P-7b is about 190486 s).")
        return

    chosen = getattr(args, "sample", None) or SAMPLE
    spec = SAMPLES[chosen]
    print(f"Sample: {chosen} — {spec['description']}")
    if chosen == "tabletop":
        generate_telemetry(dict(RIG_CONFIG))
        print()
    run_extraction(sample_config(chosen))


if __name__ == "__main__":
    main()
