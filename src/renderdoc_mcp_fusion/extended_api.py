"""Explicit, capture-bound analysis catalog shared by both gateway layers."""
from copy import deepcopy


def field(kind, **options):
    return {"type": kind, **options}


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


UINT = field("integer", minimum=0, maximum=0xffffffff)
EID = field("integer", minimum=1, maximum=0xffffffff)
RID = field("string", pattern=r"^(ResourceId::)?[0-9]+$")
STAGE = field("string", enum=["vs", "hs", "ds", "gs", "ps", "cs"])
PAGE = {"offset": UINT, "limit": field("integer", minimum=1, maximum=1000, default=100)}
SUBRESOURCE = {"mip": UINT, "slice_index": UINT, "sample": UINT}
CAST = {"type_cast": field("string", enum=["Typeless", "Float", "UNorm", "SNorm", "UInt", "SInt", "Depth", "UNormSRGB"], default="Typeless")}
RESOURCE_EVENT = {"eid": EID, "rid": RID}
XY = {"x": UINT, "y": UINT}
RANGE = {"eid_min": EID, "eid_max": EID}
SEARCH = {**RANGE, "after_eid": UINT, "limit": field("integer", minimum=1, maximum=256, default=64)}
CODE = {"eid": EID, "stage": STAGE, "offset": UINT,
        "max_lines": field("integer", minimum=1, maximum=10000, default=400)}

# Each definition names an implemented bridge method. Lifecycle and editor
# mutations are deliberately excluded from this capture analysis catalog.
DEFINITIONS = [
    ("get_frame_summary", "get_frame_packet", "Summarize frame passes.", schema({"limit": field("integer", minimum=1, maximum=1000, default=100)})),
    ("get_draw_call_details", "get_draw_packet", "Inspect an explicit draw and its bindings.", schema({"eid": EID}, ["eid"])),
    ("get_textures", "get_textures", "List texture metadata with pagination.", schema(PAGE)),
    ("get_buffers", "get_buffers", "List buffer metadata with pagination.", schema(PAGE)),
    ("get_resources", "get_resources", "List resource metadata with pagination.", schema(PAGE)),
    ("get_texture_info", "get_texture_info", "Read exact texture metadata.", schema({"rid": RID}, ["rid"])),
    ("get_texture_data", "get_texture_data", "Read bounded base64 texture bytes. Native replay reads the selected subresource before the response is capped.", schema({**RESOURCE_EVENT, **SUBRESOURCE, "max_bytes": field("integer", minimum=1, maximum=1048576, default=4096)}, ["eid", "rid"])),
    ("get_buffer_contents", "get_buffer_contents", "Read bounded raw buffer bytes.", schema({**RESOURCE_EVENT, "offset": UINT, "length": field("integer", minimum=1, maximum=1048576, default=4096)}, ["eid", "rid"])),
    ("pick_pixel", "pick_pixel", "Read a pixel from an explicit texture and event.", schema({**RESOURCE_EVENT, **XY, **SUBRESOURCE}, ["eid", "rid", "x", "y"])),
    ("get_texture_minmax", "get_texture_minmax", "Read typed minimum and maximum texture values.", schema({**RESOURCE_EVENT, **SUBRESOURCE}, ["eid", "rid"])),
    ("pixel_history", "pixel_history", "Read pixel modifications up to an explicit event.", schema({**RESOURCE_EVENT, **XY, **SUBRESOURCE, **PAGE}, ["eid", "rid", "x", "y"])),
    ("debug_pixel", "debug_pixel", "Debug a pixel shader with a bounded trace; free replay debug resources after the query.", schema({"eid": EID, **XY, "sample": UINT, "primitive": UINT, "max_steps": field("integer", minimum=1, maximum=1000, default=50)}, ["eid", "x", "y"])),
    ("debug_vertex", "debug_vertex", "Debug a vertex shader with a bounded trace.", schema({"eid": EID, "vertex_id": UINT, "instance_id": UINT, "index": UINT, "view": UINT, "max_steps": field("integer", minimum=1, maximum=1000, default=50)}, ["eid", "vertex_id"])),
    ("get_post_vs_data", "get_post_vs_data", "Inspect post-transform mesh metadata. Use export_mesh to save GPU mesh data.", schema({"eid": EID, "instance_id": UINT, "view": UINT, "stage": field("string", enum=["VSOut", "GSOut"], default="VSOut")}, ["eid"])),
    ("get_debug_messages", "get_debug_messages", "List capture validation messages with pagination.", schema(PAGE)),
    ("get_bound_textures", "get_bound_textures", "Inspect actual texture bindings for a shader stage.", schema({"eid": EID, "stage": STAGE}, ["eid"])),
    ("analyze_lighting", "analyze_lighting", "Inspect shader and constant-buffer evidence for lighting. Model and role labels are heuristics.", schema({"eid": EID, "stage": STAGE, "limit": field("integer", minimum=1, maximum=256, default=64)}, ["eid"])),
    ("identify_drawcalls", "identify_drawcalls", "Inspect bounded draws and optional render-target changes. Content labels are heuristics.", schema({**RANGE, "after_eid": UINT, "limit": field("integer", minimum=1, maximum=128, default=16), "include_diff": field("boolean", default=True), "max_pixels": field("integer", minimum=1, maximum=4194304, default=1048576), "dest": field("string")})),
    ("enumerate_counters", "enumerate_counters", "List GPU counters supported by this replay device.", schema()),
    ("fetch_counters", "fetch_counters", "Fetch real GPU counter values and units.", schema({**RANGE, "counter_ids": field("array", items=UINT, minItems=1, maxItems=64, uniqueItems=True), "limit": field("integer", minimum=1, maximum=4096, default=256)}, ["counter_ids"])),
    ("get_action_timings", "get_action_timings", "Fetch the GPU duration counter. Unsupported hardware returns an explicit error.", schema({**RANGE, "limit": field("integer", minimum=1, maximum=4096, default=256)})),
    ("find_draws_by_shader", "find_draws_by_shader", "Find actual draws using a shader. Bounded scans return a continuation event.", schema({**SEARCH, "sid": RID, "stage": STAGE, "scan_limit": field("integer", minimum=1, maximum=4096, default=512)}, ["sid"])),
    ("find_draws_by_resource", "find_draws_by_resource", "Find actual draw events from resource usage.", schema({**SEARCH, "rid": RID, "usage": field("string")}, ["rid"])),
    ("find_draws_by_texture", "find_draws_by_texture", "Find actual draw events that use a texture.", schema({**SEARCH, "rid": RID, "usage": field("string")}, ["rid"])),
    ("debug_vulkan_bindings", "debug_vulkan_bindings", "Inspect Vulkan descriptor binding evidence at an explicit event.", schema({"eid": EID, "stage": STAGE, "limit": field("integer", minimum=1, maximum=256, default=64)}, ["eid"])),
    ("export_drawcall", "export_drawcall", "Export shaders, textures, pipeline and mesh to a fresh bundle. The manifest reports partial failures.", schema({"eid": EID, "dest": field("string")}, ["eid"])),
    ("export_to_unity", "export_to_unity", "Export a Unity import bundle with OBJ, textures and an import script. This does not recover the original material or rig.", schema({"eid": EID, "dest": field("string")}, ["eid"])),
]
for _public, _native, _description, _spec in DEFINITIONS:
    if _native in {"pick_pixel", "get_texture_minmax", "pixel_history"}:
        _spec["properties"].update(deepcopy(CAST))
    if _native == "get_buffer_contents":
        _spec["properties"].update(offset=field("integer", minimum=0, maximum=0xffffffffffffffff),
            max_bytes=field("integer", minimum=1, maximum=1048576))
    if _native == "identify_drawcalls":
        _spec["properties"]["render_target"] = deepcopy(RID)

