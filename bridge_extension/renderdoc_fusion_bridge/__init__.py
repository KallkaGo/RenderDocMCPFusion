"""Fusion bridge derived from JinxiangW/renderdoc-mcp bb7d5774.
Starts only local IPC. No decompiler installation or processor registration.
"""
from .request_handler import RequestHandler
from .server import BridgeServer
_server = None

def register(version, ctx):
    global _server
    if _server is not None:
        return
    _server = BridgeServer(RequestHandler(ctx))
    _server.start()

def unregister():
    global _server
    if _server is not None:
        _server.stop()
        _server = None
