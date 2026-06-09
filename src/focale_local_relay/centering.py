"""
Local implementation of the plate-solving centering loop, ported from the
Focale backend's PlateSolver / center_on_coordinates Celery task.

All Django / Celery / TaskExecutor dependencies have been replaced with direct
Alpaca REST calls (via alpaca.py) and the local astrometry solver
(via platesolver.py).
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

import numpy as np

from .alpaca import (
    camera_get_image_array,
    camera_start_exposure,
    camera_wait_image_ready,
    telescope_set_tracking,
    telescope_slew_async,
    telescope_sync_to_coordinates,
    telescope_wait_slew_done,
)
from .exceptions import FocaleError
from .platesolver import PlateSolverClient

Logger = Callable[[str], None]


# ------------------------------------------------------------------ #
# Peak finding (ported from focale-back _solving.py)                 #
# ------------------------------------------------------------------ #

def find_peaks_for_centering(
    data: np.ndarray,
    target_n: int = 50,
    threshold_sigma: float = 3.0,
) -> np.ndarray:
    """
    Return up to *target_n* (x, y) peak coordinates from a 2-D float image,
    biased toward bright and spatially well-distributed stars.
    Uses only scipy so that scikit-image is not required.
    """
    from scipy.ndimage import gaussian_filter, maximum_filter

    if data.ndim != 2:
        data = data.mean(axis=0)

    H, W = data.shape
    smoothed = gaussian_filter(data.astype(float), sigma=1.0)

    med = np.nanmedian(smoothed)
    sig = np.nanstd(smoothed)
    thr = med + threshold_sigma * sig

    neighborhood = max(5, int(min(H, W) / 100))
    local_max_mask = (maximum_filter(smoothed, size=neighborhood) == smoothed) & (smoothed > thr)

    yx = np.argwhere(local_max_mask)
    if len(yx) == 0:
        return np.empty((0, 2), dtype=float)

    scores = smoothed[yx[:, 0], yx[:, 1]]
    order = np.argsort(scores)[::-1]
    yx = yx[order[:target_n * 3]]      # keep a surplus for the grid filter
    scores = scores[order[:target_n * 3]]

    # Spatial distribution: suppress close neighbours
    min_sep = int(np.clip(round(min(H, W) / 120), 6, 18))
    xy = _suppress_close(yx[:, ::-1].astype(float), scores, min_sep=min_sep, max_out=target_n)
    return xy


def _suppress_close(
    xy: np.ndarray,
    score: np.ndarray,
    min_sep: int,
    max_out: int | None = None,
) -> np.ndarray:
    order = np.argsort(score)[::-1]
    xy = xy[order]
    score = score[order]
    keep: list[np.ndarray] = []
    r2 = float(min_sep * min_sep)
    for p in xy:
        if not keep:
            keep.append(p)
        else:
            d2 = np.sum((np.asarray(keep) - p) ** 2, axis=1)
            if np.all(d2 >= r2):
                keep.append(p)
        if max_out is not None and len(keep) >= max_out:
            break
    return np.asarray(keep, dtype=float) if keep else np.empty((0, 2), dtype=float)


# ------------------------------------------------------------------ #
# Geometry                                                            #
# ------------------------------------------------------------------ #

def angular_separation_arcsec(
    ra1_deg: float, dec1_deg: float,
    ra2_deg: float, dec2_deg: float,
) -> float:
    """Great-circle separation in arcseconds (Haversine formula)."""
    ra1 = math.radians(ra1_deg)
    dec1 = math.radians(dec1_deg)
    ra2 = math.radians(ra2_deg)
    dec2 = math.radians(dec2_deg)
    dra = ra2 - ra1
    ddec = dec2 - dec1
    a = math.sin(ddec / 2) ** 2 + math.cos(dec1) * math.cos(dec2) * math.sin(dra / 2) ** 2
    c = 2.0 * math.asin(math.sqrt(max(0.0, min(1.0, a))))
    return math.degrees(c) * 3600.0


# ------------------------------------------------------------------ #
# Result                                                              #
# ------------------------------------------------------------------ #

@dataclass
class CenteringResult:
    success: bool
    iterations: int
    final_separation_arcsec: float | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ #
# Centering loop                                                      #
# ------------------------------------------------------------------ #

class CenteringLoop:
    """
    Local equivalent of the backend ``center_on_coordinates`` Celery task.

    Defaults mirror those of the backend ``PlateSolver`` class exactly:
      duration=5, max_iterations=10, min_peaks=20,
      success_threshold=10", failure_threshold=300",
      max_duration_adjustments=2
    """

    def __init__(
        self,
        *,
        camera_address: str,
        camera_number: int,
        telescope_address: str,
        telescope_number: int,
        target_ra_hours: float,
        target_dec_deg: float,
        cache_dir: str | None,
        scales: list[int],
        # --- centering knobs (mirrors backend PlateSolver kwargs) ---
        duration: float = 5.0,
        max_iterations: int = 10,
        min_peaks: int = 20,
        success_threshold: float = 10.0,   # arcseconds
        failure_threshold: float = 300.0,  # arcseconds
        max_duration_adjustments: int = 2,
    ) -> None:
        self._camera_address = camera_address
        self._camera_number = camera_number
        self._telescope_address = telescope_address
        self._telescope_number = telescope_number
        self._target_ra_hours = target_ra_hours
        self._target_dec_deg = target_dec_deg
        self._cache_dir = cache_dir
        self._scales = scales

        self._duration = duration
        self._max_iterations = max_iterations
        self._min_peaks = min_peaks
        self._success_threshold = success_threshold
        self._failure_threshold = failure_threshold
        self._max_duration_adjustments = max_duration_adjustments

        # --- runtime state ---
        self._num_iterations = 0
        self._duration_adjustments = 0
        self._peaks: np.ndarray = np.empty((0, 2))
        self._solution: Any = None
        self._separation: float = 1e6
        self._success: bool | None = None

    # --- derived helpers ---

    @property
    def _target_ra_deg(self) -> float:
        return self._target_ra_hours * 15.0

    @property
    def _num_peaks(self) -> int:
        return len(self._peaks)

    @property
    def _should_stop(self) -> bool:
        return (
            self._separation < self._success_threshold
            or self._num_iterations >= self._max_iterations
            or self._success is False
        )

    @property
    def _should_adjust_duration(self) -> bool:
        return (
            self._num_peaks < self._min_peaks
            and self._duration_adjustments < self._max_duration_adjustments
        )

    @property
    def _should_abort_due_to_peaks(self) -> bool:
        return (
            self._num_peaks < self._min_peaks
            and self._duration_adjustments == self._max_duration_adjustments
        )

    @property
    def _should_abort_due_to_separation(self) -> bool:
        return self._failure_threshold < self._separation < 1e6

    # --- main entry point ---

    def run(self, echo: Logger) -> CenteringResult:
        solver = PlateSolverClient(
            cache_dir=self._cache_dir,
            scales=self._scales,
        )
        try:
            telescope_set_tracking(self._telescope_address, self._telescope_number, True)
            echo("Telescope tracking enabled.")

            while not self._should_stop:
                self._num_iterations += 1
                echo(f"=== Iteration {self._num_iterations} / {self._max_iterations} ===")

                # --- slew ---
                echo(
                    f"Slewing to RA={self._target_ra_hours:.4f}h "
                    f"Dec={self._target_dec_deg:.4f}°..."
                )
                telescope_slew_async(
                    self._telescope_address, self._telescope_number,
                    self._target_ra_hours, self._target_dec_deg,
                )
                telescope_wait_slew_done(self._telescope_address, self._telescope_number)
                echo("Telescope on target.")

                # --- expose ---
                echo(f"Taking {self._duration}s exposure...")
                camera_start_exposure(
                    self._camera_address, self._camera_number, self._duration
                )
                camera_wait_image_ready(
                    self._camera_address, self._camera_number,
                    timeout_s=self._duration + 60.0,
                )
                image = camera_get_image_array(self._camera_address, self._camera_number)
                echo(f"Exposure complete — image {image.shape[1]}×{image.shape[0]} px.")

                # --- find peaks ---
                self._peaks = find_peaks_for_centering(image)
                echo(f"Found {self._num_peaks} peaks in image.")

                if self._should_adjust_duration:
                    self._duration *= 2
                    self._duration_adjustments += 1
                    self._max_iterations += 1
                    echo(
                        f"Too few peaks ({self._num_peaks} < {self._min_peaks}). "
                        f"Increasing exposure to {self._duration}s."
                    )
                    continue

                if self._should_abort_due_to_peaks:
                    self._success = False
                    echo(
                        f"Still fewer than {self._min_peaks} peaks at {self._duration}s. Aborting."
                    )
                    break

                # --- plate solve ---
                echo("Plate solving...")
                self._solution = solver.solve(
                    peaks_xy=self._peaks.tolist(),
                    ra_deg=self._target_ra_deg,
                    dec_deg=self._target_dec_deg,
                )

                if self._solution.status != "match":
                    self._success = False
                    echo("No astrometric solution found. Aborting.")
                    break

                # --- evaluate separation ---
                self._separation = angular_separation_arcsec(
                    self._target_ra_deg,
                    self._target_dec_deg,
                    self._solution.center_ra_deg,
                    self._solution.center_dec_deg,
                )
                echo(
                    f"Solution: RA={self._solution.center_ra_deg / 15:.4f}h "
                    f"Dec={self._solution.center_dec_deg:.4f}° "
                    f"scale={self._solution.scale_arcsec_per_pixel:.3f}\"/px — "
                    f"separation {self._separation:.1f}\"."
                )

                if self._should_abort_due_to_separation:
                    self._success = False
                    echo(
                        f"Separation {self._separation:.1f}\" exceeds failure threshold "
                        f"{self._failure_threshold}\". Aborting."
                    )
                    break

                # --- sync telescope ---
                if self._separation >= self._success_threshold:
                    wcs_ra_hours = self._solution.center_ra_deg / 15.0
                    echo(
                        f"Syncing telescope to WCS coords "
                        f"(RA={wcs_ra_hours:.4f}h Dec={self._solution.center_dec_deg:.4f}°)."
                    )
                    telescope_sync_to_coordinates(
                        self._telescope_address, self._telescope_number,
                        wcs_ra_hours, self._solution.center_dec_deg,
                    )
                    echo("Telescope synced.")

        finally:
            solver.close()

        # --- final status ---
        sep_str = f"{self._separation:.1f}\"" if self._separation < 1e6 else "unknown"
        if self._separation < self._success_threshold:
            self._success = True
            msg = (
                f"Centering succeeded in {self._num_iterations} iteration(s). "
                f"Final separation: {sep_str}."
            )
        elif self._success is False:
            msg = f"Centering aborted after {self._num_iterations} iteration(s). Last separation: {sep_str}."
        else:
            msg = (
                f"Max iterations ({self._max_iterations}) reached. "
                f"Final separation: {sep_str}."
            )

        echo(msg)
        return CenteringResult(
            success=bool(self._success),
            iterations=self._num_iterations,
            final_separation_arcsec=self._separation if self._separation < 1e6 else None,
            message=msg,
        )
