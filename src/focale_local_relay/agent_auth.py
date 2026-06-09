from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .exceptions import FocaleStateError
from .state import _restrict_permissions

PREFIX = b"focale-hub:v1\n"


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def signature_payload(agent_uuid: str, nonce_raw: bytes) -> bytes:
    return (
        PREFIX
        + b"agent_uuid="
        + agent_uuid.encode("utf-8")
        + b"\n"
        + b"nonce="
        + nonce_raw
    )


@dataclass
class AgentKeypair:
    private_key: Ed25519PrivateKey

    @classmethod
    def create(cls) -> "AgentKeypair":
        return cls(private_key=Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: Path) -> "AgentKeypair":
        if path.exists():
            return cls.load(path)

        path.parent.mkdir(parents=True, exist_ok=True)
        keypair = cls.create()
        keypair.save(path)
        return keypair

    @classmethod
    def load(cls, path: Path) -> "AgentKeypair":
        try:
            raw = path.read_bytes()
            private_key = serialization.load_pem_private_key(raw, password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise FocaleStateError(f"Unable to load local agent key from {path}: {exc}") from exc
        if not isinstance(private_key, Ed25519PrivateKey):
            raise FocaleStateError(
                f"Local agent key at {path} is not an Ed25519 private key."
            )
        return cls(private_key=private_key)

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    @property
    def public_key_b64(self) -> str:
        raw = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return b64e(raw)

    def save(self, path: Path) -> None:
        raw = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path.write_bytes(raw)
        _restrict_permissions(path)

    def sign_nonce(self, *, agent_uuid: str, nonce_b64: str) -> str:
        nonce_raw = b64d(nonce_b64)
        signature = self.private_key.sign(signature_payload(agent_uuid, nonce_raw))
        return b64e(signature)
