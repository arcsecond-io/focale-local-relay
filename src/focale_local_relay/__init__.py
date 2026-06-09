from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib

__all__ = ["__version__"]


def _read_version_from_pyproject() -> str:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        return str(tomllib.load(pyproject_file)["project"]["version"])


def _detect_version() -> str:
    try:
        return version("focale-local-relay")
    except PackageNotFoundError:
        return _read_version_from_pyproject()


__version__ = _detect_version()
