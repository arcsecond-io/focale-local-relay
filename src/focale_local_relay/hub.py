from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from threading import Event
from typing import Any, Callable

import websockets
from websockets.client import WebSocketClientProtocol

from .agent_auth import AgentKeypair
from .exceptions import HubProtocolError

CommandHandler = Callable[[dict[str, Any], Callable[[str], None]], Any]
TrafficCallback = Callable[[dict[str, Any]], None]


def frame(message_type: str, payload: dict | None = None) -> dict:
    return {
        "v": 1,
        "type": message_type,
        "id": uuid.uuid4().hex,
        "ts": int(time.time()),
        "payload": payload or {},
    }


@dataclass(frozen=True)
class HubWelcome:
    agent_uuid: str
    session_id: str
    keepalive_s: int


class HubClient:
    def __init__(
        self,
        *,
        hub_url: str,
        workspace_id: str,
        agent_uuid: str,
        jwt: str,
        keypair: AgentKeypair,
        command_handlers: dict[str, CommandHandler] | None = None,
        traffic_callback: TrafficCallback | None = None,
        stop_event: Event | None = None,
    ) -> None:
        self.hub_url = hub_url
        self.workspace_id = workspace_id
        self.agent_uuid = agent_uuid
        self.jwt = jwt
        self.keypair = keypair
        self._command_handlers = command_handlers or {}
        self._traffic_callback = traffic_callback
        self._stop_event = stop_event

    async def connect(self, *, once: bool = False, echo=print) -> HubWelcome:
        try:
            async with websockets.connect(
                self.hub_url,
                ping_interval=None,
                max_size=256 * 1024,
                open_timeout=20,
            ) as websocket:
                await self._send(
                    websocket,
                    "hello",
                    {
                        "agent_uuid": self.agent_uuid,
                        "workspace_id": self.workspace_id,
                        "jwt": self.jwt,
                    },
                )

                challenge = await self._recv(websocket)
                if challenge.get("type") != "challenge":
                    self._raise_for_error(challenge, "Expected a Hub challenge frame.")

                payload = challenge.get("payload") or {}
                nonce_b64 = payload.get("nonce")
                challenge_agent_uuid = payload.get("agent_uuid")
                if not nonce_b64 or not challenge_agent_uuid:
                    raise HubProtocolError("Hub challenge was missing nonce or agent_uuid.")

                await self._send(
                    websocket,
                    "challenge_response",
                    {
                        "agent_uuid": challenge_agent_uuid,
                        "nonce": nonce_b64,
                        "sig": self.keypair.sign_nonce(
                            agent_uuid=challenge_agent_uuid,
                            nonce_b64=nonce_b64,
                        ),
                    },
                )

                welcome_frame = await self._recv(websocket)
                if welcome_frame.get("type") != "welcome":
                    self._raise_for_error(welcome_frame, "Expected a Hub welcome frame.")

                welcome_payload = welcome_frame.get("payload") or {}
                welcome = HubWelcome(
                    agent_uuid=welcome_payload.get("agent_uuid", self.agent_uuid),
                    session_id=welcome_payload.get("session_id", ""),
                    keepalive_s=int(welcome_payload.get("keepalive_s", 0)),
                )

                if once:
                    return welcome

                echo(
                    "Hub session established. "
                    f"session_id={welcome.session_id} keepalive_s={welcome.keepalive_s}"
                )

                while True:
                    if self._stop_event is not None and self._stop_event.is_set():
                        await websocket.close(code=1000, reason="Relay stopped locally")
                        echo("Hub relay stopped.")
                        break

                    try:
                        message = await asyncio.wait_for(self._recv(websocket), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    message_type = message.get("type")
                    if message_type == "ping":
                        await self._send(websocket, "pong", {})
                        continue
                    if message_type == "error":
                        self._raise_for_error(message, "Hub returned an error frame.")
                    if message_type == "command":
                        asyncio.ensure_future(
                            self._dispatch_command(websocket, message, echo)
                        )
                        continue

                    echo(json.dumps(message, indent=2, sort_keys=True))

                return welcome
        except (OSError, websockets.InvalidHandshake, websockets.InvalidURI) as exc:
            raise HubProtocolError(f"Unable to connect to Hub at {self.hub_url}: {exc}") from exc

    async def _dispatch_command(
        self,
        websocket: WebSocketClientProtocol,
        message: dict,
        echo,
    ) -> None:
        """
        Run a Hub command frame in a thread-pool executor and stream progress
        back to the Hub as 'progress' frames, then send a final 'command_result'.
        """
        payload = message.get("payload") or {}
        command_name = str(payload.get("command") or payload.get("name") or "")
        correlation_id = message.get("id") or uuid.uuid4().hex

        handler = self._command_handlers.get(command_name)
        if handler is None:
            echo(f"[hub] Received unknown command: {command_name!r}")
            await self._send(websocket, "command_result", {
                "correlation_id": correlation_id,
                "ok": False,
                "error": f"Unknown command: {command_name!r}",
            })
            return

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def sync_echo(msg: str) -> None:
            if self._traffic_callback is not None:
                self._traffic_callback(
                    {
                        "direction": "local",
                        "channel": "relay",
                        "message_type": "progress",
                        "summary": msg,
                        "payload": {
                            "correlation_id": correlation_id,
                            "command": command_name,
                            "message": msg,
                        },
                    }
                )
            loop.call_soon_threadsafe(queue.put_nowait, msg)

        async def forward_progress() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    break
                echo(item)
                try:
                    await self._send(websocket, "progress", {
                        "correlation_id": correlation_id,
                        "message": item,
                    })
                except Exception:
                    pass

        fut = loop.run_in_executor(None, lambda: handler(payload, sync_echo))
        forward_task = asyncio.ensure_future(forward_progress())

        try:
            result = await fut
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, None)
            await forward_task
            await self._send(websocket, "command_result", {
                "correlation_id": correlation_id,
                "ok": False,
                "error": str(exc),
            })
            return

        loop.call_soon_threadsafe(queue.put_nowait, None)
        await forward_task
        await self._send(websocket, "command_result", {
            "correlation_id": correlation_id,
            "ok": True,
            "result": result if result is not None else {},
        })

    async def _send(
        self,
        websocket: WebSocketClientProtocol,
        message_type: str,
        payload: dict,
    ) -> None:
        framed = frame(message_type, payload)
        self._emit_traffic(
            direction="outgoing",
            channel="hub",
            message_type=message_type,
            payload=framed,
        )
        await websocket.send(json.dumps(framed, separators=(",", ":")))

    async def _recv(self, websocket: WebSocketClientProtocol) -> dict:
        try:
            raw = await websocket.recv()
        except websockets.ConnectionClosed as exc:
            raise HubProtocolError(
                f"Hub connection closed unexpectedly: code={exc.code} reason={exc.reason}"
            ) from exc

        if not isinstance(raw, str):
            raise HubProtocolError("Hub returned a non-text websocket frame.")

        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HubProtocolError(f"Hub returned invalid JSON: {exc}") from exc

        if not isinstance(message, dict):
            raise HubProtocolError("Hub returned a non-object JSON frame.")

        self._emit_traffic(
            direction="incoming",
            channel="hub",
            message_type=str(message.get("type") or "unknown"),
            payload=message,
        )
        return message

    def _raise_for_error(self, message: dict, fallback: str) -> None:
        payload = message.get("payload") or {}
        detail = payload.get("message") or fallback
        code = payload.get("code")
        if code:
            raise HubProtocolError(f"{detail} [{code}]")
        raise HubProtocolError(detail)

    def _emit_traffic(
        self,
        *,
        direction: str,
        channel: str,
        message_type: str,
        payload: dict[str, Any],
    ) -> None:
        if self._traffic_callback is None:
            return
        summary = self._summarize_traffic(message_type, payload)
        self._traffic_callback(
            {
                "direction": direction,
                "channel": channel,
                "message_type": message_type,
                "summary": summary,
                "payload": payload,
            }
        )

    def _summarize_traffic(self, message_type: str, payload: dict[str, Any]) -> str:
        inner = payload.get("payload")
        if isinstance(inner, dict):
            if message_type == "command":
                command_name = inner.get("command") or inner.get("name") or "command"
                return f"Hub command: {command_name}"
            if message_type == "command_result":
                ok = inner.get("ok")
                correlation_id = inner.get("correlation_id") or "unknown"
                return f"Command result ({'ok' if ok else 'error'}) for {correlation_id}"
            if message_type == "progress":
                return str(inner.get("message") or "Progress update")
        return message_type
