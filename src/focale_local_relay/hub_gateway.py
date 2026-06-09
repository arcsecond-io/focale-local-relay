from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .exceptions import HubGatewayError
from .state import AuthSession, FocaleState


@dataclass(frozen=True)
class MintedHubToken:
    jwt: str
    exp: int


@dataclass(frozen=True)
class OrganisationContext:
    subdomain: str
    name: str
    role: str | None = None


class HubGateway:
    CLIENT_TYPE = "desktop"
    DEFAULT_API_SERVER = "https://api.focale.space"

    def __init__(
        self,
        *,
        state: FocaleState,
        api_server: str | None = None,
    ) -> None:
        self.state = state
        self.api_server = (api_server or state.api_server or self.DEFAULT_API_SERVER).rstrip("/")
        if not self.api_server:
            raise HubGatewayError("Unable to resolve a Focale API server.", 400)

    @property
    def username(self) -> str:
        if self.state.auth:
            return self.state.auth.username
        return ""

    @property
    def is_logged_in(self) -> bool:
        return self.state.auth is not None

    @property
    def auth_type(self) -> str | None:
        if self.state.auth:
            return self.state.auth.auth_type
        return None

    @property
    def has_refresh_token(self) -> bool:
        return bool(self.state.auth and self.state.auth.refresh_token)

    def login_with_password(self, *, username: str, password: str) -> None:
        payload = self._request_dict(
            "post",
            "auth/token",
            json={
                "username": username,
                "password": password,
                "client": self.CLIENT_TYPE,
            },
            headers={"X-Focale-Client": self.CLIENT_TYPE},
            authenticated=False,
        )
        access_token = payload.get("access")
        refresh_token = payload.get("refresh")
        response_username = payload.get("username") or username
        if not access_token or not refresh_token:
            raise HubGatewayError("Focale did not return access and refresh tokens.", 500)

        self.state.auth = AuthSession(
            username=response_username,
            access_token=access_token,
            access_exp=payload.get("access_exp"),
            refresh_token=refresh_token,
            refresh_exp=payload.get("refresh_exp"),
            auth_type="token",
        )
        self.state.save()

    def refresh_access_token(self) -> None:
        session = self.require_auth_session()
        if session.auth_type != "token" or not session.refresh_token:
            raise HubGatewayError("No refreshable JWT session is available.", 401)

        now = int(time.time())
        if session.refresh_exp and now >= int(session.refresh_exp):
            self._clear_auth_session()
            raise HubGatewayError(
                "Your Focale session expired or was revoked. Sign in again.",
                401,
            )

        try:
            payload = self._request_dict(
                "post",
                "auth/token/refresh",
                json={"refresh": session.refresh_token},
                authenticated=False,
                retry_on_401=False,
            )
        except HubGatewayError as exc:
            if exc.status == 401 or self._is_invalid_refresh_error(exc):
                self._clear_auth_session()
                raise HubGatewayError(
                    "Your Focale session expired or was revoked. Sign in again.",
                    401,
                ) from exc
            raise
        access_token = payload.get("access")
        refresh_token = payload.get("refresh")
        if not access_token or not refresh_token:
            raise HubGatewayError("Focale returned an incomplete refresh response.", 500)

        self.state.auth = AuthSession(
            username=payload.get("username") or session.username,
            access_token=access_token,
            access_exp=payload.get("access_exp"),
            refresh_token=refresh_token,
            refresh_exp=payload.get("refresh_exp"),
            auth_type="token",
            created_at=session.created_at,
        )
        self.state.save()

    def _clear_auth_session(self) -> None:
        if self.state.auth is None:
            return
        self.state.auth = None
        self.state.save()

    @staticmethod
    def _is_invalid_refresh_error(exc: HubGatewayError) -> bool:
        message = str(exc)
        return exc.status == 400 and "Invalid or expired refresh token." in message

    def enroll_agent(self, *, public_key_b64: str, organisation: str | None = None) -> str:
        payload = {"public_key_b64": public_key_b64}
        if organisation:
            payload["organisation"] = organisation
        else:
            payload["profile"] = self.require_username()

        response = self._request_dict("post", self._scope_path("agent/enroll", organisation), json=payload)
        agent_uuid = response.get("uuid")
        if not agent_uuid:
            raise HubGatewayError("Focale did not return an agent uuid.", 500)
        return agent_uuid

    def mint_agent_token(
        self, *, agent_uuid: str, organisation: str | None = None
    ) -> MintedHubToken:
        payload = {"agent_uuid": agent_uuid}
        if organisation:
            payload["organisation"] = organisation
        else:
            payload["profile"] = self.require_username()

        response = self._request_dict("post", self._scope_path("agent/mint", organisation), json=payload)
        jwt_token = response.get("jwt")
        exp = response.get("exp")
        if not jwt_token or exp is None:
            raise HubGatewayError("Focale returned an incomplete Hub token response.", 500)
        return MintedHubToken(jwt=jwt_token, exp=int(exp))

    def require_login(self) -> None:
        if not self.is_logged_in:
            raise HubGatewayError("No Focale login was found. Run `focale login` first.", 401)

    def require_username(self) -> str:
        self.require_login()
        username = self.username
        if not username:
            raise HubGatewayError(
                "The stored Focale session is missing its username. Login again with `focale login`.",
                400,
            )
        return username

    def require_auth_session(self) -> AuthSession:
        if self.state.auth is not None:
            if self.state.auth.auth_type != "token":
                self._clear_auth_session()
                raise HubGatewayError(
                    "Focale only supports JWT sessions. Sign in again.",
                    401,
                )
            return self.state.auth
        raise HubGatewayError("No Focale credentials are available. Run `focale login` first.", 401)

    def ensure_authenticated(self) -> None:
        session = self.require_auth_session()
        now = int(time.time())
        access_exp = int(session.access_exp or 0)
        if access_exp and now < access_exp - 30:
            return
        self.refresh_access_token()

    def list_organisation_contexts(self) -> list[OrganisationContext]:
        profile = self._request_dict("get", self._scope_path(f"profiles/{self.require_username()}", None))
        memberships = profile.get("memberships") or []
        if not isinstance(memberships, list):
            raise HubGatewayError("Unexpected Focale profile memberships payload.", 500)

        seen: set[str] = set()
        contexts: list[OrganisationContext] = []
        for membership in memberships:
            if not isinstance(membership, dict):
                continue
            org_data = membership.get("organisation")
            if not isinstance(org_data, dict):
                continue
            subdomain = str(org_data.get("subdomain") or "").strip()
            if not subdomain or subdomain in seen:
                continue
            seen.add(subdomain)
            contexts.append(
                OrganisationContext(
                    subdomain=subdomain,
                    name=str(org_data.get("name") or subdomain),
                    role=membership.get("role"),
                )
            )
        return sorted(contexts, key=lambda item: item.subdomain)

    def list_alpaca_servers(self, *, organisation: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] | None = None
        if not organisation:
            params = {"profile": self.require_username()}

        payload = self._request(
            "get",
            self._scope_path("alpacaservers", organisation),
            params=params,
        )
        if not isinstance(payload, list):
            raise HubGatewayError("Unexpected Focale alpacaservers payload.", 500)

        servers: list[dict[str, Any]] = []
        for row in payload:
            if isinstance(row, dict):
                servers.append(row)
        return servers

    def create_alpaca_server(
        self,
        *,
        name: str,
        address: str,
        manufacturer: str | None = None,
        organisation: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "address": address,
        }
        if manufacturer:
            payload["manufacturer"] = manufacturer
        if organisation:
            payload["organisation"] = organisation
        else:
            payload["profile"] = self.require_username()
        return self._request_dict(
            "post",
            self._scope_path("alpacaservers", organisation),
            json=payload,
        )

    def list_alpaca_devices(
        self,
        *,
        server_uuid: str | None = None,
        organisation: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if server_uuid:
            params["server"] = server_uuid

        payload = self._request(
            "get",
            self._scope_path("alpacadevices", organisation),
            params=params or None,
        )
        if not isinstance(payload, list):
            raise HubGatewayError("Unexpected Focale alpacadevices payload.", 500)
        return [row for row in payload if isinstance(row, dict)]

    def create_alpaca_device(
        self,
        *,
        server_uuid: str,
        name: str,
        number: int,
        unique_id: str,
        device_type: str,
        organisation: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "server": server_uuid,
            "name": name,
            "number": number,
            "unique_id": unique_id,
            "type": device_type,
        }
        return self._request_dict(
            "post",
            self._scope_path("alpacadevices", organisation),
            json=payload,
        )

    def list_observing_sites(self, *, organisation: str | None = None) -> list[dict[str, Any]]:
        payload = self._request("get", self._scope_path("observingsites", organisation))
        if not isinstance(payload, list):
            raise HubGatewayError("Unexpected Focale observing sites payload.", 500)
        return [row for row in payload if isinstance(row, dict)]

    def create_observing_site(
        self,
        *,
        name: str,
        longitude: float,
        latitude: float,
        height: float | None = None,
        organisation: str | None = None,
    ) -> dict[str, Any]:
        coordinates: dict[str, Any] = {
            "longitude": longitude,
            "latitude": latitude,
        }
        if height is not None:
            coordinates["height"] = height

        payload: dict[str, Any] = {
            "name": name,
            "coordinates": coordinates,
        }
        if organisation:
            payload["organisation"] = organisation

        return self._request_dict(
            "post",
            self._scope_path("observingsites", organisation),
            json=payload,
        )

    def update_observing_site(
        self,
        *,
        site_uuid: str,
        payload: dict[str, Any],
        organisation: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "patch",
            self._scope_path(f"observingsites/{site_uuid}", organisation),
            json=payload,
        )

    def list_telescopes(self, *, organisation: str | None = None) -> list[dict[str, Any]]:
        payload = self._request("get", self._scope_path("telescopes", organisation))
        if not isinstance(payload, list):
            raise HubGatewayError("Unexpected Focale telescopes payload.", 500)
        return [row for row in payload if isinstance(row, dict)]

    def create_telescope(
        self,
        *,
        name: str,
        observing_site: str,
        device_id: int | None = None,
        organisation: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "observing_site": observing_site,
        }
        if device_id is not None:
            payload["device"] = device_id
        if organisation:
            payload["organisation"] = organisation

        return self._request_dict(
            "post",
            self._scope_path("telescopes", organisation),
            json=payload,
        )

    def update_telescope(
        self,
        *,
        telescope_uuid: str,
        payload: dict[str, Any],
        organisation: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "patch",
            self._scope_path(f"telescopes/{telescope_uuid}", organisation),
            json=payload,
        )

    def list_equipment(
        self,
        *,
        equipment_path: str,
        organisation: str | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._request("get", self._scope_path(equipment_path, organisation))
        if not isinstance(payload, list):
            raise HubGatewayError(
                f"Unexpected Focale {equipment_path} payload.",
                500,
            )
        return [row for row in payload if isinstance(row, dict)]

    def create_equipment(
        self,
        *,
        equipment_path: str,
        payload: dict[str, Any],
        organisation: str | None = None,
    ) -> dict[str, Any]:
        request_payload = dict(payload)
        if organisation:
            request_payload["organisation"] = organisation
        return self._request_dict(
            "post",
            self._scope_path(equipment_path, organisation),
            json=request_payload,
        )

    def update_equipment(
        self,
        *,
        equipment_path: str,
        equipment_uuid: str,
        payload: dict[str, Any],
        organisation: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "patch",
            self._scope_path(f"{equipment_path}/{equipment_uuid}", organisation),
            json=payload,
        )

    def _scope_path(self, path: str, organisation: str | None) -> str:
        if organisation:
            return f"{organisation}/{path}"
        return path

    def _auth_headers(self) -> dict[str, str]:
        session = self.require_auth_session()
        if session.auth_type == "token":
            return {"Authorization": f"Bearer {session.access_token}"}
        return {"X-Focale-API-Authorization": f"Key {session.access_token}"}

    def _request_dict(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        retry_on_401: bool = True,
    ) -> dict[str, Any]:
        payload = self._request(
            method,
            path,
            json=json,
            params=params,
            headers=headers,
            authenticated=authenticated,
            retry_on_401=retry_on_401,
        )
        if not isinstance(payload, dict):
            raise HubGatewayError(
                f"Unexpected Focale response type: {type(payload)!r}.",
                500,
            )
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        retry_on_401: bool = True,
    ) -> Any:
        request_headers = dict(headers or {})
        if authenticated:
            self.ensure_authenticated()
            request_headers.update(self._auth_headers())

        url = f"{self.api_server}/{path.strip('/')}/"
        try:
            response = httpx.request(
                method.upper(),
                url,
                json=json,
                params=params,
                headers=request_headers,
                timeout=30,
            )
        except httpx.RequestError as exc:
            raise HubGatewayError(str(exc), 400) from exc

        if response.status_code == 401 and authenticated and retry_on_401:
            session = self.require_auth_session()
            if session.auth_type == "token" and session.refresh_token:
                self.refresh_access_token()
                return self._request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers=headers,
                    authenticated=authenticated,
                    retry_on_401=False,
                )

        if not (200 <= response.status_code < 300):
            raise HubGatewayError(response.text or f"HTTP {response.status_code}", response.status_code)

        try:
            data = response.json() if response.text else {}
        except ValueError as exc:
            raise HubGatewayError(
                f"Focale returned invalid JSON for {path}: {exc}",
                response.status_code,
            ) from exc

        return data
