class FocaleError(RuntimeError):
    """Base exception for Focale."""


class FocaleStateError(FocaleError):
    """Raised when the local Focale state is invalid."""


class HubGatewayError(FocaleError):
    """Raised when a Focale Hub API request fails."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class HubProtocolError(FocaleError):
    """Raised when the Hub handshake or session fails."""
