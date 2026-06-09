from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from focale_local_relay.agent_auth import AgentKeypair, b64d, signature_payload


def test_keypair_signs_expected_payload():
    keypair = AgentKeypair.create()
    agent_uuid = "00000000-0000-0000-0000-000000000123"
    nonce_b64 = "bW9jay1ub25jZS1ieXRlcy0wMDAwMDAwMDAwMDA="
    signature_b64 = keypair.sign_nonce(agent_uuid=agent_uuid, nonce_b64=nonce_b64)

    public_key = Ed25519PublicKey.from_public_bytes(b64d(keypair.public_key_b64))
    public_key.verify(
        b64d(signature_b64),
        signature_payload(agent_uuid, b64d(nonce_b64)),
    )


def test_public_key_is_raw_ed25519_bytes():
    keypair = AgentKeypair.create()

    raw = keypair.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    assert len(raw) == 32
    assert b64d(keypair.public_key_b64) == raw
