from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from focale_local_relay.state import AlpacaServerRecord, AuthSession, FocaleState, InstallationRecord


def test_state_roundtrip():
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with patch.object(
            FocaleState,
            "config_dir",
            classmethod(lambda cls: tmp_path),
        ):
            state = FocaleState(
                workspace_id="workspace-1",
                hub_url="wss://hub.example/ws/agent",
                default_organisation="my-observatory",
                auth=AuthSession(
                    username="alice",
                    access_token="access-token",
                    access_exp=123,
                    refresh_token="refresh-token",
                    refresh_exp=456,
                ),
            )
            state.set_installation(
                InstallationRecord(
                    agent_uuid="agent-1",
                    public_key_b64="public-key",
                    scope_type="profile",
                    scope_value="alice",
                )
            )
            state.set_alpaca_server(
                AlpacaServerRecord(
                    scope_type="organisation",
                    scope_value="my-observatory",
                    address="http://192.168.1.10:11111",
                    name="ASCOM Remote",
                    manufacturer="ASCOM Initiative",
                    remote_uuid="server-uuid",
                    registered_at="2026-01-01T00:00:00+00:00",
                )
            )
            state.save()

            loaded = FocaleState.load()

            assert loaded.workspace_id == "workspace-1"
            assert loaded.hub_url == "wss://hub.example/ws/agent"
            assert loaded.default_organisation == "my-observatory"
            assert loaded.auth is not None
            assert loaded.auth.username == "alice"
            assert loaded.auth.refresh_token == "refresh-token"
            record = loaded.get_installation(scope_type="profile", scope_value="alice")
            assert record is not None
            assert record.agent_uuid == "agent-1"
            alpaca = loaded.get_alpaca_server(
                scope_type="organisation",
                scope_value="my-observatory",
                address="http://192.168.1.10:11111",
            )
            assert alpaca is not None
            assert alpaca.remote_uuid == "server-uuid"


def test_missing_state_generates_workspace_id():
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with patch.object(
            FocaleState,
            "config_dir",
            classmethod(lambda cls: tmp_path),
        ):
            state = FocaleState.load()

            assert state.workspace_id
            assert state.hub_url is None
            assert state.default_organisation is None
            assert state.installations == {}
            assert state.alpaca_servers == {}
