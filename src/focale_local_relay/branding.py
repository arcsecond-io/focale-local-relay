from __future__ import annotations

import sys
from pathlib import Path

from ._environment import ENVIRONMENT as BAKED_ENVIRONMENT

APP_NAME = "Focale Relay"
APP_DESCRIPTION = (
    "Desktop controls for Focale Relay session setup, Hub diagnostics, "
    "and local plate solving."
)
ACCOUNT_GROUP_TITLE = "Focale Relay Account"
DEFAULT_ENVIRONMENT_LABEL = "Focale Relay Cloud"

_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_WINDOW_ICON_CANDIDATES = (
    "app-icon.png",
    "app-icon.ico",
    "app-icon.svg",
)
_BUILD_ICON_CANDIDATES = (
    "app-icon.ico",
    "app-icon.icns",
    "app-icon.png",
)


def display_name(environment: str = BAKED_ENVIRONMENT) -> str:
    if environment == "production":
        return APP_NAME
    return f"{APP_NAME} {environment.capitalize()}"


def default_environment_label(environment: str = BAKED_ENVIRONMENT) -> str:
    if environment == "production":
        return DEFAULT_ENVIRONMENT_LABEL
    return f"{APP_NAME} {environment.capitalize()}"


def window_title(version: str, environment: str = BAKED_ENVIRONMENT) -> str:
    env_suffix = ""
    if environment != "production":
        env_suffix = f" - {environment.capitalize()}"
    return f"{APP_NAME} {version}{env_suffix}"


def _asset_path(name: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        bundled = Path(getattr(sys, "_MEIPASS")) / "focale_local_relay" / "assets" / name
        if bundled.exists():
            return bundled
    return _ASSET_DIR / name


def find_window_icon_path() -> Path | None:
    for name in _WINDOW_ICON_CANDIDATES:
        path = _asset_path(name)
        if path.exists():
            return path
    return None


def find_build_icon_path() -> Path | None:
    for name in _BUILD_ICON_CANDIDATES:
        path = _asset_path(name)
        if path.exists():
            return path
    return None
