import json

from focale_local_relay.alpaca import _parse_discovery_payload, normalize_alpaca_address


def test_normalize_alpaca_address() -> None:
    assert normalize_alpaca_address("HTTP://LOCALHOST:11111/") == "http://localhost:11111"
    assert normalize_alpaca_address("192.168.0.5:32227") == "http://192.168.0.5:32227"
    assert normalize_alpaca_address("http://10.0.0.1") == "http://10.0.0.1"


def test_parse_discovery_payload() -> None:
    payload = json.dumps({"AlpacaPort": 11111}).encode("utf-8")
    assert _parse_discovery_payload(payload) == {"alpaca_port": 11111}

    payload = json.dumps({"alpaca_port": "12345"}).encode("utf-8")
    assert _parse_discovery_payload(payload) == {"alpaca_port": 12345}

    assert _parse_discovery_payload(b"invalid-json") is None
