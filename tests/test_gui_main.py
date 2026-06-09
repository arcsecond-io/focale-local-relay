import runpy
import sys
import types

import pytest


def test_gui_main_supports_top_level_execution(monkeypatch) -> None:
    package = types.ModuleType("focale_local_relay")
    package.__path__ = []
    gui_module = types.ModuleType("focale_local_relay.gui")

    calls: list[str] = []

    def fake_main() -> int:
        calls.append("called")
        return 0

    gui_module.main = fake_main

    monkeypatch.setitem(sys.modules, "focale_local_relay", package)
    monkeypatch.setitem(sys.modules, "focale_local_relay.gui", gui_module)

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path("src/focale_local_relay/gui_main.py", run_name="__main__")

    assert excinfo.value.code == 0
    assert calls == ["called"]
