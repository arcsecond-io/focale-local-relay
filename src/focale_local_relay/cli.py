from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from . import __version__
from . import services as _services
from .agent_auth import AgentKeypair
from .alpaca import discover_alpaca_servers, normalize_alpaca_address
from .hub_gateway import HubGateway
from .exceptions import FocaleError, HubGatewayError
from .hub import HubClient
from .platesolver import PlateSolverClient
from .state import AlpacaServerRecord, FocaleState, InstallationRecord


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RuntimeOptions:
    api_server: str | None


pass_options = click.make_pass_decorator(RuntimeOptions, ensure=True)


def _scope(organisation: str | None, username: str) -> tuple[str, str]:
    if organisation:
        return "organisation", organisation
    return "profile", username


def _context_label(organisation: str | None) -> str:
    if organisation:
        return f"organisation `{organisation}`"
    return "personal"


def _resolve_context_organisation(
    state: FocaleState,
    organisation: str | None,
) -> str | None:
    if organisation is not None:
        return organisation.strip() or None
    if state.default_organisation:
        return state.default_organisation
    return None


def _resolve_hub_url(state: FocaleState, hub_url: str | None) -> str:
    resolved = hub_url or state.hub_url
    if not resolved:
        raise click.ClickException(
            "No Hub URL is configured. Pass `--hub-url` the first time you connect."
        )
    return resolved


def _result_line(label: str, ok: bool, detail: str) -> None:
    status = "OK" if ok else "FAIL"
    click.echo(f"[{status}] {label}: {detail}")


def _ensure_installation(
    gateway: HubGateway,
    state: FocaleState,
    keypair: AgentKeypair,
    *,
    organisation: str | None,
    re_enroll: bool,
    echo,
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
    echo,
) -> None:
    discovered = discover_alpaca_servers()
    if not discovered:
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


def _parse_scales(scales: str) -> list[int]:
    values: list[int] = []
    for part in scales.split(","):
        raw = part.strip()
        if not raw:
            continue
        try:
            values.append(int(raw))
        except ValueError as exc:
            raise click.ClickException(
                f"Invalid scale value `{raw}`. Use comma-separated integers, e.g. 6 or 5,6."
            ) from exc
    if not values:
        raise click.ClickException("At least one scale must be provided.")
    return values


def _load_peaks_file(path: Path) -> list[list[float]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Unable to read peaks file `{path}`: {exc}") from exc

    peaks = payload.get("peaks_xy") if isinstance(payload, dict) else payload
    if not isinstance(peaks, list):
        raise click.ClickException("Peaks payload must be a list or an object with `peaks_xy`.")

    parsed: list[list[float]] = []
    for row in peaks:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise click.ClickException("Each peak must be a two-value [x, y] array.")
        try:
            parsed.append([float(row[0]), float(row[1])])
        except (TypeError, ValueError) as exc:
            raise click.ClickException("Each peak coordinate must be numeric.") from exc
    return parsed


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--api-server",
    help="Override the Focale API base URL, for example https://api.focale.dev.",
)
@click.version_option(version=__version__)
@click.pass_context
def main(ctx: click.Context, api_server: str | None) -> None:
    ctx.obj = RuntimeOptions(api_server=api_server)


@main.command(help="Login to Focale with password/JWT.")
@click.option("--username", prompt=True, help="Focale username (without @).")
@pass_options
def login(options: RuntimeOptions, username: str) -> None:
    try:
        state = FocaleState.load()
        gateway = HubGateway(
            state=state,
            api_server=options.api_server,
        )
        password = click.prompt("Focale password", hide_input=True)
        gateway.login_with_password(username=username, password=password)
    except HubGatewayError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Focale login saved for {gateway.username}. "
        f"API: {gateway.api_server}"
    )


