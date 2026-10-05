"""Keep the shared service alive while MCP connectors send heartbeats.

Call this registry from the service's single asyncio thread. All methods are
synchronous; callers from other threads must serialize access.
"""
import math
import re
import time


_CLIENT_ID = re.compile(r"[0-9a-f]{32}")


def _validate_client_id(client_id):
    if not isinstance(client_id, str) or _CLIENT_ID.fullmatch(client_id) is None:
        raise ValueError("Connector client ID must be 32 lowercase hexadecimal characters")


class ConnectorLeases:
    """Track connector heartbeats and an idle shutdown grace period."""

    def __init__(self, clock=time.monotonic, lease_timeout=30.0, grace_period=60.0):
        if not math.isfinite(lease_timeout) or lease_timeout <= 0:
            raise ValueError("Connector lease timeout must be positive and finite")
        if not math.isfinite(grace_period) or grace_period < 0:
            raise ValueError("Shutdown grace period must be nonnegative and finite")
        self._clock = clock
        self._lease_timeout = lease_timeout
        self._grace_period = grace_period
        self._leases = {}
        self._empty_since = clock()
        self._closed = False

    def _expire(self, now):
        expired = [client_id for client_id, deadline in self._leases.items()
                   if deadline <= now]
        last_expiry = None
        for client_id in expired:
            deadline = self._leases.pop(client_id)
            last_expiry = deadline if last_expiry is None else max(last_expiry, deadline)
        if last_expiry is not None and not self._leases:
            # A delayed poll must not give dead connectors extra grace time.
            self._empty_since = last_expiry

    def register(self, client_id):
        """Create or refresh a lease and cancel the idle shutdown countdown."""
        if self._closed:
            raise RuntimeError("Connector lease registry is closed")
        _validate_client_id(client_id)
        now = self._clock()
        self._expire(now)
        self._leases[client_id] = now + self._lease_timeout
        self._empty_since = None

    def unregister(self, client_id):
        """Remove a lease; repeated removal never restarts the countdown."""
        _validate_client_id(client_id)
        now = self._clock()
        self._expire(now)
        if client_id in self._leases:
            del self._leases[client_id]
            if not self._leases:
                self._empty_since = now

    def poll(self):
        """Expire leases whose connectors missed their heartbeat deadline."""
        self._expire(self._clock())

    @property
    def should_exit(self):
        now = self._clock()
        self._expire(now)
        return self._closed or (not self._leases
                                and now >= self._empty_since + self._grace_period)

    def status(self):
        """Return lifetime counts and timers without exposing connector IDs."""
        now = self._clock()
        self._expire(now)
        remaining = None
        if self._closed:
            remaining = 0.0
        elif not self._leases:
            remaining = max(0.0, self._empty_since + self._grace_period - now)
        return {
            "lifetime": "mcp_connectors",
            "connector_count": len(self._leases),
            "lease_timeout_seconds": self._lease_timeout,
            "shutdown_grace_seconds": self._grace_period,
            "shutdown_in_seconds": remaining,
        }

    def close(self):
        """Release all leases exactly once and reject further registration."""
        self._closed = True
        self._leases.clear()
