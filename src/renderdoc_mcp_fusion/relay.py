"""Light stdlib stdio connector; all RenderDoc work belongs to the shared Hub."""

import http.client
import json
import math
import queue
import socket
import sys
import threading
import uuid

from .config import Config
from .shared_service import ensure_service, update_connector


MAX_LINE = 4 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024


def _valid_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _key(value):
    return type(value), value


def _reject_constant(value):
    raise ValueError("Non-finite JSON number: " + value)


class Relay:
    def __init__(self, config, *, request_timeout=300, heartbeat_interval=5):
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request_timeout must be positive and finite")
        if not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive and finite")
        self.config = config
        self.client_id = uuid.uuid4().hex
        self.request_timeout = request_timeout
        self.heartbeat_interval = heartbeat_interval
        self.protocol = "2025-11-25"
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._requests = {}
        self._connections = {}
        self._sockets = {}
        self._queued = set()
        self._cancelled_queued = set()
        # Keep ensure and record publication in the same order across workers;
        # otherwise a delayed old instance could replace a restarted Hub.
        self._startup_lock = threading.Lock()
        # Serialize lease traffic so a heartbeat cannot renew after release.
        # Hub startup does not hold this lock: close must cancel promptly even
        # while ensure_service is waiting for a newly launched Hub.
        self._lease_lock = threading.Lock()
        self._record = None
        self._heartbeat_thread = None

    def queue_request(self, message):
        if isinstance(message, dict) and "id" in message and _valid_id(message["id"]):
            with self._lock:
                self._queued.add(_key(message["id"]))

    def abandon_queued(self, message):
        if isinstance(message, dict) and "id" in message and _valid_id(message["id"]):
            with self._lock:
                key = _key(message["id"])
                self._queued.discard(key)
                self._cancelled_queued.discard(key)

    def cancel(self, request_id):
        if not _valid_id(request_id):
            return
        key = _key(request_id)
        with self._lock:
            cancelled = self._requests.get(key)
            if cancelled is not None:
                cancelled.set()
            elif key in self._queued:
                self._cancelled_queued.add(key)
            active = self._sockets.get(key)
            connection = self._connections.get(key)
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if connection is not None:
            if connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

    def close(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            requests = [key[1] for key in self._requests]
            self._queued.clear()
            self._cancelled_queued.clear()
        for request_id in requests:
            self.cancel(request_id)
        with self._lease_lock:
            record, self._record = self._record, None
            if record is not None:
                self._release(record)

    def _release(self, record):
        try:
            update_connector(record, self.client_id, release=True)
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            # A disappeared Hub needs no release; a transient failure is also
            # bounded by its lease expiry if the connector has already closed.
            pass

    def _remember_service(self, record):
        with self._lease_lock:
            if self._closed.is_set():
                # ensure_service registered before returning, possibly after
                # close had already released the previously known instance.
                self._release(record)
                return
            self._record = record
            if self._heartbeat_thread is None:
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat, daemon=True, name="fusion-connector-heartbeat")
                self._heartbeat_thread.start()

    def _heartbeat(self):
        while not self._closed.wait(self.heartbeat_interval):
            with self._lease_lock:
                if self._closed.is_set():
                    return
                if self._record is not None:
                    try:
                        update_connector(self._record, self.client_id)
                    except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                        # Never ensure/start a Hub from the background thread.
                        # Only another user request can restart a stopped Hub.
                        pass

    def _forward(self, message, record, cancelled):
        port, token = record.get("port"), record.get("token")
        if (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
                or not isinstance(token, str) or not token):
            raise ValueError("Invalid private Hub connection record")
        relay = self
        key = _key(message["id"])

        class Connection(http.client.HTTPConnection):
            def connect(self):
                super().connect()
                with relay._lock:
                    relay._sockets[key] = self.sock
                    aborted = relay._closed.is_set() or cancelled.is_set()
                if aborted:
                    self.close()
                    raise RuntimeError("Request cancelled before transmission")

        connection = Connection("127.0.0.1", port, timeout=self.request_timeout)
        with self._lock:
            if self._closed.is_set() or cancelled.is_set():
                raise RuntimeError("Request cancelled before transmission")
            self._connections[key] = connection
        response = None
        try:
            body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
            connection.request("POST", "/mcp", body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": self.protocol, "Authorization": "Bearer " + token,
                "X-Fusion-Client": self.client_id,
            })
            response = connection.getresponse()
            payload = response.read(MAX_RESPONSE + 1)
            if len(payload) > MAX_RESPONSE:
                raise RuntimeError("Shared MCP response exceeded 8 MiB")
            if response.status != 200:
                raise RuntimeError("Shared MCP Hub returned HTTP " + str(response.status))
            result = json.loads(payload, parse_constant=_reject_constant)
            if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                    or ("result" in result) == ("error" in result)
                    or type(result.get("id")) is not type(message["id"]) or result.get("id") != message["id"]):
                raise RuntimeError("Shared MCP returned an invalid or mismatched response")
            return result
        finally:
            with self._lock:
                if self._connections.get(key) is connection:
                    self._connections.pop(key, None)
                    self._sockets.pop(key, None)
            if response is not None:
                response.close()
            connection.close()

    def handle(self, message):
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
                or "id" in message and not _valid_id(message["id"])):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
        if not isinstance(message.get("params", {}), dict):
            return ({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": "params must be an object"}}
                    if "id" in message else None)
        if "id" not in message:
            if message["method"] == "notifications/cancelled":
                self.cancel(message.get("params", {}).get("requestId"))
            # The Hub uses stateless requests, so initialized/other session
            # notifications have no process-wide side effects to forward.
            return None
        request_id = message["id"]
        key = _key(request_id)
        cancelled = threading.Event()
        with self._lock:
            self._queued.discard(key)
            if key in self._cancelled_queued:
                self._cancelled_queued.discard(key)
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32800, "message": "Request cancelled before execution"}}
            if self._closed.is_set():
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "Connector is closing"}}
            if key in self._requests:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Request ID is already in flight"}}
            self._requests[key] = cancelled
        try:
            with self._startup_lock:
                if self._closed.is_set() or cancelled.is_set():
                    raise RuntimeError("Request cancelled before Hub startup")
                record = ensure_service(self.config, client_id=self.client_id)
                self._remember_service(record)
            if self._closed.is_set() or cancelled.is_set():
                raise RuntimeError("Request cancelled before transmission")
            result = self._forward(message, record, cancelled)
            if message["method"] == "initialize" and isinstance(result.get("result"), dict):
                protocol = result["result"].get("protocolVersion")
                if isinstance(protocol, str):
                    self.protocol = protocol
            return result
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
            aborted = cancelled.is_set() or self._closed.is_set()
            # Do not include local filesystem paths, tokens or backend internals
            # in a wire failure; detailed Hub startup errors go to local stderr.
            if not aborted:
                print("Fusion connector: " + str(exc), file=sys.stderr)
            return {"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32800 if aborted else -32000,
                "message": "Request cancelled" if aborted else "Shared Hub request failed; see connector stderr",
            }}
        finally:
            with self._lock:
                self._requests.pop(key, None)


