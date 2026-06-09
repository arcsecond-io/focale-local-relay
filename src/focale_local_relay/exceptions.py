class FocaleError(RuntimeError):
    """Base exception for Focale."""


class FocaleStateError(FocaleError):
    """Raised when the local Focale state is invalid."""


class HubGatewayError(FocaleError):
    """Raised when a Focale Hub API request fails."""

    def __init__(self, message: str, status: int = 400, *, transient: bool = False):
        super().__init__(message)
        self.status = status
        # ``transient`` marks errors that are worth retrying — network failures
        # / timeouts / 5xx (e.g. the API restarting during a deploy) — as opposed
        # to an explicit rejection that should surface (or deauthenticate) at once.
        self.transient = transient


class HubProtocolError(FocaleError):
    """Raised when the Hub handshake or session fails."""
