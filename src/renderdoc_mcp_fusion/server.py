from contextlib import asynccontextmanager
import json
import logging
import sys

import anyio

from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool

from .artifacts import json_safe
from .config import Config
from .errors import FusionError
from .gui import EID, STAGE, field, schema
from .shared_router import SharedRouter
from . import __version__
from .extended_api import PUBLIC_EXTRA_SPECS


EVENT = {"event_id": EID}
DEST = {"output_path": field("string", description="Fresh output path. Existing files are never overwritten. Default: Fusion artifacts directory.")}
RESOURCE = {**EVENT, "resource_id": field("string", pattern=r"^(ResourceId::)?[0-9]+$", description="Exact resource ID from this session, supplied as a string.")}
SPECS = [
    ("get_backend_status", "Discover available GUI windows, headless runtime, capability limits, and the pinned capture session.", schema()),
    ("open_capture", "Open .rdc and pin the backend/capture. auto reuses this MCP's private GUI or starts one when RenderDoc is installed, otherwise uses headless. External manual GUI windows require an explicit window_id. gui also starts its bridge automatically: no shell command or manual extension setup is needed. Use headless explicitly for independent replay. GUI launch/query failures never switch backend.", schema({"capture_path": field("string"), "backend": field("string", enum=["auto", "headless", "gui"], default="auto"), "window_id": field("string"), "headless_mode": field("string", enum=["on_demand", "persistent"], default="on_demand", description="Headless on_demand releases the replay engine after each tool call and automatically reloads this file for the next query. persistent keeps it running.")}, ["capture_path"])),
    ("get_capture_status", "Verify that the backend still holds this session's capture.", schema()),
    ("list_draws", "List capture draw/action events with bounded results. Inspect returned event kinds. Headless v0.3.0 names may be empty; event IDs are authoritative.", schema({"filter": field("string"), "limit": field("integer", minimum=1, maximum=10000, default=100)})),
    ("get_pipeline_state", "Pipeline at an explicit event. GUI includes D3D12 fixed-function state; bundled headless returns a limited pipeline summary.", schema(EVENT, ["event_id"])),
    ("get_shader_info", "Read shader reflection, disassembly, source or source-with-disassembly fallback (code). GUI text is paginated with offset/max_lines. Headless supports reflect/disasm only.", schema({**EVENT, "stage": STAGE, "mode": field("string", enum=["reflect", "disasm", "source", "code"], default="reflect"), "offset": field("integer", minimum=0), "max_lines": field("integer", minimum=1, maximum=10000), "file_index": field("integer", minimum=0)}, ["event_id"])),
    ("get_bindings", "Return actual GUI bound resources or explicitly labeled headless declarations. Never infer missing texture IDs.", schema(EVENT, ["event_id"])),
    ("get_cbuffer_data", "Read actual constant-buffer values, binding ID and offset through GUI. Bundled headless v0.3.0 reports unsupported.", schema({**EVENT, "stage": STAGE, "slot": field("integer", minimum=0)}, ["event_id"])),
    ("export_buffer", "GUI: export exact raw buffer bytes at an explicit event to a fresh file.", schema({**RESOURCE, **DEST, "offset": field("integer", minimum=0), "length": field("integer", minimum=0, description="0 means remaining bytes")}, ["event_id", "resource_id"])),
    ("export_texture", "GUI: export a texture image (PNG/HDR/DDS). Native headless texture export is disabled because it writes beside the source capture.", schema({**RESOURCE, **DEST, "format": field("string", enum=["PNG", "HDR", "DDS"] )}, ["event_id", "resource_id"])),
    ("export_mesh", "Export post-transform GPU mesh data. GUI writes raw buffers/metadata to a fresh directory; headless writes OBJ positions/triangles. This does not recover the original rigged model.", schema({**EVENT, **DEST}, ["event_id"])),
    ("export_render_target", "Headless: export RT PNG via one-shot CLI with an explicit output directory. Reopening the capture adds startup cost.", schema({**EVENT, **DEST, "target": field("integer", minimum=0, maximum=7, default=0)}, ["event_id"])),
    ("list_backend_tools", "List permitted native operations with exact schemas. Does not change the selected capture/backend. backend may inspect a catalog before opening.", schema({"backend": field("string", enum=["gui", "headless"])})),
    ("call_backend_tool", "Call an allowed native tool in the current pinned session. Query list_backend_tools first. Pass event IDs explicitly. Lifecycle changes and unsafe default exports are blocked.", schema({"tool_name": field("string"), "arguments": field("object")}, ["tool_name"])),
]
SPECS.extend(PUBLIC_EXTRA_SPECS)