def main():
    relay = Relay(Config.from_environment())
    incoming = queue.Queue(maxsize=64)
    output_lock = threading.Lock()
    output_closed = threading.Event()

    def write(result):
        if result is None or output_closed.is_set():
            return
        try:
            with output_lock:
                sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
                sys.stdout.flush()
        except (OSError, ValueError):
            output_closed.set()
            relay.close()

    def worker():
        while True:
            message = incoming.get()
            try:
                if message is None:
                    return
                write(relay.handle(message))
            finally:
                incoming.task_done()

    workers = [threading.Thread(target=worker, daemon=True, name="fusion-connector") for _ in range(4)]
    for thread in workers:
        thread.start()
    try:
        while not output_closed.is_set():
            line = sys.stdin.buffer.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "MCP input exceeds 4 MiB"}})
                # Finish this oversized line before parsing the next request.
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(MAX_LINE + 1)
                continue
            try:
                message = json.loads(line, parse_constant=_reject_constant)
            except (ValueError, UnicodeError):
                write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
                continue
            if isinstance(message, dict) and "id" not in message:
                write(relay.handle(message))
                continue
            relay.queue_request(message)
            try:
                incoming.put_nowait(message)
            except queue.Full:
                relay.abandon_queued(message)
                write({"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                       "error": {"code": -32000, "message": "Connector request queue is full"}})
    finally:
        relay.close()
        # Workers are daemon threads; bounded socket cancellation does not keep
        # this lightweight process alive after the host closes its pipe.
        for _ in workers:
            try:
                incoming.put_nowait(None)
            except queue.Full:
                break


if __name__ == "__main__":
    main()
