from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import astrometry

from .exceptions import FocaleError


@dataclass(frozen=True)
class PlateSolveResult:
    status: str
    center_ra_deg: float | None = None
    center_dec_deg: float | None = None
    scale_arcsec_per_pixel: float | None = None
    wcs_header: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PlateSolverClient:
    def __init__(
        self,
        *,
        cache_dir: str | None = None,
        scales: list[int] | None = None,
        positional_noise_pixels: float = 1.0,
        sip_order: int = 3,
        tune_up_logodds_threshold: float | None = 14.0,
        output_logodds_threshold: float = 21.0,
        minimum_quad_size_fraction: float = 0.1,
        maximum_quads: int = 0,
    ) -> None:
        self.cache_dir = cache_dir
        self.scales = scales or [6]
        self.positional_noise_pixels = positional_noise_pixels
        self.sip_order = sip_order
        self.tune_up_logodds_threshold = tune_up_logodds_threshold
        self.output_logodds_threshold = output_logodds_threshold
        self.minimum_quad_size_fraction = minimum_quad_size_fraction
        self.maximum_quads = maximum_quads
        self._solver: astrometry.Solver | None = None
        self._init_solver()

    @property
    def is_ready(self) -> bool:
        return self._solver is not None

    def close(self) -> None:
        if self._solver is not None:
            self._solver.close()
            self._solver = None

    def health(self) -> dict[str, Any]:
        if not self.is_ready:
            raise FocaleError("Local plate solver failed to initialize.")
        return {"ok": True, "mode": "local", "scales": self.scales}

    def solve(
        self,
        *,
        peaks_xy: list[list[float]],
        ra_deg: float | None = None,
        dec_deg: float | None = None,
        radius_deg: float | None = None,
        lower_arcsec_per_pixel: float | None = None,
        upper_arcsec_per_pixel: float | None = None,
    ) -> PlateSolveResult:
        if not self._solver:
            raise FocaleError("Local plate solver is unavailable.")

        solution_parameters = astrometry.SolutionParameters(
            positional_noise_pixels=self.positional_noise_pixels,
            sip_order=self.sip_order,
            tune_up_logodds_threshold=self.tune_up_logodds_threshold,
            output_logodds_threshold=self.output_logodds_threshold,
            minimum_quad_size_fraction=self.minimum_quad_size_fraction,
            maximum_quads=self.maximum_quads,
        )
        result = self._solver.solve(
            peaks_xy,
            size_hint=_size_hint(lower_arcsec_per_pixel, upper_arcsec_per_pixel),
            position_hint=_position_hint(ra_deg, dec_deg, radius_deg),
            solution_parameters=solution_parameters,
        )
        if not result.has_match():
            return PlateSolveResult(status="no_match")
        match = result.best_match()
        return PlateSolveResult(
            status="match",
            center_ra_deg=match.center_ra_deg,
            center_dec_deg=match.center_dec_deg,
            scale_arcsec_per_pixel=match.scale_arcsec_per_pixel,
            wcs_header={key: value for key, (value, _comment) in match.wcs_fields.items()},
        )

    def _init_solver(self) -> None:
        cache = (
            Path(self.cache_dir)
            if self.cache_dir
            else Path.home() / ".cache" / "focale" / "astrometry"
        )
        cache.mkdir(parents=True, exist_ok=True)
        index_files = _index_files(cache, set(self.scales))
        try:
            self._solver = astrometry.Solver(index_files)
        except Exception as exc:
            # The astrometry library says "loading <path> failed" when a .fits
            # file on disk is corrupted (e.g. from an interrupted download).
            # Delete every .fits file mentioned in the error message and retry once.
            deleted = _delete_mentioned_fits(str(exc), index_files)
            if deleted:
                # Re-fetch (re-download) the index list after removing bad files.
                index_files = _index_files(cache, set(self.scales))
                try:
                    self._solver = astrometry.Solver(index_files)
                    return
                except Exception as exc2:
                    exc = exc2
            raise FocaleError(f"Local plate solver failed to initialize: {exc}") from exc


def _index_files(cache_dir: Path, scales: set[int]) -> list[Path]:
    invalid_scales = sorted(scale for scale in scales if scale < 0 or scale > 19)
    if invalid_scales:
        joined = ", ".join(str(scale) for scale in invalid_scales)
        raise FocaleError(
            f"Invalid astrometry scales: {joined}. Expected integers between 0 and 19."
        )

    index_files: list[Path] = []
    light_scales = {scale for scale in scales if scale <= 6}
    wide_scales = {scale for scale in scales if scale >= 7}

    if light_scales:
        index_files.extend(
            astrometry.series_5200.index_files(
                cache_directory=cache_dir,
                scales=light_scales,
            )
        )
    if wide_scales:
        index_files.extend(
            astrometry.series_4100.index_files(
                cache_directory=cache_dir,
                scales=wide_scales,
            )
        )
    return index_files


def _delete_mentioned_fits(error_msg: str, index_files: list[Path]) -> bool:
    """
    Parse an astrometry error message for .fits paths, delete those files,
    and return True if at least one file was removed.
    """
    # Build a set of known paths for fast membership testing.
    known = {str(p): p for p in index_files}

    # The library typically says: loading "<path>" failed
    mentioned = set(re.findall(r'"([^"]+\.fits)"', error_msg))
    # Also try unquoted paths in case the format varies.
    mentioned |= set(re.findall(r'((?:/[^\s]+)+\.fits)', error_msg))

    deleted = False
    for raw_path in mentioned:
        path = Path(raw_path)
        if path.exists():
            try:
                path.unlink()
                deleted = True
            except OSError:
                pass
        # Also delete by matching basename against known index files.
        elif raw_path in known:
            try:
                known[raw_path].unlink()
                deleted = True
            except OSError:
                pass
    return deleted


def _size_hint(
    lower: float | None,
    upper: float | None,
) -> astrometry.SizeHint | None:
    if lower is None and upper is None:
        return None
    return astrometry.SizeHint(
        lower_arcsec_per_pixel=(
            astrometry.DEFAULT_LOWER_ARCSEC_PER_PIXEL if lower is None else lower
        ),
        upper_arcsec_per_pixel=(
            astrometry.DEFAULT_UPPER_ARCSEC_PER_PIXEL if upper is None else upper
        ),
    )


def _position_hint(
    ra_deg: float | None,
    dec_deg: float | None,
    radius_deg: float | None,
) -> astrometry.PositionHint | None:
    if ra_deg is None and dec_deg is None and radius_deg is None:
        return None
    if ra_deg is None or dec_deg is None:
        raise FocaleError("Position hint requires both RA and Dec.")
    return astrometry.PositionHint(
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        radius_deg=180.0 if radius_deg is None else radius_deg,
    )
