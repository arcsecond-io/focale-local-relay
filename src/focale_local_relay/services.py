from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from dataclasses import asdict

from . import __version__
from . import branding
from ._environment import ENVIRONMENT as BAKED_ENVIRONMENT
from .agent_auth import AgentKeypair
from .alpaca import (
    ConfiguredAlpacaDevice,
    DiscoveredAlpacaServer,
    discover_alpaca_servers,
    get_configured_devices,
    get_telescope_site_coordinates,
    normalize_alpaca_address,
)
from .hub_gateway import HubGateway
from .exceptions import FocaleError, HubGatewayError
from .hub import HubClient
from .centering import CenteringLoop
from .platesolver import PlateSolverClient
from .state import AlpacaServerRecord, CenteringConfig, FocaleState, InstallationRecord

Logger = Callable[[str], None]
TrafficCallback = Callable[[dict[str, Any]], None]


ENVIRONMENT_PRESETS: dict[str, dict[str, str]] = {
    "production": {
        "label": branding.DEFAULT_ENVIRONMENT_LABEL,
        "api_server": "https://api.focale.space",
        "hub_url": "wss://hub.focale.space/ws/agent",
    },
    "staging": {
        "label": branding.display_name("staging"),
        "api_server": "https://api.focale.dev",
        "hub_url": "wss://hub.focale.dev/ws/agent",
    },
    "dev": {
        "label": branding.display_name("dev"),
        "api_server": "http://localhost:8000",
        "hub_url": "ws://localhost:8002/ws/agent",
    },
}

SITE_SCOPED_DEVICE_PATHS: dict[str, str] = {
    "Dome": "enclosures",
    "SafetyMonitor": "safetymonitors",
    "ObservingConditions": "observingconditions",
}

