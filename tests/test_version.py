from importlib.metadata import PackageNotFoundError

import focale_local_relay as focale


def test_detect_version_prefers_installed_metadata(monkeypatch) -> None:
    monkeypatch.setattr(focale, "version", lambda name: "9.9.9")
    monkeypatch.setattr(focale, "_read_version_from_pyproject", lambda: "0.0.0")

    assert focale._detect_version() == "9.9.9"


def test_detect_version_falls_back_to_pyproject(monkeypatch) -> None:
    def raise_package_not_found(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(focale, "version", raise_package_not_found)
    monkeypatch.setattr(focale, "_read_version_from_pyproject", lambda: "0.3.2")

    assert focale._detect_version() == "0.3.2"
