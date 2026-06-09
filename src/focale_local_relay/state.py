from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ._environment import ENVIRONMENT as BAKED_ENVIRONMENT
from .exceptions import FocaleStateError


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _restrict_permissions(path: Path) -> None:
    if os.name != "nt" and path.exists():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@dataclass
class AuthSession:
    username: str
    access_token: str
    auth_type: str = "token"
    access_exp: int | None = None
    refresh_token: str | None = None
    refresh_exp: int | None = None
    created_at: str = field(default_factory=_utcnow)


@dataclass
class InstallationRecord:
    agent_uuid: str
    public_key_b64: str
    scope_type: str
    scope_value: str
    created_at: str = field(default_factory=_utcnow)


@dataclass
class AlpacaServerRecord:
    scope_type: str
    scope_value: str
    address: str
    name: str
    manufacturer: str | None = None
    remote_uuid: str | None = None
    last_seen_at: str = field(default_factory=_utcnow)
    registered_at: str | None = None


@dataclass
class CenteringConfig:
    """Persisted parameters for the Hub-triggered centering procedure."""
    duration: float = 5.0
    max_iterations: int = 10
    min_peaks: int = 20
    success_threshold: float = 10.0
    failure_threshold: float = 300.0
    max_duration_adjustments: int = 2
    cache_dir: str | None = None


@dataclass
class FocaleState:
    workspace_id: str
    environment: str | None = None
    api_server: str | None = None
    hub_url: str | None = None
    default_organisation: str | None = None
    auth: AuthSession | None = None
    installations: dict[str, InstallationRecord] = field(default_factory=dict)
    alpaca_servers: dict[str, AlpacaServerRecord] = field(default_factory=dict)
    centering: CenteringConfig = field(default_factory=CenteringConfig)

    @classmethod
    def config_dir(cls) -> Path:
        app_dir = "focale" if BAKED_ENVIRONMENT == "production" else f"focale-{BAKED_ENVIRONMENT}"
        home = Path.home()
        if os.name == "nt":
            root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
            base = Path(root) if root else home / "AppData" / "Roaming"
            return base / "FocaleLocalRelay" / app_dir
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / app_dir
        xdg_root = os.environ.get("XDG_CONFIG_HOME")
        if xdg_root:
            return Path(xdg_root).expanduser() / app_dir
        return home / ".config" / app_dir

    @classmethod
    def state_file(cls) -> Path:
        return cls.config_dir() / "state.json"

    @classmethod
    def private_key_file(cls) -> Path:
        return cls.config_dir() / "agent-key.pem"

    @staticmethod
    def scope_key(scope_type: str, scope_value: str) -> str:
        return f"{scope_type}:{scope_value}"

    @staticmethod
    def alpaca_key(scope_type: str, scope_value: str, address: str) -> str:
        return f"{scope_type}:{scope_value}:{address}"

    @classmethod
    def load(cls) -> "FocaleState":
        path = cls.state_file()
        if not path.exists():
            return cls(workspace_id=uuid.uuid4().hex)

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FocaleStateError(f"Unable to read {path}: {exc}") from exc

        installs = {}
        alpaca_servers = {}
        try:
            for key, record in (data.get("installations") or {}).items():
                installs[key] = InstallationRecord(**record)
            for key, record in (data.get("alpaca_servers") or {}).items():
                alpaca_servers[key] = AlpacaServerRecord(**record)
        except TypeError as exc:
            raise FocaleStateError(f"Invalid state record in {path}: {exc}") from exc

        workspace_id = data.get("workspace_id") or uuid.uuid4().hex
        auth_payload = data.get("auth")
        auth = None
        if auth_payload:
            try:
                auth = AuthSession(**auth_payload)
            except TypeError as exc:
                raise FocaleStateError(f"Invalid auth record in {path}: {exc}") from exc

        centering = CenteringConfig()
        centering_payload = data.get("centering")
        if centering_payload and isinstance(centering_payload, dict):
            try:
                centering = CenteringConfig(**centering_payload)
            except TypeError:
                pass

        return cls(
            workspace_id=workspace_id,
            environment=data.get("environment"),
            api_server=data.get("api_server"),
            hub_url=data.get("hub_url"),
            default_organisation=data.get("default_organisation"),
            auth=auth,
            installations=installs,
            alpaca_servers=alpaca_servers,
            centering=centering,
        )

    def save(self) -> None:
        directory = self.config_dir()
        directory.mkdir(parents=True, exist_ok=True)

        payload = {
            "workspace_id": self.workspace_id,
            "environment": self.environment,
            "api_server": self.api_server,
            "hub_url": self.hub_url,
            "default_organisation": self.default_organisation,
            "auth": asdict(self.auth) if self.auth else None,
            "installations": {
                key: asdict(record) for key, record in self.installations.items()
            },
            "alpaca_servers": {
                key: asdict(record) for key, record in self.alpaca_servers.items()
            },
            "centering": asdict(self.centering),
        }
        path = self.state_file()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        _restrict_permissions(path)

    def get_installation(
        self, *, scope_type: str, scope_value: str
    ) -> InstallationRecord | None:
        return self.installations.get(self.scope_key(scope_type, scope_value))

    def set_installation(self, record: InstallationRecord) -> None:
        key = self.scope_key(record.scope_type, record.scope_value)
        self.installations[key] = record

    def clear_installation(self, *, scope_type: str, scope_value: str) -> None:
        key = self.scope_key(scope_type, scope_value)
        self.installations.pop(key, None)

    def get_alpaca_server(
        self, *, scope_type: str, scope_value: str, address: str
    ) -> AlpacaServerRecord | None:
        return self.alpaca_servers.get(self.alpaca_key(scope_type, scope_value, address))

    def set_alpaca_server(self, record: AlpacaServerRecord) -> None:
        key = self.alpaca_key(record.scope_type, record.scope_value, record.address)
        self.alpaca_servers[key] = record

    def clear_remote_state(self) -> None:
        self.auth = None
        self.default_organisation = None
        self.installations.clear()
        self.alpaca_servers.clear()