@main.command(help="Show the current Focale session status.")
@pass_options
def status(options: RuntimeOptions) -> None:
    try:
        state = FocaleState.load()
        gateway = HubGateway(
            state=state,
            api_server=options.api_server,
        )
    except FocaleError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Focale version: {__version__}")
    click.echo(f"Focale api server: {gateway.api_server}")
    click.echo(f"Logged in: {'yes' if gateway.is_logged_in else 'no'}")
    click.echo(f"Username: {gateway.username or '(none)'}")
    click.echo(f"Auth type: {gateway.auth_type or '(none)'}")
    click.echo(f"Refresh token available: {'yes' if gateway.has_refresh_token else 'no'}")
    click.echo(f"Focale state: {state.state_file()}")
    click.echo(f"Workspace id: {state.workspace_id}")
    click.echo(f"Default hub url: {state.hub_url or '(none)'}")
    click.echo(f"Default context: {_context_label(state.default_organisation)}")
    click.echo(f"Known ASCOM servers in state: {len(state.alpaca_servers)}")

    if state.installations:
        click.echo("Agent installations:")
        for key, record in sorted(state.installations.items()):
            click.echo(
                f"  - {key}: agent_uuid={record.agent_uuid} created_at={record.created_at}"
            )
    else:
        click.echo("Agent installations: none")


@main.group(help="Manage personal vs organisation context.")
def context() -> None:
    return None


@context.command("show", help="Show the default context used by connect/doctor.")
@pass_options
def context_show(options: RuntimeOptions) -> None:
    try:
        state = FocaleState.load()
        gateway = HubGateway(
            state=state,
            api_server=options.api_server,
        )
    except FocaleError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Default context: {_context_label(state.default_organisation)}")
    click.echo(f"User: {gateway.username or '(unknown)'}")


@context.command("list", help="List all available contexts for the logged-in user.")
@pass_options
def context_list(options: RuntimeOptions) -> None:
    try:
        state = FocaleState.load()
        gateway = HubGateway(
            state=state,
            api_server=options.api_server,
        )
        gateway.ensure_authenticated()
        contexts = gateway.list_organisation_contexts()
    except FocaleError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"* personal ({gateway.require_username()})")
    for item in contexts:
        role = f", role={item.role}" if item.role else ""
        click.echo(f"* {item.subdomain} ({item.name}{role})")
    click.echo(f"Current default: {_context_label(state.default_organisation)}")


@context.command("use", help="Set default context: `personal` or an organisation subdomain.")
@click.argument("target")
@click.option(
    "--force",
    is_flag=True,
    help="Allow an organisation subdomain that is not returned by the memberships API.",
)
@pass_options
def context_use(options: RuntimeOptions, target: str, force: bool) -> None:
    normalized = target.strip()
    if not normalized:
        raise click.ClickException("Context target cannot be empty.")

    try:
        state = FocaleState.load()
        gateway = HubGateway(
            state=state,
            api_server=options.api_server,
        )

        if normalized.lower() in {"personal", "profile", "me"}:
            state.default_organisation = None
            state.save()
            click.echo("Default context set to personal.")
            return

        if not force:
            gateway.ensure_authenticated()
            memberships = {item.subdomain for item in gateway.list_organisation_contexts()}
            if normalized not in memberships:
                raise click.ClickException(
                    f"`{normalized}` is not listed in your memberships. "
                    "Use `focale context list` or pass `--force`."
                )

        state.default_organisation = normalized
        state.save()
        click.echo(f"Default context set to organisation `{normalized}`.")
    except FocaleError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command(help="Connect to the Focale Hub using the secured challenge flow.")
