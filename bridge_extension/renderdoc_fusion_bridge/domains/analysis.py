"""Bounded replay evidence, GPU counters, and explicitly labelled analysis heuristics.

The legacy RenderDocMCP bridge was used as an API reference. This implementation
does not infer semantic truth from resource names or substitute timings with zero.
"""

import json
import math
import os
import re
import struct

import renderdoc as rd


class _ReplayFatalError(RuntimeError):
    def __init__(self, code, message):
        self.native_code = str(code)
        self.native_message = str(message)
        super().__init__("RenderDoc replay is unusable: {} ({}). Reopen the capture before retrying.".format(message, code))


def _replay_failure_status(controller):
    query = getattr(controller, "GetFatalErrorStatus", None)
    if query is None:
        return None
    status = query()
    code = status.code
    succeeded = getattr(getattr(rd, "ResultCode", None), "Succeeded", 0)
    if code == succeeded or str(code).split(".")[-1] == "Succeeded":
        return None
    message = status.Message() if callable(getattr(status, "Message", None)) else getattr(status, "message", str(status))
    return code, message


def replay_failure(controller):
    """Return native fatal replay details, or None; call only on replay thread.

    GetFatalErrorStatus returns ResultDetails. Only ResultCode.Succeeded means
    usable replay. The status persists after a failure and querying does not
    clear it. Older controllers without this API return None.
    """
    failure = _replay_failure_status(controller)
    return "{} ({})".format(failure[1], failure[0]) if failure is not None else None


