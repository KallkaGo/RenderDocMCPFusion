"""Request-local identity assigned by each trusted local stdio connector."""
from contextvars import ContextVar

current_client: ContextVar[str] = ContextVar("fusion_current_client", default="")