@click.option("--hub-url", help="Hub websocket URL, for example wss://hub.focale.space/ws/agent.")
@click.option(
    "--organisation",
    help="Organisation subdomain override. If omitted, the saved default context is used.",
)
@click.option(
    "--workspace-id",
    help="Optional override for the local workspace identifier. Defaults to the saved local id.",
)
@click.option(
    "--once",
    is_flag=True,
    help="Validate the login, enrollment, JWT minting, and Hub handshake, then exit.",
)
@click.option(
    "--re-enroll",
    is_flag=True,
    help="Force a fresh Focale agent enrollment before connecting.",
)
@click.option(
    "--discover-alpaca/--no-discover-alpaca",
    default=True,
    show_default=True,
    help="Automatically discover and register local ASCOM Remote servers.",
)
@pass_options
def connect(
    options: RuntimeOptions,
    hub_url: str | None,
    organisation: str | None,
    workspace_id: str | None,
    once: bool,
    re_enroll: bool,
    discover_alpaca: bool,
) -> None:
    try:
        state = FocaleState.load()
        gateway = HubGateway(
            state=state,
            api_server=options.api_server,
        )
        keypair = AgentKeypair.load_or_create(state.private_key_file())
        gateway.ensure_authenticated()
        resolved_organisation = _resolve_context_organisation(state, organisation)
        resolved_hub_url = _resolve_hub_url(state, hub_url)
        if hub_url and hub_url != state.hub_url:
            state.hub_url = hub_url
            state.save()

        record = _ensure_installation(
            gateway,
            state,
            keypair,
            organisation=resolved_organisation,
            re_enroll=re_enroll,
            echo=click.echo,
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

            click.echo("Stored agent enrollment was rejected. Re-enrolling once.")
            state.clear_installation(scope_type=scope_type, scope_value=scope_value)
            record = _ensure_installation(
                gateway,
                state,
                keypair,
                organisation=resolved_organisation,
                re_enroll=False,
                echo=click.echo,
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
                    echo=click.echo,
                )
            except Exception as exc:  # pragma: no cover - best effort path
                click.echo(f"ASCOM discovery skipped: {exc}")

        click.echo(
            f"Connecting to Hub as agent {record.agent_uuid} "
            f"({ _context_label(resolved_organisation) }) on workspace "
            f"{workspace_id or state.workspace_id}."
        )
        welcome = asyncio.run(
            HubClient(
                hub_url=resolved_hub_url,
                workspace_id=workspace_id or state.workspace_id,
                agent_uuid=record.agent_uuid,
                jwt=minted.jwt,
                keypair=keypair,
                command_handlers=_services.default_command_handlers(),
            ).connect(once=once, echo=click.echo)
        )
    except FocaleError as exc:
        raise click.ClickException(str(exc)) from exc

    if once:
        click.echo(
            f"Hub handshake succeeded. session_id={welcome.session_id} keepalive_s={welcome.keepalive_s}"
        )


@main.command(help="Run a step-by-step diagnostic for Focale auth and Hub connectivity.")
@click.option("--hub-url", help="Hub websocket URL, for example wss://hub.focale.space/ws/agent.")
@click.option(
    "--organisation",
    help="Organisation subdomain override. If omitted, the saved default context is used.",
)
@click.option(
    "--workspace-id",
    help="Optional override for the local workspace identifier. Defaults to the saved local id.",
)
@click.option(
    "--force-refresh",
    is_flag=True,
    help="Force a JWT refresh before the rest of the checks.",
)
@click.option(
    "--re-enroll",
    is_flag=True,
    help="Force a new agent enrollment before minting the Hub JWT.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable JSON instead of human-readable lines.",
)
@pass_options
def doctor(
    options: RuntimeOptions,
    hub_url: str | None,
    organisation: str | None,
    workspace_id: str | None,
    force_refresh: bool,
    re_enroll: bool,
    json_output: bool,
) -> None:
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
        item = {"label": label, "ok": ok, "detail": detail, **extra}
        results.append(item)
        if not json_output:
            _result_line(label, ok, detail)

    try:
        state = FocaleState.load()
        gateway = HubGateway(
            state=state,
            api_server=options.api_server,
        )
        resolved_organisation = _resolve_context_organisation(state, organisation)
        report("state", True, f"workspace_id={state.workspace_id}", workspace_id=state.workspace_id)
        report("context", True, _context_label(resolved_organisation))
    except FocaleError as exc:
        report("state", False, str(exc))
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "ok": False,
                        "api_server": options.api_server,
                        "hub_url": hub_url,
                        "steps": results,
                    },
                    indent=2,
                )
            )
            raise SystemExit(1) from exc
        raise click.ClickException("Doctor failed at state initialization.") from exc

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
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "ok": False,
                        "api_server": gateway.api_server if gateway else options.api_server,
                        "hub_url": hub_url,
                        "steps": results,
                    },
                    indent=2,
                )
            )
            raise SystemExit(1) from exc
        raise click.ClickException("Doctor failed because no usable Focale login is available.") from exc

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
        resolved_hub_url = _resolve_hub_url(state, hub_url)
        if hub_url and hub_url != state.hub_url:
            state.hub_url = hub_url
            state.save()
        report("hub-url", True, resolved_hub_url, hub_url=resolved_hub_url)
    except click.ClickException as exc:
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
                echo=lambda message: None,
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
                ).connect(once=True, echo=click.echo)
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

    if json_output:
        click.echo(
            json.dumps(
                {
                    "ok": not had_failure,
                    "api_server": gateway.api_server if gateway else options.api_server,
                    "hub_url": resolved_hub_url or hub_url,
                    "context": _context_label(resolved_organisation),
                    "steps": results,
                },
                indent=2,
            )
        )
        raise SystemExit(1 if had_failure else 0)

    if had_failure:
        raise click.ClickException("Doctor found at least one failing step.")


