"""Authenticated loopback MCP transport for one process-wide capture router."""

import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
import math
import os
import re
import socket

import anyio
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .client_context import current_client


HOST = "127.0.0.1"
CLIENT_ID = re.compile(r"[0-9a-f]{32}\Z")


def bind_listener(port=0):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((HOST, port))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


class _RequestSecurity:
    def __init__(self, app, settings):
        self.app = app
        self.security = TransportSecurityMiddleware(settings)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            rejection = await self.security.validate_request(Request(scope, receive))
            if rejection is not None:
                await rejection(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _MCPRequests:
    def __init__(self, manager, expected_token):
        self.manager = manager
        self.expected_token = expected_token

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        authorization = request.headers.get("authorization", "").encode("utf-8")
        if not hmac.compare_digest(authorization, self.expected_token):
            await Response("Forbidden", status_code=403)(scope, receive, send)
            return
        client_id = request.headers.get("x-fusion-client", "")
        if not CLIENT_ID.fullmatch(client_id):
            await Response("Invalid client identity", status_code=400)(scope, receive, send)
            return
        context = current_client.set(client_id)
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            current_client.reset(context)


def create_http_app(server, router, *, port, token, instance_id,
                    reap_interval=1.0, shutdown_callback=None, connector_registry=None):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    if not isinstance(token, str) or not token:
        raise ValueError("A non-empty local service token is required")
    if not isinstance(instance_id, str) or not CLIENT_ID.fullmatch(instance_id):
        raise ValueError("instance_id must be a UUID hex string")
    if not math.isfinite(reap_interval) or reap_interval <= 0:
        raise ValueError("reap_interval must be positive and finite")
    settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:%s" % port, "localhost:%s" % port],
        allowed_origins=["http://127.0.0.1:%s" % port, "http://localhost:%s" % port],
    )
    manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True,
                                          security_settings=settings)
    expected_token = ("Bearer " + token).encode("utf-8")
    stopping = False

    async def begin_shutdown():
        nonlocal stopping
        stopping = True
        if shutdown_callback is not None:
            result = shutdown_callback()
            if asyncio.iscoroutine(result):
                await result

    async def reap():
        while True:
            await anyio.sleep(reap_interval)
            try:
                await router.reap_idle()
            except Exception:
                logging.getLogger(__name__).exception("Capture cleanup failed; shared Hub remains available")
                continue
            if getattr(router, "should_exit", False) and shutdown_callback is not None:
                await begin_shutdown()
                return

    @asynccontextmanager
    async def lifespan(_app):
        try:
            async with manager.run():
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(reap)
                    try:
                        yield
                    finally:
                        tasks.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await router.close()

    async def health(_request):
        return JSONResponse({"service": "renderdoc-mcp-fusion", "version": __version__,
                             "transport": "streamable-http", "instance_id": instance_id,
                             "pid": os.getpid()})

    routes = [Route("/health", health, methods=["GET"]),
              Route("/mcp", _MCPRequests(manager, expected_token))]
    if connector_registry is not None:
        async def update_connector(request):
            provided = request.headers.get("authorization", "").encode("utf-8")
            if not hmac.compare_digest(provided, expected_token):
                return Response("Forbidden", status_code=403)
            if stopping or getattr(router, "should_exit", False):
                return Response("Shared Hub is shutting down", status_code=409)
            import json
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    return Response("Connector registration is too large", status_code=413)
            try:
                value = json.loads(body)
                if not isinstance(value, dict) or set(value) != {"client_id", "action"}:
                    raise ValueError("Expected a connector identity and action")
                if value["action"] not in ("heartbeat", "release"):
                    raise ValueError("Unknown connector action")
                # No await between the shutdown check, registration and response.
                if stopping or getattr(router, "should_exit", False):
                    return Response("Shared Hub is shutting down", status_code=409)
                if value["action"] == "release":
                    connector_registry.unregister(value["client_id"])
                else:
                    connector_registry.register(value["client_id"])
            except (ValueError, TypeError, UnicodeError):
                return Response("Invalid connector registration", status_code=400)
            except (RuntimeError, OSError):
                return Response("Connector registry is unavailable", status_code=409)
            status = connector_registry.status()
            router.keep_alive = status["connector_count"] > 0
            return JSONResponse(status)
        routes.append(Route("/connectors", update_connector, methods=["POST"]))
    if shutdown_callback is not None:
        async def shutdown(request):
            provided = request.headers.get("authorization", "").encode("utf-8")
            if not hmac.compare_digest(provided, expected_token):
                return Response("Forbidden", status_code=403)
            return JSONResponse({"status": "shutting-down"}, status_code=202,
                                background=BackgroundTask(begin_shutdown))
        routes.append(Route("/shutdown", shutdown, methods=["POST"]))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(_RequestSecurity, settings=settings)
    return app