def windows_developer_mode():
    """Read the 64-bit Windows enable setting without changing it.

    A readable key with no enable value is unconfigured/disabled. Access
    failures, unavailable registry support and unexpected values are unknown.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock", 0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")
            except FileNotFoundError:
                return False
    except OSError:
        return None
    if kind != winreg.REG_DWORD or value not in (0, 1):
        return None
    return bool(value)


def _integer(params, key, default=None, minimum=0, maximum=0xffffffff):
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("{} must be an integer in [{}, {}]".format(key, minimum, maximum))
    return value


def _rid_key(value):
    return str(value).split("::")[-1]


def _resource(binding):
    descriptor = getattr(binding, "descriptor", binding)
    return getattr(descriptor, "resource", getattr(descriptor, "resourceId", None))


def _valid_resource(value):
    return value is not None and _rid_key(value) not in ("Null", "0", "None", "")


def _plain(value, state, depth=0):
    """Serialize SWIG data under a shared node and sequence budget."""
    state["remaining"] -= 1
    if state["remaining"] < 0 or depth > 6:
        state["truncated"] = True
        return {"omitted": "serialization_budget", "type": type(value).__name__}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if type(value).__name__ == "ResourceId":
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": len(value)}
    if isinstance(value, dict):
        pairs = list(value.items())
    else:
        try:
            items = list(value)
        except TypeError:
            items = None
        if items is not None:
            truncated = len(items) > state["limit"]
            state["truncated"] = state["truncated"] or truncated
            result = []
            for item in items[:state["limit"]]:
                if state["remaining"] <= 0:
                    state["truncated"] = True
                    break
                result.append(_plain(item, state, depth + 1))
            return {"items": result, "total": len(items), "truncated": True} if truncated else result
        pairs = []
        for name in dir(value):
            if name.startswith("_") or name in ("this", "thisown"):
                continue
            try:
                item = getattr(value, name)
                if not callable(item):
                    pairs.append((name, item))
            except Exception:
                continue
    result = {}
    for name, item in pairs[:state["limit"]]:
        if state["remaining"] <= 0:
            state["truncated"] = True
            break
        result[str(name)] = _plain(item, state, depth + 1)
    if len(pairs) > state["limit"]:
        state["truncated"] = True
    return result or str(value)


class AnalysisServiceMixin:
    @staticmethod
    def _analysis_envelope(data=None, code=None, message=None, truncated=False):
        return {"ok": code is None, "mode": "summary", "data": data,
                "err": {"code": code, "msg": message} if code else None,
                "meta": {"cap": "active", "truncated": truncated}}

    @staticmethod
    def _analysis_check_fatal(controller):
        # GetFatalErrorStatus is the public controller API. Older installations
        # may lack it; their missing counter evidence is still checked below.
        failure = _replay_failure_status(controller)
        if failure is not None:
            raise _ReplayFatalError(failure[0], failure[1])

    def _analysis_counter_preflight(self, controller):
        properties = controller.GetAPIProperties() if callable(getattr(controller, "GetAPIProperties", None)) else self.ctx.APIProps()
        if os.name != "nt" or str(properties.pipelineType).split(".")[-1].lower() != "d3d12":
            return None
        if windows_developer_mode() is False:
            return self._analysis_envelope(data={"complete": False, "developer_mode": "disabled_or_unconfigured",
                "registry_setting": r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock\AllowDevelopmentWithoutDevLicense",
                "fetch_attempted": False}, code="developer_mode_required",
                message="D3D12 GPU counters require Windows Developer Mode. The enable setting is disabled or unconfigured; counter replay was not attempted.")
        return None

    def _analysis_execute(self, operation, restore=False):
        if not self.ctx.IsCaptureLoaded():
            return self._no_capture()
        previous = int(self.ctx.CurEvent()) if restore and hasattr(self.ctx, "CurEvent") else None
        result = {}
        def collect(controller):
            fatal = False
            try:
                self._analysis_check_fatal(controller)
                result.update(operation(controller))
                self._analysis_check_fatal(controller)
            except _ReplayFatalError:
                fatal = True
                raise
            finally:
                if not fatal:
                    self._analysis_check_fatal(controller)
                    if previous is not None:
                        controller.SetFrameEvent(previous, False)
                        self._analysis_check_fatal(controller)
        try:
            self.ctx.Replay().BlockInvoke(collect)
        except _ReplayFatalError as exc:
            return self._analysis_envelope(data={"complete": False,
                "native_error": {"code": exc.native_code, "message": exc.native_message}},
                code="replay_fatal_error", message=str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            return self._analysis_envelope(code="invalid_params", message=str(exc))
        except Exception as exc:
            return self._analysis_envelope(code="replay_error", message=str(exc))
        return result

    @staticmethod
    def _analysis_stage(name):
        aliases = {"vertex": "vs", "fragment": "ps", "pixel": "ps", "compute": "cs",
                   "geometry": "gs", "hull": "hs", "domain": "ds", "tess_ctrl": "hs", "tess_eval": "ds"}
        name = str(name).lower()
        name = aliases.get(name, name)
        attributes = {"vs": "Vertex", "ps": "Pixel", "cs": "Compute", "gs": "Geometry", "hs": "Hull", "ds": "Domain"}
        if name not in attributes:
            raise ValueError("Unsupported stage: " + name)
        return name.upper(), getattr(rd.ShaderStage, attributes[name])

    @staticmethod
    def _analysis_range(params, default_limit=64, max_limit=256):
        minimum = _integer(params, "eid_min", 0)
        maximum = _integer(params, "eid_max", 0xffffffff)
        after = _integer(params, "after_eid", 0)
        limit = _integer(params, "limit", default_limit, 1, max_limit)
        if minimum > maximum:
            raise ValueError("eid_min must not exceed eid_max")
        return minimum, maximum, after, limit

    @staticmethod
    def _analysis_draws(controller):
        result = []
        def visit(nodes):
            for action in nodes:
                if action.flags & rd.ActionFlags.Drawcall:
                    result.append(action)
                visit(action.children)
        visit(controller.GetRootActions())
        return sorted(result, key=lambda action: int(action.eventId))

    @staticmethod
    def _analysis_has_gpu_work(controller, minimum, maximum):
        flags = rd.ActionFlags.Drawcall | getattr(rd.ActionFlags, "Dispatch", 0) | getattr(rd.ActionFlags, "MeshDispatch", 0)
        def contains(actions):
            for action in actions:
                if minimum <= int(action.eventId) <= maximum and action.flags & flags:
                    return True
                if contains(action.children):
                    return True
            return False
        return contains(controller.GetRootActions())

    @staticmethod
    def _analysis_require_event(controller, eid):
        def contains(actions):
            for action in actions:
                if int(action.eventId) == eid:
                    return True
                if any(int(event.eventId) == eid for event in getattr(action, "events", [])):
                    return True
                if contains(action.children):
                    return True
            return False
        if not contains(controller.GetRootActions()):
            raise ValueError("eid {} does not exist in the active capture".format(eid))

    def _analysis_action(self, action):
        return {"eid": int(action.eventId), "name": action.customName or action.GetName(self.ctx.GetStructuredFile()),
                "flags": str(action.flags), "num_indices": int(action.numIndices),
                "num_instances": int(action.numInstances)}

    @staticmethod
    def _analysis_variable(variable, state, depth=0):
        record = {"name": variable.name, "type": str(variable.type),
                  "rows": int(variable.rows), "columns": int(variable.columns)}
        state["remaining"] -= 1
        if state["remaining"] <= 0 or depth > 6:
            state["truncated"] = True
            record["omitted"] = "serialization_budget"
            return record
        members = list(getattr(variable, "members", []))
        if members:
            record["members"] = [AnalysisServiceMixin._analysis_variable(v, state, depth + 1) for v in members[:state["limit"]] if state["remaining"] > 0]
            state["truncated"] = state["truncated"] or len(members) > len(record["members"])
            return record
        kind = str(variable.type).split(".")[-1].lower()
        fields = {"float": "f32v", "double": "f64v", "uint": "u32v", "sint": "s32v", "int": "s32v",
                  "bool": "u32v", "half": "f16v", "ulong": "u64v", "slong": "s64v",
                  "ushort": "u16v", "sshort": "s16v", "ubyte": "u8v", "sbyte": "s8v"}
        field = fields.get(kind)
        if field is None:
            raise RuntimeError("Unsupported shader variable type: " + kind)
        count = max(1, int(variable.rows) * int(variable.columns))
        values = getattr(variable.value, field)
        def typed(v):
            if kind == "bool":
                return bool(v)
            if kind == "half":
                # Python bindings expose rdhalf either as float or raw uint16.
                return struct.unpack("<e", struct.pack("<H", v))[0] if isinstance(v, int) else float(v)
            return v
        record["value"] = [typed(v) for v in values[:min(count, state["limit"])]]
        record["value"] = _plain(record["value"], state)
        state["truncated"] = state["truncated"] or count > state["limit"]
        return record

    @staticmethod
    def _analysis_counter_description(controller, counter):
        desc = controller.DescribeCounter(counter)
        return {"id": int(counter), "name": desc.name, "description": desc.description,
                "unit": str(desc.unit), "result_type": str(desc.resultType),
                "result_byte_width": int(desc.resultByteWidth)}

    @staticmethod
    def _analysis_counter_value(value, desc):
        kind = str(desc.resultType).split(".")[-1]
        width = int(desc.resultByteWidth)
        if kind in ("Float", "Double") and width in (4, 8):
            field = "f" if width == 4 else "d"
        elif kind == "UInt" and width in (4, 8):
            field = "u32" if width == 4 else "u64"
        else:
            raise RuntimeError("Unsupported counter result type/width: {}/{}".format(kind, width))
        result = getattr(value, field)
        if isinstance(result, float) and not math.isfinite(result):
            raise RuntimeError("Counter returned a non-finite value")
        return result

    def enumerate_counters(self, params):
        def collect(controller):
            items = [self._analysis_counter_description(controller, counter) for counter in controller.EnumerateCounters()]
            return self._analysis_envelope({"items": items, "count": len(items), "complete": True})
        return self._analysis_execute(collect)

    def fetch_counters(self, params):
        def collect(controller):
            minimum, maximum, _, limit = self._analysis_range(params, 256, 4096)
            ids = params.get("counter_ids")
            if not isinstance(ids, list) or not ids or len(ids) > 64 or any(isinstance(c, bool) or not isinstance(c, int) for c in ids):
                raise ValueError("counter_ids must contain 1 to 64 integer counter IDs")
            available = {int(c): c for c in controller.EnumerateCounters()}
            missing = [c for c in ids if c not in available]
            if missing:
                return self._analysis_envelope(code="counter_unavailable", message="Unavailable counters: " + str(missing))
            selected = [available[c] for c in dict.fromkeys(ids)]
            preflight = self._analysis_counter_preflight(controller)
            if preflight is not None:
                return preflight
            descriptions = {int(c): controller.DescribeCounter(c) for c in selected}
            descriptor_records = [self._analysis_counter_description(controller, c) for c in selected]
            values = [r for r in controller.FetchCounters(selected) if minimum <= int(r.eventId) <= maximum]
            self._analysis_check_fatal(controller)
            if not values and self._analysis_has_gpu_work(controller, minimum, maximum):
                return self._analysis_envelope(data={"complete": False, "items": [], "counters": descriptor_records},
                    code="counter_results_unavailable", message="No counter results were returned for a range containing GPU actions")
            values.sort(key=lambda r: (int(r.eventId), int(r.counter)))
            truncated = len(values) > limit
            items = [{"eid": int(r.eventId), "counter": int(r.counter),
                      "value": self._analysis_counter_value(r.value, descriptions[int(r.counter)]),
                      "unit": str(descriptions[int(r.counter)].unit)} for r in values[:limit]]
            return self._analysis_envelope({"items": items, "count": len(items), "total": len(values),
                                           "complete": not truncated,
                                           "counters": descriptor_records}, truncated=truncated)
        return self._analysis_execute(collect)

    def get_action_timings(self, params):
        def collect(controller):
            minimum, maximum, _, limit = self._analysis_range(params, 256, 4096)
            counter = rd.GPUCounter.EventGPUDuration
            if int(counter) not in {int(c) for c in controller.EnumerateCounters()}:
                return self._analysis_envelope(code="counter_unavailable", message="EventGPUDuration is unavailable for this capture/device")
            preflight = self._analysis_counter_preflight(controller)
            if preflight is not None:
                return preflight
            desc = controller.DescribeCounter(counter)
            if str(desc.unit).split(".")[-1] != "Seconds":
                return self._analysis_envelope(code="unsupported_counter_unit", message="GPU duration counter unit is " + str(desc.unit))
            values = sorted([r for r in controller.FetchCounters([counter]) if minimum <= int(r.eventId) <= maximum], key=lambda r: int(r.eventId))
            self._analysis_check_fatal(controller)
            if not values and self._analysis_has_gpu_work(controller, minimum, maximum):
                return self._analysis_envelope(data={"complete": False, "items": []}, code="counter_results_unavailable",
                    message="No GPU duration results were returned for a range containing GPU actions")
            items = []
            for result in values[:limit]:
                duration = self._analysis_counter_value(result.value, desc)
                if duration < 0:
                    return self._analysis_envelope(code="counter_value_unavailable", message="Negative GPU duration at eid {}".format(result.eventId))
                items.append({"eid": int(result.eventId), "duration_seconds": duration})
            truncated = len(values) > limit
            return self._analysis_envelope({"source": "GPUCounter.EventGPUDuration", "counter": self._analysis_counter_description(controller, counter),
                                           "items": items, "total": len(values), "complete": not truncated}, truncated=truncated)
        return self._analysis_execute(collect)

    def _analysis_find_resource(self, params, texture_only=False):
        def collect(controller):
            minimum, maximum, after, limit = self._analysis_range(params)
            rid = params.get("rid")
            if not _valid_resource(rid):
                raise ValueError("A non-null rid is required")
            resources = controller.GetTextures() if texture_only else controller.GetResources()
            resource = next((r for r in resources if _rid_key(r.resourceId) == _rid_key(rid)), None)
            if resource is None:
                return self._analysis_envelope(code="resource_not_found", message="Resource not found: " + str(rid))
            draws = {int(a.eventId): a for a in self._analysis_draws(controller)}
            uses = {}
            usage_filter = str(params.get("usage", "")).lower()
            for use in controller.GetUsage(resource.resourceId):
                eid = int(use.eventId)
                if eid in draws and minimum <= eid <= maximum and eid > after and (not usage_filter or usage_filter in str(use.usage).lower()):
                    uses.setdefault(eid, []).append({"usage": str(use.usage), "view": str(getattr(use, "view", ""))})
            selected = sorted(uses)[:limit]
            items = [dict(self._analysis_action(draws[eid]), usage=uses[eid]) for eid in selected]
            truncated = len(uses) > limit
            return self._analysis_envelope({"rid": str(resource.resourceId), "source": "ReplayController.GetUsage",
                                           "items": items, "count": len(items), "total": len(uses),
                                           "next_after_eid": selected[-1] if truncated else None, "complete": not truncated}, truncated=truncated)
        return self._analysis_execute(collect)

    def find_draws_by_resource(self, params):
        return self._analysis_find_resource(params)

    def find_draws_by_texture(self, params):
        return self._analysis_find_resource(params, texture_only=True)

    def find_draws_by_shader(self, params):
        def collect(controller):
            minimum, maximum, after, limit = self._analysis_range(params)
            scan_limit = _integer(params, "scan_limit", 512, 1, 4096)
            sid = params.get("sid")
            if not _valid_resource(sid):
                raise ValueError("A non-null sid is required")
            stages = [self._analysis_stage(params["stage"])] if params.get("stage") else [self._analysis_stage(s) for s in ("VS", "HS", "DS", "GS", "PS")]
            candidates = [a for a in self._analysis_draws(controller) if minimum <= int(a.eventId) <= maximum and int(a.eventId) > after]
            items, errors, scanned = [], [], 0
            for action in candidates[:scan_limit]:
                scanned += 1
                try:
                    controller.SetFrameEvent(int(action.eventId), False)
                    self._analysis_check_fatal(controller)
                    pipe = controller.GetPipelineState()
                    matches = [name for name, stage in stages if _rid_key(pipe.GetShader(stage)) == _rid_key(sid)]
                    if matches:
                        items.append(dict(self._analysis_action(action), stages=matches))
                        if len(items) >= limit:
                            break
                except _ReplayFatalError:
                    raise
                except Exception as exc:
                    errors.append({"eid": int(action.eventId), "error": str(exc)})
            truncated = scanned < len(candidates)
            return self._analysis_envelope({"sid": str(sid), "items": items, "count": len(items), "scanned": scanned,
                                           "next_after_eid": int(candidates[scanned - 1].eventId) if truncated and scanned else None,
                                           "errors": errors, "complete": not truncated and not errors}, truncated=truncated)
        return self._analysis_execute(collect, restore=True)

    def analyze_lighting(self, params):
        def collect(controller):
            eid = _integer(params, "eid", minimum=1)
            limit = _integer(params, "limit", 64, 1, 256)
            name, stage = self._analysis_stage(params.get("stage", "PS"))
            self._analysis_require_event(controller, eid)
            controller.SetFrameEvent(eid, False)
            self._analysis_check_fatal(controller)
            pipe = controller.GetPipelineState()
            shader = pipe.GetShader(stage)
            reflection = pipe.GetShaderReflection(stage)
            if not _valid_resource(shader) or reflection is None:
                return self._analysis_envelope(code="shader_reflection_unavailable", message="Bound shader and reflection are required")
            pattern = re.compile(r"light|shadow|ambient|diffuse|specular|radiance|irradiance|normal|roughness|metallic|exposure", re.I)
            state = {"remaining": 2048, "limit": limit, "truncated": False}
            blocks, textures, errors = [], [], []
            reflected_blocks = list(getattr(reflection, "constantBlocks", []))
            for index, block in enumerate(reflected_blocks[:limit]):
                record = {"name": block.name, "reflection_index": index, "variables": None}
                try:
                    binding = pipe.GetConstantBlock(stage, index, 0)
                    descriptor = getattr(binding, "descriptor", binding)
                    resource = _resource(binding)
                    record["binding"] = _plain(binding, state)
                    # Non-buffer blocks (e.g. Vulkan push constants) legitimately use a null resource.
                    if getattr(block, "bufferBacked", True) and not _valid_resource(resource):
                        raise RuntimeError("Constant block has no bound buffer")
                    pipeline = pipe.GetComputePipelineObject() if name == "CS" else pipe.GetGraphicsPipelineObject()
                    variables = controller.GetCBufferVariableContents(pipeline, shader, stage, pipe.GetShaderEntryPoint(stage), index,
                                    resource if resource is not None else rd.ResourceId(), int(getattr(descriptor, "byteOffset", 0)),
                                    int(getattr(descriptor, "byteSize", getattr(block, "byteSize", 0))))
                    variables = list(variables)
                    record["variables"] = [self._analysis_variable(v, state) for v in variables[:limit]]
                    state["truncated"] = state["truncated"] or len(variables) > limit
                    record["name_candidates"] = [v.name for v in variables if pattern.search(v.name)]
                except _ReplayFatalError:
                    raise
                except Exception as exc:
                    record["error"] = str(exc)
                    errors.append({"block": index, "error": str(exc)})
                blocks.append(record)
            try:
                declarations = list(getattr(reflection, "readOnlyResources", []))
                bindings = list(pipe.GetReadOnlyResources(stage, False))
                for binding in bindings[:limit]:
                    access = getattr(binding, "access", None)
                    index = int(getattr(access, "index", -1))
                    declaration = declarations[index] if 0 <= index < len(declarations) else None
                    if declaration is not None and getattr(declaration, "isTexture", False):
                        textures.append({"name": declaration.name, "rid": str(_resource(binding)),
                                         "reflection_index": index, "slot": int(declaration.fixedBindNumber),
                                         "array_element": int(getattr(access, "arrayElement", 0)),
                                         "lighting_name_candidate": bool(pattern.search(declaration.name))})
                state["truncated"] = state["truncated"] or len(bindings) > limit
            except Exception as exc:
                errors.append({"operation": "GetReadOnlyResources", "error": str(exc)})
            state["truncated"] = state["truncated"] or len(reflected_blocks) > limit
            return self._analysis_envelope({"eid": eid, "stage": name, "sid": str(shader), "constant_blocks": blocks,
                                           "textures": textures, "heuristic": "Name candidates only; lighting model and material roles are unverified",
                                           "errors": errors, "complete": not errors and not state["truncated"]}, truncated=state["truncated"])
        return self._analysis_execute(collect, restore=True)

    def debug_vulkan_bindings(self, params):
        def collect(controller):
            eid = _integer(params, "eid", minimum=1)
            limit = _integer(params, "limit", 64, 1, 256)
            name, stage = self._analysis_stage(params.get("stage", "PS"))
            self._analysis_require_event(controller, eid)
            controller.SetFrameEvent(eid, False)
            self._analysis_check_fatal(controller)
            pipe = controller.GetPipelineState()
            if "vulkan" not in str(self.ctx.APIProps().pipelineType).lower():
                return self._analysis_envelope(code="unsupported_api", message="Vulkan capture required")
            vk = controller.GetVulkanPipelineState()
            state = {"remaining": 2048, "limit": limit, "truncated": False}
            pipeline = getattr(vk, "compute" if name == "CS" else "graphics")
            reflection = pipe.GetShaderReflection(stage)
            data = {"eid": eid, "stage": name, "descriptor_sets": _plain(pipeline.descriptorSets, state),
                    "shader": _plain(getattr(vk, {"VS": "vertexShader", "HS": "tessControlShader", "DS": "tessEvalShader", "GS": "geometryShader", "PS": "fragmentShader", "CS": "computeShader"}[name]), state),
                    "reflection": _plain(reflection, state), "complete": not state["truncated"]}
            return self._analysis_envelope(data, truncated=state["truncated"])
        return self._analysis_execute(collect, restore=True)

    def _analysis_pixel_diff(self, controller, eid, texture, max_pixels):
        width, height = int(texture.width), int(texture.height)
        if width * height > max_pixels:
            raise RuntimeError("Render target exceeds max_pixels; no pixel data was read")
        if int(getattr(texture, "depth", 1)) != 1 or int(getattr(texture, "arraysize", 1)) != 1:
            raise RuntimeError("Pixel diff requires a non-array 2D render target")
        if int(getattr(texture, "msSamp", 1)) > 1:
            raise RuntimeError("Multisample pixel diff is unsupported; no implicit sample selection")
        sub = rd.Subresource()
        sub.mip, sub.slice, sub.sample = 0, 0, 0
        fmt = texture.format
        if fmt.Special():
            # ElementSize alone is unsafe for compressed formats: it returns
            # the size of an entire block. Only known one-pixel layouts qualify.
            packed_sizes = {"R10G10B10A2": 4, "R11G11B10": 4, "R9G9B9E5": 4,
                            "R5G6B5": 2, "R5G5B5A1": 2, "R4G4B4A4": 2,
                            "R4G4": 1, "S8": 1, "A8": 1}
            kind = str(fmt.type).split(".")[-1]
            pixel_bytes = packed_sizes.get(kind)
            if pixel_bytes is None:
                raise RuntimeError("Compressed or unsupported packed render target pixel layout: " + kind)
            if callable(getattr(fmt, "ElementSize", None)) and int(fmt.ElementSize()) != pixel_bytes:
                raise RuntimeError("Packed render target element size disagrees with known format layout")
        else:
            pixel_bytes = int(fmt.compCount) * int(fmt.compByteWidth)
            if int(fmt.compCount) not in (1, 2, 3, 4) or int(fmt.compByteWidth) not in (1, 2, 4, 8):
                raise RuntimeError("Unknown render target pixel layout")
        controller.SetFrameEvent(eid - 1, False)
        self._analysis_check_fatal(controller)
        before = bytes(controller.GetTextureData(texture.resourceId, sub))
        self._analysis_check_fatal(controller)
        controller.SetFrameEvent(eid, False)
        self._analysis_check_fatal(controller)
        after = bytes(controller.GetTextureData(texture.resourceId, sub))
        self._analysis_check_fatal(controller)
        expected = width * height * pixel_bytes
        if len(before) != expected or len(after) != expected:
            raise RuntimeError("Texture read size mismatch: expected {}, got {}/{}".format(expected, len(before), len(after)))
        min_x, min_y, max_x, max_y, changed = width, height, -1, -1, 0
        for offset in range(0, expected, pixel_bytes):
            if before[offset:offset + pixel_bytes] != after[offset:offset + pixel_bytes]:
                index = offset // pixel_bytes
                x, y = index % width, index // width
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
                changed += 1
        return {"rid": str(texture.resourceId), "before_eid": eid - 1, "after_eid": eid,
                "width": width, "height": height, "changed_pixels": changed,
                "changed_fraction": changed / (width * height) if width * height else 0,
                "screen_bbox": [min_x, min_y, max_x + 1, max_y + 1] if changed else None,
                "bbox_max_exclusive": True, "coordinate_origin": "texture-row-zero",
                "format": str(fmt.Name()), "comparison": "exact raw pixel bytes", "interpretation": "Observed color change; occluded or equal-color writes are not detected"}

    def identify_drawcalls(self, params):
        def collect(controller):
            minimum, maximum, after, limit = self._analysis_range(params, 16, 128)
            max_pixels = _integer(params, "max_pixels", 1048576, 1, 4194304)
            include_diff = params.get("include_diff", True)
            if not isinstance(include_diff, bool):
                raise ValueError("include_diff must be boolean")
            dest = os.path.abspath(str(params["dest"])) if params.get("dest") else None
            if dest and os.path.exists(dest):
                return self._analysis_envelope(code="destination_exists", message=dest)
            candidates = [a for a in self._analysis_draws(controller) if minimum <= int(a.eventId) <= maximum and int(a.eventId) > after]
            items, errors = [], []
            texture_lookup = {_rid_key(t.resourceId): t for t in controller.GetTextures()} if include_diff else {}
            state = {"remaining": 4096, "limit": 32, "truncated": False}
            for action in candidates[:limit]:
                item = self._analysis_action(action)
                try:
                    controller.SetFrameEvent(item["eid"], False)
                    self._analysis_check_fatal(controller)
                    pipe = controller.GetPipelineState()
                    item["topology"] = str(pipe.GetPrimitiveTopology())
                    item["shaders"] = {name: str(pipe.GetShader(stage)) for name, stage in [self._analysis_stage(s) for s in ("VS", "HS", "DS", "GS", "PS")]}
                    outputs = list(pipe.GetOutputTargets())
                    item["outputs"] = _plain(outputs, state)
                    item["depth_target"] = _plain(pipe.GetDepthTarget(), state)
                    item["read_only_bindings"] = _plain(pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False), state)
                    labels = [token for token in ("shadow", "lighting", "depth", "transparent", "ui", "postprocess") if token in item["name"].lower()]
                    item["heuristic_labels"] = {"source": "action_name_substring", "labels": labels, "verified": False}
                    if include_diff:
                        rid = params.get("render_target")
                        target = texture_lookup.get(_rid_key(rid)) if rid is not None else next((texture_lookup.get(_rid_key(_resource(o))) for o in outputs if _valid_resource(_resource(o))), None)
                        if target is None:
                            raise RuntimeError("No readable color render target found")
                        item["pixel_diff"] = self._analysis_pixel_diff(controller, item["eid"], target, max_pixels)
                except _ReplayFatalError:
                    raise
                except Exception as exc:
                    item["error"] = str(exc)
                    errors.append({"eid": item["eid"], "error": str(exc)})
                items.append(item)
            truncated = len(candidates) > limit or state["truncated"]
            data = {"items": items, "count": len(items), "total": len(candidates), "errors": errors,
                    "next_after_eid": items[-1]["eid"] if len(candidates) > limit and items else None,
                    "complete": not truncated and not errors, "include_diff": include_diff}
            if dest:
                parent = os.path.dirname(dest)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(dest, "x", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, allow_nan=False)
                    stream.write("\n")
                data["path"] = dest
            return self._analysis_envelope(data, truncated=truncated)
        return self._analysis_execute(collect, restore=True)