@main.group(help="Run plate solving locally or through a remote plate solver service.")
def platesolver() -> None:
    return None


@platesolver.command("status", help="Check plate solver availability.")
@click.option(
    "--service-url",
    help="Remote solver base URL, for example http://127.0.0.1:8900.",
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Local cache directory for astrometry indexes (local mode only).",
)
@click.option(
    "--scales",
    default="6",
    show_default=True,
    help="Comma-separated astrometry scales.",
)
def platesolver_status(
    service_url: str | None,
    cache_dir: Path | None,
    scales: str,
) -> None:
    solver = PlateSolverClient(
        service_url=service_url,
        cache_dir=str(cache_dir) if cache_dir else None,
        scales=_parse_scales(scales),
    )
    try:
        health = solver.health()
    except FocaleError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        solver.close()

    click.echo(json.dumps({"mode": solver.mode, "health": health}, indent=2))


@platesolver.command("solve", help="Solve a plate from centroid peaks.")
@click.option(
    "--peaks-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="JSON file containing peaks as [[x,y],...] or {\"peaks_xy\": [[x,y],...]}",
)
@click.option(
    "--service-url",
    help="Remote solver base URL, for example http://127.0.0.1:8900.",
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Local cache directory for astrometry indexes (local mode only).",
)
@click.option(
    "--scales",
    default="6",
    show_default=True,
    help="Comma-separated astrometry scales.",
)
@click.option("--ra-deg", type=float, help="Optional RA hint in degrees.")
@click.option("--dec-deg", type=float, help="Optional Dec hint in degrees.")
@click.option("--radius-deg", type=float, help="Optional search radius hint in degrees.")
@click.option("--lower-arcsec-per-pixel", type=float, help="Optional lower scale bound.")
@click.option("--upper-arcsec-per-pixel", type=float, help="Optional upper scale bound.")
def platesolver_solve(
    peaks_file: Path,
    service_url: str | None,
    cache_dir: Path | None,
    scales: str,
    ra_deg: float | None,
    dec_deg: float | None,
    radius_deg: float | None,
    lower_arcsec_per_pixel: float | None,
    upper_arcsec_per_pixel: float | None,
) -> None:
    peaks_xy = _load_peaks_file(peaks_file)
    solver = PlateSolverClient(
        service_url=service_url,
        cache_dir=str(cache_dir) if cache_dir else None,
        scales=_parse_scales(scales),
    )
    try:
        result = solver.solve(
            peaks_xy=peaks_xy,
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            radius_deg=radius_deg,
            lower_arcsec_per_pixel=lower_arcsec_per_pixel,
            upper_arcsec_per_pixel=upper_arcsec_per_pixel,
        )
    except FocaleError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        solver.close()

    click.echo(json.dumps(result.to_dict(), indent=2))