GUI_EXTRA_SCHEMAS = {native: deepcopy(spec) for _, native, _, spec in DEFINITIONS}
for _export in ("export_drawcall", "export_to_unity"):
    GUI_EXTRA_SCHEMAS[_export]["required"].append("dest")
for _method in ("get_shader_disasm", "get_shader_source", "get_shader_code"):
    _properties = deepcopy(CODE)
    if _method != "get_shader_disasm":
        _properties.update(file=field("string"), file_index=UINT)
    GUI_EXTRA_SCHEMAS[_method] = schema(_properties, ["eid", "stage"])

PUBLIC_TO_NATIVE = {public: native for public, native, _, _ in DEFINITIONS}
PARAM_NAMES = {"eid": "event_id", "rid": "resource_id", "sid": "shader_id", "dest": "output_path"}
PUBLIC_EXTRA_SPECS = []
for _public, _native, _description, _spec in DEFINITIONS:
    _public_schema = deepcopy(_spec)
    _public_schema["properties"] = {PARAM_NAMES.get(key, key): value for key, value in _public_schema["properties"].items()}
    _public_schema["required"] = [PARAM_NAMES.get(key, key) for key in _public_schema["required"]]
    PUBLIC_EXTRA_SPECS.append((_public, "GUI: " + _description, _public_schema))


async def dispatch_extended(router, selected, name, arguments):
    from .errors import require
    require(router.session.backend == "gui", "UNSUPPORTED_OPERATION",
            name + " requires GUI replay; open this capture with backend='gui'. "
            "Use list_backend_tools for available headless operations.")
    reverse_names = {value: key for key, value in PARAM_NAMES.items()}
    params = {reverse_names.get(key, key): value for key, value in arguments.items()}
    if name in {"export_drawcall", "export_to_unity"}:
        params["dest"] = str(router.export_path(name, "", params.get("dest"), directory=True))
    elif params.get("dest"):
        params["dest"] = str(router.export_path(name, ".json", params["dest"]))
    return await selected.call(PUBLIC_TO_NATIVE[name], params)


def list_capture_files(directory, offset=0, limit=100):
    """List local RDC files without starting replay or choosing a capture."""
    from pathlib import Path
    from .errors import FusionError, require
    require(isinstance(directory, str) and bool(directory), "INVALID_ARGUMENT", "directory is required")
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 1000,
            "INVALID_ARGUMENT", "offset must be nonnegative and limit must be 1..1000")
    path = Path(directory).expanduser().resolve()
    try:
        require(path.is_dir(), "DIRECTORY_NOT_FOUND", "Expected an existing directory")
        files = sorted((p for p in path.iterdir() if p.suffix.lower() == ".rdc" and p.is_file()), key=lambda p: (p.name.casefold(), p.name))
        items = []
        for entry in files[offset:offset + limit]:
            stat = entry.stat()
            items.append({"name": entry.name, "path": str(entry), "size": stat.st_size, "modified_ns": stat.st_mtime_ns})
    except OSError as exc:
        raise FusionError("DIRECTORY_READ_FAILED", "Cannot read capture directory", str(exc)) from exc
    next_offset = offset + len(items)
    return {"directory": str(path), "items": items, "total": len(files),
            "next_offset": next_offset if next_offset < len(files) else None,
            "recursive": False}
