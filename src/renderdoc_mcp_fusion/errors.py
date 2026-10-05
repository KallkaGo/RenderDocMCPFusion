class FusionError(Exception):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


class BackendTransportError(FusionError):
    """The backend connection is no longer safe to use for an existing session."""


def require(condition, code, message):
    if not condition:
        raise FusionError(code, message)