# Every capture operation requires an explicit caller-bound handle.
for _index, (_name, _description, _spec) in enumerate(SPECS):
    if _name == "open_capture":
        _description = (
            "Open an RDC and return capture_id. auto/gui automatically launches RenderDoc and "
            "loads its bridge without installing or enabling an extension. Existing pooled RDCs "
            "are reused. Explicit window_id selects an existing bridge. GUI failures never switch "
            "backend. Pass capture_id to all subsequent analysis calls.")
        _spec["properties"]["headless_mode"]["default"] = "persistent"
        _spec["properties"]["headless_mode"]["description"] = (
            "persistent retains replay until 300 idle seconds, explicit release, or shared service shutdown. "
            "on_demand explicitly releases replay after each call. The shared MCP service remains running.")
    else:
        _spec["properties"]["capture_id"] = field("string", minLength=1,
            description="Handle returned to this connection by open_capture.")
        if _name != "get_backend_status":
            _spec["required"].append("capture_id")
        if _name == "get_backend_status":
            _description = "Inspect the shared service or a caller-owned capture without starting replay."
        elif _name == "list_backend_tools":
            _description = "List the allowed native tools and exact schemas for capture_id."
    SPECS[_index] = (_name, _description, _spec)
SPECS.extend([
    ("list_captures", "List local RDC files in an explicit directory without opening replay. Does not recurse into subdirectories.",
     schema({"directory": field("string", minLength=1), "offset": field("integer", minimum=0), "limit": field("integer", minimum=1, maximum=1000, default=100)}, ["directory"])),
    ("list_instances", "List pooled RDC backends without starting replay.", schema()),
    ("release_capture", "Release this connection's capture handle. Other clients remain connected. "
     "GUI windows remain open; headless replay may stop when the last handle is released.",
     schema({"capture_id": field("string", minLength=1)}, ["capture_id"])),
])


def create_server(config=None, *, router=None, manage_lifespan=True):
    router = router if router is not None else SharedRouter(config or Config.from_environment())

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield router
        finally:
            if manage_lifespan:
                with anyio.CancelScope(shield=True):
                    await router.close()

    server = Server("renderdoc-mcp-fusion", version=__version__, lifespan=lifespan,
        instructions="One shared local service routes explicit capture_id handles. Each connection "
        "must open its own handle. No implicit current capture exists. GUI opens automatically; "
        "no manual extension enablement is needed. Queries require explicit event IDs. "
        "Headless replay expires after 300 idle seconds. Connectors renew service leases every 5 seconds; "
        "missing heartbeats expire after 30 seconds. The shared service stops 60 seconds after its last "
        "connector leaves or expires. Reconnecting during that grace period keeps the service alive. "
        "Reopen explicitly after a capture handle expires or the service restarts.")

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, description=description, inputSchema=spec) for name, description, spec in SPECS]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            response = await router.execute(name, arguments or {})
        except FusionError as exc:
            response = {"ok": False, "error": {"code": exc.code, "message": str(exc),
                "details": json_safe(exc.details)}, "capture_id": (arguments or {}).get("capture_id")}
        except Exception:
            logging.exception("Unhandled Fusion operation %s", name)
            response = {"ok": False, "error": {"code": "INTERNAL_ERROR",
                "message": "Operation failed; see the shared service log"}}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(response, ensure_ascii=False, allow_nan=False))],
            structuredContent=response, isError=not response["ok"])

    return server


def main():
    from .relay import main as relay_main
    relay_main()


if __name__ == "__main__":
    main()