TELESCOPE_SCOPED_DEVICE_PATHS: dict[str, str] = {
    "Camera": "cameras",
    "FilterWheel": "filterwheels",
    "Rotator": "rotators",
    "Focuser": "focusers",
    "Switch": "switches",
    "CoverCalibrator": "covercalibrators",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope(organisation: str | None, username: str) -> tuple[str, str]:
    if organisation:
        return "organisation", organisation
    return "profile", username


def environment_ids() -> list[str]:
    return list(ENVIRONMENT_PRESETS.keys())


def environment_label(environment: str | None) -> str:
    if environment and environment in ENVIRONMENT_PRESETS:
        return ENVIRONMENT_PRESETS[environment]["label"]
    return "Custom"


def infer_environment(state: FocaleState) -> str | None:
    for environment, preset in ENVIRONMENT_PRESETS.items():
        if (
            state.api_server == preset["api_server"]
            and normalize_hub_url(state.hub_url) == normalize_hub_url(preset["hub_url"])
        ):
            return environment
    return None


def environment_defaults(environment: str | None) -> dict[str, str]:
    env_key = environment or "production"
    preset = ENVIRONMENT_PRESETS.get(env_key)
    if preset is None:
        raise FocaleError(f"Unknown environment `{env_key}`.")
    return dict(preset)


def context_label(organisation: str | None) -> str:
    if organisation:
        return f"organisation `{organisation}`"
    return "personal"


def resolve_context_organisation(
    state: FocaleState,
    organisation: str | None,
) -> str | None:
    if organisation is not None:
        return organisation.strip() or None
    if state.default_organisation:
        return state.default_organisation
    return None


def resolve_hub_url(state: FocaleState, hub_url: str | None) -> str:
    resolved = normalize_hub_url(hub_url) if hub_url else normalize_hub_url(state.hub_url)
    if not resolved:
        raise FocaleError(
            "No Hub URL is configured. Provide one before connecting or running diagnostics."
        )
    return resolved


def normalize_hub_url(hub_url: str | None) -> str | None:
    if hub_url is None:
        return None

    raw = hub_url.strip()
    if not raw:
        return None

    if raw.startswith("//"):
        raw = f"wss:{raw}"
    elif "://" not in raw:
        raw = f"wss://{raw}"

    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme == "http":
        scheme = "ws"
    elif scheme == "https":
        scheme = "wss"
    elif scheme not in {"ws", "wss"}:
        raise FocaleError(
            "Hub URL must use ws://, wss://, http://, or https://."
        )

    if not parsed.netloc:
        raise FocaleError("Hub URL must include a host.")

    path = parsed.path or "/ws/agent"
    if path == "/":
        path = "/ws/agent"

    return urlunsplit((scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def parse_scales(scales: str) -> list[int]:
    values: list[int] = []
    for part in scales.split(","):
        raw = part.strip()
        if not raw:
            continue
        try:
            values.append(int(raw))
        except ValueError as exc:
            raise FocaleError(
                f"Invalid scale value `{raw}`. Use comma-separated integers, e.g. 6 or 5,6."
            ) from exc
    if not values:
        raise FocaleError("At least one scale must be provided.")
    return values


def load_peaks_file(path: Path) -> list[list[float]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FocaleError(f"Unable to read peaks file `{path}`: {exc}") from exc

    peaks = payload.get("peaks_xy") if isinstance(payload, dict) else payload
    if not isinstance(peaks, list):
        raise FocaleError("Peaks payload must be a list or an object with `peaks_xy`.")

    parsed: list[list[float]] = []
    for row in peaks:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise FocaleError("Each peak must be a two-value [x, y] array.")
        try:
            parsed.append([float(row[0]), float(row[1])])
        except (TypeError, ValueError) as exc:
            raise FocaleError("Each peak coordinate must be numeric.") from exc
    return parsed


def _gateway(
    *,
    state: FocaleState,
    api_server: str | None,
) -> HubGateway:
    if api_server and api_server != state.api_server:
        state.api_server = api_server
        state.save()
    return HubGateway(
        state=state,
        api_server=api_server,
    )


def _ensure_installation(
    gateway: HubGateway,
    state: FocaleState,
    keypair: AgentKeypair,
    *,
    organisation: str | None,
    re_enroll: bool,
    echo: Logger,
) -> InstallationRecord:
    username = gateway.require_username()
    scope_type, scope_value = _scope(organisation, username)
    existing = state.get_installation(scope_type=scope_type, scope_value=scope_value)

    if re_enroll and existing:
        state.clear_installation(scope_type=scope_type, scope_value=scope_value)
        existing = None

    if existing and existing.public_key_b64 == keypair.public_key_b64:
        return existing

    echo("Enrolling a local Hub agent with Focale.")
    agent_uuid = gateway.enroll_agent(
        public_key_b64=keypair.public_key_b64,
        organisation=organisation,
    )
    record = InstallationRecord(
        agent_uuid=agent_uuid,
        public_key_b64=keypair.public_key_b64,
        scope_type=scope_type,
        scope_value=scope_value,
    )
    state.set_installation(record)
    state.save()
    return record


def _discover_and_register_alpaca(
    gateway: HubGateway,
    state: FocaleState,
    *,
    organisation: str | None,
    echo: Logger,
) -> None:
    discovered = discover_alpaca_servers()
    if not discovered:
        echo("No local ASCOM Remote servers discovered.")
        return

    username = gateway.require_username()
    scope_type, scope_value = _scope(organisation, username)
    existing_remote = gateway.list_alpaca_servers(organisation=organisation)
    existing_by_address: dict[str, dict[str, Any]] = {}
    for server in existing_remote:
        address = server.get("address")
        if not address:
            continue
        existing_by_address[normalize_alpaca_address(str(address))] = server

    created = 0
    already = 0
    changed = False
    now = _utcnow()

    for server in discovered:
        normalized_address = normalize_alpaca_address(server.address)
        if not normalized_address:
            continue

        cached = state.get_alpaca_server(
            scope_type=scope_type,
            scope_value=scope_value,
            address=normalized_address,
        )
        remote_match = existing_by_address.get(normalized_address)

        if remote_match:
            remote_uuid = remote_match.get("uuid")
            state.set_alpaca_server(
                AlpacaServerRecord(
                    scope_type=scope_type,
                    scope_value=scope_value,
                    address=normalized_address,
                    name=str(remote_match.get("name") or server.name),
                    manufacturer=(
                        str(remote_match.get("manufacturer"))
                        if remote_match.get("manufacturer")
                        else server.manufacturer
                    ),
                    remote_uuid=str(remote_uuid) if remote_uuid else None,
                    last_seen_at=now,
                    registered_at=(
                        cached.registered_at
                        if cached and cached.registered_at
                        else now
                    ),
                )
            )
            already += 1
            changed = True
            continue

        created_server = gateway.create_alpaca_server(
            name=server.name,
            address=normalized_address,
            manufacturer=server.manufacturer,
            organisation=organisation,
        )
        existing_by_address[normalized_address] = created_server
        created_uuid = created_server.get("uuid")
        state.set_alpaca_server(
            AlpacaServerRecord(
                scope_type=scope_type,
                scope_value=scope_value,
                address=normalized_address,
                name=str(created_server.get("name") or server.name),
                manufacturer=(
                    str(created_server.get("manufacturer"))
                    if created_server.get("manufacturer")
                    else server.manufacturer
                ),
                remote_uuid=str(created_uuid) if created_uuid else None,
                last_seen_at=now,
                registered_at=now,
            )
        )
        created += 1
        changed = True
        echo(f"Registered ASCOM Remote server: {server.name} ({normalized_address})")

    if changed:
        state.save()
    echo(f"ASCOM discovery: {already} already registered, {created} new registrations.")


def login(
    *,
    api_server: str | None,
    username: str,
    secret: str,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_server=api_server)
    gateway.login_with_password(username=username, password=secret)

    return {
        "ok": True,
        "username": gateway.username,
        "auth_mode": "password",
        "api_server": gateway.api_server,
        "environment": state.environment or infer_environment(state),
    }


def user_settings() -> dict[str, Any]:
    state = FocaleState.load()
    defaults = environment_defaults(BAKED_ENVIRONMENT)
    api_server = state.api_server or defaults["api_server"]
    hub_url = state.hub_url or defaults["hub_url"]
    username = state.auth.username if state.auth else None
    return {
        "username": username,
        "api_server": api_server,
        "hub_url": hub_url,
        "environment": BAKED_ENVIRONMENT,
        "environment_label": environment_label(BAKED_ENVIRONMENT),
        "logged_in": state.auth is not None,
    }


def ensure_environment() -> None:
    """Apply the baked-in environment to state if not already configured correctly."""
    state = FocaleState.load()
    preset = environment_defaults(BAKED_ENVIRONMENT)
    if (
        state.environment == BAKED_ENVIRONMENT
        and state.api_server == preset["api_server"]
        and normalize_hub_url(state.hub_url) == normalize_hub_url(preset["hub_url"])
    ):
        return
    select_environment(BAKED_ENVIRONMENT)


def select_environment(environment: str) -> dict[str, Any]:
    preset = environment_defaults(environment)
    state = FocaleState.load()
    previous_environment = state.environment or infer_environment(state)
    changed = (
        previous_environment != environment
        or state.api_server != preset["api_server"]
        or normalize_hub_url(state.hub_url) != normalize_hub_url(preset["hub_url"])
    )

    state.environment = environment
    state.api_server = preset["api_server"]
    state.hub_url = preset["hub_url"]
    if changed:
        state.clear_remote_state()
    state.save()

    return {
        "ok": True,
        "changed": changed,
        "environment": environment,
        "environment_label": environment_label(environment),
        "api_server": state.api_server,
        "hub_url": state.hub_url,
    }


def status(
    *,
    api_server: str | None,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_server=api_server)
    auth_error: str | None = None

    session = state.auth
    if session and session.auth_type == "token":
        access_exp = int(session.access_exp or 0)
        now = int(time.time())
        if not access_exp or now >= access_exp - 30:
            try:
                gateway.ensure_authenticated()
            except HubGatewayError as exc:
                auth_error = str(exc)
                state = FocaleState.load()
                gateway = _gateway(state=state, api_server=api_server)

    current_environment = state.environment or infer_environment(state)
    payload = {
        "focale_version": __version__,
        "api_server": gateway.api_server,
        "environment": current_environment,
        "environment_label": environment_label(current_environment),
        "logged_in": gateway.is_logged_in,
        "username": gateway.username or None,
        "auth_type": gateway.auth_type,
        "has_refresh_token": gateway.has_refresh_token,
        "state_path": str(state.state_file()),
        "workspace_id": state.workspace_id,
        "hub_url": state.hub_url,
        "default_context": context_label(state.default_organisation),
        "known_alpaca_servers": len(state.alpaca_servers),
        "installations": {
            key: {
                "agent_uuid": record.agent_uuid,
                "created_at": record.created_at,
            }
            for key, record in sorted(state.installations.items())
        },
    }
    if auth_error:
        payload["auth_error"] = auth_error
    return payload


def discover_local_alpaca() -> dict[str, Any]:
    discovered = discover_alpaca_servers()
    return {
        "ok": True,
        "count": len(discovered),
        "servers": [
            {
                "name": server.name,
                "address": server.address,
                "manufacturer": server.manufacturer,
            }
            for server in discovered
        ],
    }


def _default_site_name(server: DiscoveredAlpacaServer) -> str:
    base = server.name.strip() or "Focale"
    if base.lower().endswith("observatory"):
        return base
    return f"{base} Observatory"


def _default_telescope_name(server: DiscoveredAlpacaServer) -> str:
    base = server.name.strip() or "Focale"
    if base.lower().endswith("telescope"):
        return base
    return f"{base} Telescope"


def _default_equipment_name(device: ConfiguredAlpacaDevice) -> str:
    name = device.name.strip()
    if name:
        return name
    return f"{device.type} {device.number}"


def _find_by_name(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in items:
        if str(item.get("name") or "").strip() == name:
            return item
    return None


def _find_site_by_uuid(
    sites: list[dict[str, Any]],
    site_uuid: str | None,
) -> dict[str, Any] | None:
    if not site_uuid:
        return None
    for site in sites:
        if str(site.get("uuid") or "") == site_uuid:
            return site
    return None


def _coordinates_payload(
    longitude: float,
    latitude: float,
    height: float | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "longitude": longitude,
        "latitude": latitude,
    }
    if height is not None:
        payload["height"] = height
    return payload


def _ensure_remote_server(
    *,
    gateway: HubGateway,
    state: FocaleState,
    server: DiscoveredAlpacaServer,
    existing_by_address: dict[str, dict[str, Any]],
    organisation: str | None,
    echo: Logger,
) -> tuple[dict[str, Any], bool]:
    normalized_address = normalize_alpaca_address(server.address)
    remote_match = existing_by_address.get(normalized_address)

    username = gateway.require_username()
    scope_type, scope_value = _scope(organisation, username)
    cached = state.get_alpaca_server(
        scope_type=scope_type,
        scope_value=scope_value,
        address=normalized_address,
    )
    now = _utcnow()

    if remote_match is None:
        remote_match = gateway.create_alpaca_server(
            name=server.name,
            address=normalized_address,
            manufacturer=server.manufacturer,
            organisation=organisation,
        )
        existing_by_address[normalized_address] = remote_match
        created = True
        echo(f"Registered ASCOM Remote server: {server.name} ({normalized_address})")
    else:
        created = False

    remote_uuid = remote_match.get("uuid")
    state.set_alpaca_server(
        AlpacaServerRecord(
            scope_type=scope_type,
            scope_value=scope_value,
            address=normalized_address,
            name=str(remote_match.get("name") or server.name),
            manufacturer=(
                str(remote_match.get("manufacturer"))
                if remote_match.get("manufacturer")
                else server.manufacturer
            ),
            remote_uuid=str(remote_uuid) if remote_uuid else None,
            last_seen_at=now,
            registered_at=(cached.registered_at if cached and cached.registered_at else now),
        )
    )

    return remote_match, created


def _ensure_alpaca_devices(
    *,
    gateway: HubGateway,
    remote_server: dict[str, Any],
    configured_devices: list[ConfiguredAlpacaDevice],
    organisation: str | None,
    echo: Logger,
) -> tuple[list[dict[str, Any]], int, int]:
    remote_server_uuid = str(remote_server.get("uuid") or "").strip()
    if not remote_server_uuid:
        raise FocaleError("Focale did not return a UUID for the registered Alpaca server.")

    remote_devices = gateway.list_alpaca_devices(
        server_uuid=remote_server_uuid,
        organisation=organisation,
    )
    devices_by_unique_id = {
        str(device.get("unique_id") or ""): device for device in remote_devices
    }

    created = 0
    already_registered = 0
    ensured_devices: list[dict[str, Any]] = []

    for device in configured_devices:
        existing = devices_by_unique_id.get(device.unique_id)
        if existing is None:
            existing = gateway.create_alpaca_device(
                server_uuid=remote_server_uuid,
                name=device.name,
                number=device.number,
                unique_id=device.unique_id,
                device_type=device.type,
                organisation=organisation,
            )
            devices_by_unique_id[device.unique_id] = existing
            created += 1
            echo(f"Registered Alpaca device: {device.name} ({device.type})")
        else:
            already_registered += 1
        ensured_devices.append(existing)

    return ensured_devices, created, already_registered


def _ensure_observing_site(
    *,
    gateway: HubGateway,
    server: DiscoveredAlpacaServer,
    telescope_device_id: int | None,
    telescope_coordinates: dict[str, Any] | None,
    sites: list[dict[str, Any]],
    telescopes: list[dict[str, Any]],
    organisation: str | None,
    echo: Logger,
) -> tuple[dict[str, Any], bool]:
    preferred_telescope: dict[str, Any] | None = None
    if telescope_device_id is not None:
        for telescope in telescopes:
            if telescope.get("device") == telescope_device_id:
                preferred_telescope = telescope
                break

    if preferred_telescope is None:
        preferred_telescope = _find_by_name(telescopes, _default_telescope_name(server))

    if preferred_telescope is not None:
        site = _find_site_by_uuid(sites, str(preferred_telescope.get("observing_site") or ""))
        if site is not None:
            return site, False

    default_site_name = _default_site_name(server)
    existing_site = _find_by_name(sites, default_site_name)
    if existing_site is not None:
        current_coordinates = existing_site.get("coordinates")
        if telescope_coordinates and not current_coordinates:
            updated_site = gateway.update_observing_site(
                site_uuid=str(existing_site.get("uuid")),
                payload={"coordinates": telescope_coordinates},
                organisation=organisation,
            )
            sites[:] = [
                updated_site if str(site.get("uuid")) == str(updated_site.get("uuid")) else site
                for site in sites
            ]
            echo(f"Updated observing site coordinates for {default_site_name}.")
            return updated_site, False
        return existing_site, False

    if telescope_coordinates is None:
        raise FocaleError(
            "Focale found a local Alpaca server, but could not read telescope site coordinates. "
            "Automatic observing-site creation needs a Telescope device that exposes SiteLatitude and SiteLongitude."
        )

    created_site = gateway.create_observing_site(
        name=default_site_name,
        longitude=float(telescope_coordinates["longitude"]),
        latitude=float(telescope_coordinates["latitude"]),
        height=(
            float(telescope_coordinates["height"])
            if telescope_coordinates.get("height") is not None
            else None
        ),
        organisation=organisation,
    )
    sites.append(created_site)
    echo(f"Created observing site: {default_site_name}")
    return created_site, True


def _ensure_telescope(
    *,
    gateway: HubGateway,
    server: DiscoveredAlpacaServer,
    site: dict[str, Any],
    telescope_device_id: int | None,
    telescopes: list[dict[str, Any]],
    organisation: str | None,
    echo: Logger,
) -> tuple[dict[str, Any] | None, bool]:
    if telescope_device_id is None:
        return None, False

    telescope = None
    for candidate in telescopes:
        if candidate.get("device") == telescope_device_id:
            telescope = candidate
            break

    if telescope is None:
        telescope = _find_by_name(telescopes, _default_telescope_name(server))

    site_uuid = str(site.get("uuid") or "")
    if telescope is None:
        created_telescope = gateway.create_telescope(
            name=_default_telescope_name(server),
            observing_site=site_uuid,
            device_id=telescope_device_id,
            organisation=organisation,
        )
        telescopes.append(created_telescope)
        echo(f"Created telescope: {_default_telescope_name(server)}")
        return created_telescope, True

    updates: dict[str, Any] = {}
    if telescope.get("device") != telescope_device_id:
        updates["device"] = telescope_device_id
    if str(telescope.get("observing_site") or "") != site_uuid:
        updates["observing_site"] = site_uuid

    if updates:
        telescope = gateway.update_telescope(
            telescope_uuid=str(telescope.get("uuid")),
            payload=updates,
            organisation=organisation,
        )
        for index, candidate in enumerate(telescopes):
            if str(candidate.get("uuid") or "") == str(telescope.get("uuid") or ""):
                telescopes[index] = telescope
                break
        echo(f"Updated telescope: {telescope.get('name')}")

    return telescope, False


def _ensure_equipment_for_device(
    *,
    gateway: HubGateway,
    device: dict[str, Any],
    configured_device: ConfiguredAlpacaDevice,
    site: dict[str, Any] | None,
    telescope: dict[str, Any] | None,
    equipment_cache: dict[str, list[dict[str, Any]]],
    organisation: str | None,
    echo: Logger,
) -> bool:
    device_type = configured_device.type
    if device_type in SITE_SCOPED_DEVICE_PATHS:
        equipment_path = SITE_SCOPED_DEVICE_PATHS[device_type]
        if site is None:
            return False
        scope_key = "observing_site"
        scope_value = str(site.get("uuid") or "")
    elif device_type in TELESCOPE_SCOPED_DEVICE_PATHS:
        equipment_path = TELESCOPE_SCOPED_DEVICE_PATHS[device_type]
        if telescope is None:
            return False
        scope_key = "telescope"
        scope_value = str(telescope.get("uuid") or "")
    else:
        return False

    items = equipment_cache.setdefault(
        equipment_path,
        gateway.list_equipment(equipment_path=equipment_path, organisation=organisation),
    )
    device_id = device.get("id")
    if device_id is None:
        return False

    match: dict[str, Any] | None = None
    for item in items:
        if item.get("device") == device_id:
            match = item
            break

    if match is None:
        target_name = _default_equipment_name(configured_device)
        for item in items:
            if (
                str(item.get("name") or "") == target_name
                and str(item.get(scope_key) or "") == scope_value
            ):
                match = item
                break

    if match is None:
        created = gateway.create_equipment(
            equipment_path=equipment_path,
            payload={
                "name": _default_equipment_name(configured_device),
                scope_key: scope_value,
                "device": device_id,
            },
            organisation=organisation,
        )
        items.append(created)
        echo(f"Created {configured_device.type} equipment: {_default_equipment_name(configured_device)}")
        return True

    if match.get("device") != device_id:
        updated = gateway.update_equipment(
            equipment_path=equipment_path,
            equipment_uuid=str(match.get("uuid")),
            payload={"device": device_id},
            organisation=organisation,
        )
        for index, item in enumerate(items):
            if str(item.get("uuid") or "") == str(updated.get("uuid") or ""):
                items[index] = updated
                break
        echo(f"Linked {configured_device.type} equipment: {match.get('name')}")

    return False


def register_local_alpaca(
    *,
    api_server: str | None,
    echo: Logger,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_server=api_server)
    gateway.ensure_authenticated()

    discovered = discover_alpaca_servers()
    if not discovered:
        echo("No local ASCOM Remote servers discovered.")
        return {
            "ok": True,
            "discovered": 0,
            "registered": 0,
            "already_registered": 0,
        }

    existing_remote = gateway.list_alpaca_servers(organisation=None)
    existing_by_address: dict[str, dict[str, Any]] = {}
    for server in existing_remote:
        address = server.get("address")
        if not address:
            continue
        existing_by_address[normalize_alpaca_address(str(address))] = server

    sites = gateway.list_observing_sites(organisation=None)
    telescopes = gateway.list_telescopes(organisation=None)
    equipment_cache: dict[str, list[dict[str, Any]]] = {}

    changed = False
    registered = 0
    already_registered = 0
    devices_registered = 0
    devices_already_registered = 0
    sites_created = 0
    telescopes_created = 0
    equipments_created = 0
    server_summaries: list[dict[str, Any]] = []

    for server in discovered:
        remote_server, server_created = _ensure_remote_server(
            gateway=gateway,
            state=state,
            server=server,
            existing_by_address=existing_by_address,
            organisation=None,
            echo=echo,
        )
        changed = True
        if server_created:
            registered += 1
        else:
            already_registered += 1

        configured_devices = get_configured_devices(server.address)
        ensured_devices, created_device_count, existing_device_count = _ensure_alpaca_devices(
            gateway=gateway,
            remote_server=remote_server,
            configured_devices=configured_devices,
            organisation=None,
            echo=echo,
        )
        devices_registered += created_device_count
        devices_already_registered += existing_device_count
        if created_device_count:
            changed = True

        device_by_unique_id = {
            str(device.get("unique_id") or ""): device for device in ensured_devices
        }
        telescope_configured = next(
            (device for device in configured_devices if device.type == "Telescope"),
            None,
        )
        telescope_remote = (
            device_by_unique_id.get(telescope_configured.unique_id)
            if telescope_configured is not None
            else None
        )
        telescope_device_id = (
            int(telescope_remote["id"])
            if telescope_remote is not None and telescope_remote.get("id") is not None
            else None
        )
        telescope_coordinates = None
        if telescope_configured is not None:
            coords = get_telescope_site_coordinates(
                server.address,
                device_number=telescope_configured.number,
            )
            if coords is not None:
                telescope_coordinates = _coordinates_payload(
                    longitude=coords.longitude,
                    latitude=coords.latitude,
                    height=coords.height,
                )

        site, site_created = _ensure_observing_site(
            gateway=gateway,
            server=server,
            telescope_device_id=telescope_device_id,
            telescope_coordinates=telescope_coordinates,
            sites=sites,
            telescopes=telescopes,
            organisation=None,
            echo=echo,
        )
        if site_created:
            sites_created += 1
            changed = True

        telescope, telescope_created = _ensure_telescope(
            gateway=gateway,
            server=server,
            site=site,
            telescope_device_id=telescope_device_id,
            telescopes=telescopes,
            organisation=None,
            echo=echo,
        )
        if telescope_created:
            telescopes_created += 1
            changed = True

        created_equipment_for_server = 0
        for configured_device in configured_devices:
            remote_device = device_by_unique_id.get(configured_device.unique_id)
            if remote_device is None:
                continue
            if _ensure_equipment_for_device(
                gateway=gateway,
                device=remote_device,
                configured_device=configured_device,
                site=site,
                telescope=telescope,
                equipment_cache=equipment_cache,
                organisation=None,
                echo=echo,
            ):
                equipments_created += 1
                created_equipment_for_server += 1
                changed = True

        server_summaries.append(
            {
                "name": server.name,
                "address": server.address,
                "configured_devices": len(configured_devices),
                "created_site": site_created,
                "created_telescope": telescope_created,
                "created_equipment_count": created_equipment_for_server,
            }
        )

    if changed:
        state.save()

    echo(
        "Observatory setup: "
        f"{already_registered} server(s) already registered, {registered} new server registration(s), "
        f"{devices_registered} new device registration(s), {sites_created} new site(s), "
        f"{telescopes_created} new telescope(s), {equipments_created} new equipment item(s)."
    )
    return {
        "ok": True,
        "discovered": len(discovered),
        "registered": registered,
        "already_registered": already_registered,
        "devices_registered": devices_registered,
        "devices_already_registered": devices_already_registered,
        "sites_created": sites_created,
        "telescopes_created": telescopes_created,
        "equipments_created": equipments_created,
        "environment": state.environment or infer_environment(state),
        "environment_label": environment_label(state.environment or infer_environment(state)),
        "servers_summary": server_summaries,
        "servers": [
            {
                "name": server.name,
                "address": server.address,
                "manufacturer": server.manufacturer,
            }
            for server in discovered
        ],
    }


def list_contexts(
    *,
    api_server: str | None,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_server=api_server)
    gateway.ensure_authenticated()
    return {
        "current_default": context_label(state.default_organisation),
        "personal": gateway.require_username(),
        "organisations": [
            {
                "subdomain": item.subdomain,
                "name": item.name,
                "role": item.role,
            }
            for item in gateway.list_organisation_contexts()
        ],
    }


def set_default_context(
    *,
    api_server: str | None,
    target: str,
    force: bool = False,
) -> dict[str, Any]:
    normalized = target.strip()
    if not normalized:
        raise FocaleError("Context target cannot be empty.")

    state = FocaleState.load()
    gateway = _gateway(state=state, api_server=api_server)

    if normalized.lower() in {"personal", "profile", "me"}:
        state.default_organisation = None
        state.save()
        return {"default_context": "personal"}

    if not force:
        gateway.ensure_authenticated()
        memberships = {item.subdomain for item in gateway.list_organisation_contexts()}
        if normalized not in memberships:
            raise FocaleError(
                f"`{normalized}` is not listed in your memberships. Refresh contexts or force the change."
            )

    state.default_organisation = normalized
    state.save()
    return {"default_context": context_label(normalized)}


def connect_once(
    *,
    api_server: str | None,
    hub_url: str | None,
    organisation: str | None,
    workspace_id: str | None,
    re_enroll: bool,
    discover_alpaca: bool,
    echo: Logger,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_server=api_server)
    keypair = AgentKeypair.load_or_create(state.private_key_file())
    gateway.ensure_authenticated()
    resolved_organisation = resolve_context_organisation(state, organisation)
    resolved_hub_url = resolve_hub_url(state, hub_url)

    if resolved_hub_url != state.hub_url:
        state.hub_url = resolved_hub_url
        state.save()

    record = _ensure_installation(
        gateway,
        state,
        keypair,
        organisation=resolved_organisation,
        re_enroll=re_enroll,
        echo=echo,
    )

    try:
        minted = gateway.mint_agent_token(
            agent_uuid=record.agent_uuid,
            organisation=resolved_organisation,
        )
    except HubGatewayError as exc:
        username = gateway.require_username()
        scope_type, scope_value = _scope(resolved_organisation, username)
        if exc.status != 403 or re_enroll:
            raise

        echo("Stored agent enrollment was rejected. Re-enrolling once.")
        state.clear_installation(scope_type=scope_type, scope_value=scope_value)
        record = _ensure_installation(
            gateway,
            state,
            keypair,
            organisation=resolved_organisation,
            re_enroll=False,
            echo=echo,
        )
        minted = gateway.mint_agent_token(
            agent_uuid=record.agent_uuid,
            organisation=resolved_organisation,
        )

    if discover_alpaca:
        try:
            _discover_and_register_alpaca(
                gateway,
                state,
                organisation=resolved_organisation,
                echo=echo,
            )
        except Exception as exc:  # pragma: no cover - best effort path
            echo(f"ASCOM discovery skipped: {exc}")

    echo(
        f"Connecting to Hub as agent {record.agent_uuid} "
        f"({context_label(resolved_organisation)}) on workspace "
        f"{workspace_id or state.workspace_id}."
    )

    welcome = asyncio.run(
        HubClient(
            hub_url=resolved_hub_url,
            workspace_id=workspace_id or state.workspace_id,
            agent_uuid=record.agent_uuid,
            jwt=minted.jwt,
            keypair=keypair,
        ).connect(once=True, echo=echo)
    )

    return {
        "ok": True,
        "context": context_label(resolved_organisation),
        "hub_url": resolved_hub_url,
        "workspace_id": workspace_id or state.workspace_id,
        "agent_uuid": record.agent_uuid,
        "session_id": welcome.session_id,
        "keepalive_s": welcome.keepalive_s,
    }


def relay_messages(
    *,
    api_server: str | None,
    hub_url: str | None,
    organisation: str | None,
    workspace_id: str | None,
    re_enroll: bool,
    discover_alpaca: bool,
    echo: Logger,
    traffic_callback: TrafficCallback | None = None,
    stop_event: Event | None = None,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_server=api_server)
    keypair = AgentKeypair.load_or_create(state.private_key_file())
    gateway.ensure_authenticated()
    resolved_organisation = resolve_context_organisation(state, organisation)
    resolved_hub_url = resolve_hub_url(state, hub_url)

    if resolved_hub_url != state.hub_url:
        state.hub_url = resolved_hub_url
        state.save()

    record = _ensure_installation(
        gateway,
        state,
        keypair,
        organisation=resolved_organisation,
        re_enroll=re_enroll,
        echo=echo,
    )

    try:
        minted = gateway.mint_agent_token(
            agent_uuid=record.agent_uuid,
            organisation=resolved_organisation,
        )
    except HubGatewayError as exc:
        username = gateway.require_username()
        scope_type, scope_value = _scope(resolved_organisation, username)
        if exc.status != 403 or re_enroll:
            raise

        echo("Stored agent enrollment was rejected. Re-enrolling once.")
        state.clear_installation(scope_type=scope_type, scope_value=scope_value)
        record = _ensure_installation(
            gateway,
            state,
            keypair,
            organisation=resolved_organisation,
            re_enroll=False,
            echo=echo,
        )
        minted = gateway.mint_agent_token(
            agent_uuid=record.agent_uuid,
            organisation=resolved_organisation,
        )

    if discover_alpaca:
        try:
            _discover_and_register_alpaca(
                gateway,
                state,
                organisation=resolved_organisation,
                echo=echo,
            )
        except Exception as exc:  # pragma: no cover - best effort path
            echo(f"ASCOM discovery skipped: {exc}")

    resolved_workspace_id = workspace_id or state.workspace_id
    echo(
        f"Starting relay as agent {record.agent_uuid} "
        f"({context_label(resolved_organisation)}) on workspace "
        f"{resolved_workspace_id}."
    )

    welcome = asyncio.run(
        HubClient(
            hub_url=resolved_hub_url,
            workspace_id=resolved_workspace_id,
            agent_uuid=record.agent_uuid,
            jwt=minted.jwt,
            keypair=keypair,
            command_handlers=default_command_handlers(),
            traffic_callback=traffic_callback,
            stop_event=stop_event,
        ).connect(once=False, echo=echo)
    )

    return {
        "ok": True,
        "context": context_label(resolved_organisation),
        "hub_url": resolved_hub_url,
        "workspace_id": resolved_workspace_id,
        "agent_uuid": record.agent_uuid,
        "session_id": welcome.session_id,
        "keepalive_s": welcome.keepalive_s,
        "stopped": bool(stop_event and stop_event.is_set()),
    }


def doctor(
    *,
    api_server: str | None,
    hub_url: str | None,
    organisation: str | None,
    workspace_id: str | None,
    force_refresh: bool,
    re_enroll: bool,
    echo: Logger,
) -> dict[str, Any]:
    state: FocaleState | None = None
    gateway: HubGateway | None = None
    keypair: AgentKeypair | None = None
    record: InstallationRecord | None = None
    resolved_hub_url: str | None = None
    resolved_organisation: str | None = None
    minted = None
    had_failure = False
    results: list[dict[str, object]] = []

    def report(label: str, ok: bool, detail: str, **extra: object) -> None:
        results.append({"label": label, "ok": ok, "detail": detail, **extra})
        status = "OK" if ok else "FAIL"
        echo(f"[{status}] {label}: {detail}")

    try:
        state = FocaleState.load()
        gateway = _gateway(state=state, api_server=api_server)
        resolved_organisation = resolve_context_organisation(state, organisation)
        report("state", True, f"workspace_id={state.workspace_id}", workspace_id=state.workspace_id)
        report("context", True, context_label(resolved_organisation))
    except FocaleError as exc:
        report("state", False, str(exc))
        return {
            "ok": False,
            "api_server": api_server,
            "hub_url": hub_url,
            "steps": results,
        }

    try:
        gateway.require_login()
        report(
            "login",
            True,
            f"username={gateway.username} auth_type={gateway.auth_type}",
            username=gateway.username,
            auth_type=gateway.auth_type,
        )
    except FocaleError as exc:
        report("login", False, str(exc))
        return {
            "ok": False,
            "api_server": gateway.api_server if gateway else api_server,
            "hub_url": hub_url,
            "steps": results,
        }

    try:
        if force_refresh and gateway.auth_type == "token":
            gateway.refresh_access_token()
            report("refresh", True, "forced refresh succeeded")
        else:
            gateway.ensure_authenticated()
            detail = (
                "JWT is valid or refreshed automatically"
                if gateway.auth_type == "token"
                else "authenticated"
            )
            report("refresh", True, detail)
    except FocaleError as exc:
        report("refresh", False, str(exc))
        had_failure = True

    try:
        keypair = AgentKeypair.load_or_create(state.private_key_file())
        report(
            "keypair",
            True,
            f"public_key_b64_prefix={keypair.public_key_b64[:12]}",
            public_key_b64_prefix=keypair.public_key_b64[:12],
        )
    except FocaleError as exc:
        report("keypair", False, str(exc))
        had_failure = True

    try:
        resolved_hub_url = resolve_hub_url(state, hub_url)
        if resolved_hub_url != state.hub_url:
            state.hub_url = resolved_hub_url
            state.save()
        report("hub-url", True, resolved_hub_url, hub_url=resolved_hub_url)
    except FocaleError as exc:
        report("hub-url", False, str(exc))
        had_failure = True

    if not had_failure and gateway and state and keypair:
        try:
            record = _ensure_installation(
                gateway,
                state,
                keypair,
                organisation=resolved_organisation,
                re_enroll=re_enroll,
                echo=lambda _message: None,
            )
            report("enroll", True, f"agent_uuid={record.agent_uuid}", agent_uuid=record.agent_uuid)
        except FocaleError as exc:
            report("enroll", False, str(exc))
            had_failure = True

    if not had_failure and gateway and record:
        try:
            minted = gateway.mint_agent_token(
                agent_uuid=record.agent_uuid,
                organisation=resolved_organisation,
            )
            report("mint", True, f"exp={minted.exp}", exp=minted.exp)
        except FocaleError as exc:
            report("mint", False, str(exc))
            had_failure = True

    if not had_failure and keypair and record and resolved_hub_url and gateway and state:
        try:
            welcome = asyncio.run(
                HubClient(
                    hub_url=resolved_hub_url,
                    workspace_id=workspace_id or state.workspace_id,
                    agent_uuid=record.agent_uuid,
                    jwt=minted.jwt,
                    keypair=keypair,
                ).connect(once=True, echo=echo)
            )
            report(
                "hub",
                True,
                f"session_id={welcome.session_id} keepalive_s={welcome.keepalive_s}",
                session_id=welcome.session_id,
                keepalive_s=welcome.keepalive_s,
            )
        except FocaleError as exc:
            report("hub", False, str(exc))
            had_failure = True

    return {
        "ok": not had_failure,
        "api_server": gateway.api_server if gateway else api_server,
        "hub_url": resolved_hub_url or hub_url,
        "context": context_label(resolved_organisation),
        "steps": results,
    }


def platesolver_status(
    *,
    cache_dir: str | None,
    scales: str,
) -> dict[str, Any]:
    solver = PlateSolverClient(
        cache_dir=cache_dir,
        scales=parse_scales(scales),
    )
    try:
        health = solver.health()
    finally:
        solver.close()
    return {"mode": "local", "health": health}


def center_on_coordinates(
    *,
    camera_address: str,
    camera_number: int,
    telescope_address: str,
    telescope_number: int,
    target_ra_hours: float,
    target_dec_deg: float,
    cache_dir: str | None,
    scales: str,
    duration: float,
    max_iterations: int,
    min_peaks: int,
    success_threshold: float,
    failure_threshold: float,
    max_duration_adjustments: int,
    echo: Logger,
) -> dict[str, Any]:
    loop = CenteringLoop(
        camera_address=camera_address,
        camera_number=camera_number,
        telescope_address=telescope_address,
        telescope_number=telescope_number,
        target_ra_hours=target_ra_hours,
        target_dec_deg=target_dec_deg,
        cache_dir=cache_dir,
        scales=parse_scales(scales),
        duration=duration,
        max_iterations=max_iterations,
        min_peaks=min_peaks,
        success_threshold=success_threshold,
        failure_threshold=failure_threshold,
        max_duration_adjustments=max_duration_adjustments,
    )
    return loop.run(echo).to_dict()


# ------------------------------------------------------------------ #
# Centering config persistence                                         #
# ------------------------------------------------------------------ #

def get_centering_config() -> dict[str, Any]:
    """Return the persisted centering configuration as a plain dict."""
    state = FocaleState.load()
    return asdict(state.centering)


def save_centering_config(
    *,
    duration: float,
    max_iterations: int,
    min_peaks: int,
    success_threshold: float,
    failure_threshold: float,
    max_duration_adjustments: int,
    cache_dir: str | None,
) -> dict[str, Any]:
    """Persist centering parameters to state and return the saved values."""
    state = FocaleState.load()
    state.centering = CenteringConfig(
        duration=duration,
        max_iterations=max_iterations,
        min_peaks=min_peaks,
        success_threshold=success_threshold,
        failure_threshold=failure_threshold,
        max_duration_adjustments=max_duration_adjustments,
        cache_dir=cache_dir,
    )
    state.save()
    return asdict(state.centering)


# ------------------------------------------------------------------ #
# Hub command handlers                                                 #
# ------------------------------------------------------------------ #

def _find_alpaca_device(
    state: FocaleState,
    device_type: str,
) -> tuple[str, int] | None:
    """
    Return (address, device_number) for the first registered Alpaca server
    that exposes a device of the requested type.
    """
    for _key, server_record in state.alpaca_servers.items():
        try:
            devices = get_configured_devices(server_record.address)
        except FocaleError:
            continue
        for device in devices:
            if device.type.lower() == device_type.lower():
                return server_record.address, device.number
    return None


def handle_center_on_coordinates(
    payload: dict[str, Any],
    echo: Logger,
) -> dict[str, Any]:
    """
    Hub command handler for ``center_on_coordinates``.

    The Hub payload must provide ``target_ra_hours`` (or ``ra_hours``) and
    ``target_dec_deg`` (or ``dec_deg``).  Camera / telescope are resolved from
    the locally registered Alpaca servers.  All other parameters default to the
    persisted centering configuration.
    """
    state = FocaleState.load()
    config = state.centering

    camera = _find_alpaca_device(state, "Camera")
    if camera is None:
        raise FocaleError(
            "No Camera device found in registered Alpaca servers. "
            "Please register your Alpaca server first."
        )
    telescope = _find_alpaca_device(state, "Telescope")
    if telescope is None:
        raise FocaleError(
            "No Telescope device found in registered Alpaca servers. "
            "Please register your Alpaca server first."
        )

    camera_address, camera_number = camera
    telescope_address, telescope_number = telescope

    try:
        target_ra_hours = float(
            payload.get("target_ra_hours") or payload.get("ra_hours") or 0.0
        )
        target_dec_deg = float(
            payload.get("target_dec_deg") or payload.get("dec_deg") or 0.0
        )
    except (TypeError, ValueError) as exc:
        raise FocaleError(f"Invalid RA/Dec in command payload: {exc}") from exc

    scales_str = str(payload.get("scales") or "6")

    echo(
        f"Centering on RA={target_ra_hours:.4f}h Dec={target_dec_deg:.4f}° "
        f"using {camera_address} (cam #{camera_number}) "
        f"and {telescope_address} (tel #{telescope_number})."
    )

    return center_on_coordinates(
        camera_address=camera_address,
        camera_number=camera_number,
        telescope_address=telescope_address,
        telescope_number=telescope_number,
        target_ra_hours=target_ra_hours,
        target_dec_deg=target_dec_deg,
        cache_dir=config.cache_dir,
        scales=scales_str,
        duration=config.duration,
        max_iterations=config.max_iterations,
        min_peaks=config.min_peaks,
        success_threshold=config.success_threshold,
        failure_threshold=config.failure_threshold,
        max_duration_adjustments=config.max_duration_adjustments,
        echo=echo,
    )


def default_command_handlers() -> dict[str, Any]:
    """Return the default mapping of Hub command names to local handler functions."""
    return {
        "center_on_coordinates": handle_center_on_coordinates,
    }
