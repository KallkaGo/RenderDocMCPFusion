"""Bounded inline replay queries compatible with the reference bridge.

Reference: RenderDocMCP/renderdoc_mcp_bridge/__init__.py.
Copyright (c) 2026 StellaAstra

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import base64
import math

import renderdoc as rd

from .inventory import _plain


MAX_QUERY_BYTES = 1024 * 1024
MAX_QUERY_ITEMS = 1000


def _json(value):
    """Detach replay-owned values and allow strict JSON, even for NaN pixels."""
    def finite(item):
        if isinstance(item, float) and not math.isfinite(item):
            return str(item)
        if isinstance(item, dict):
            return {key: finite(val) for key, val in item.items()}
        if isinstance(item, list):
            return [finite(val) for val in item]
        return item
    return finite(_plain(value))


def _integer(params, name, default=None, maximum=0xffffffff, minimum=0):
    value = params.get(name, default)
    if value is None or isinstance(value, bool):
        raise ValueError("{} is required and must be an integer".format(name))
    # Do not silently round coordinates or buffer sizes.
    result = int(value)
    if isinstance(value, float) and result != value:
        raise ValueError("{} must be an integer".format(name))
    if not minimum <= result <= maximum:
        raise ValueError("{} must be between {} and {}".format(name, minimum, maximum))
    return result


def _page(params, values, serialize):
    offset = _integer(params, "offset", 0)
    limit = _integer(params, "limit", 100, MAX_QUERY_ITEMS, 1)
    total = len(values)
    end = min(total, offset + limit)
    return {"items": [serialize(values[i]) for i in range(offset, end)],
            "offset": offset, "limit": limit, "total": total,
            "next_offset": end if end < total else None}, end < total


def _rid_key(value):
    text = str(value)
    return text.split("::", 1)[-1] if text.startswith("ResourceId::") else text


class QueryAPIServiceMixin:
    def _query(self, params, operation, event=False):
        if not self.ctx.IsCaptureLoaded():
            return self._no_capture()
        result = {}

        def collect(controller):
            try:
                if event:
                    controller.SetFrameEvent(self._query_eid(controller, params), True)
                data, truncated = operation(controller)
                result.update({"ok": True, "mode": "summary", "data": data, "err": None,
                               "meta": {"cap": "active", "truncated": bool(truncated)}})
            except (ValueError, TypeError, KeyError) as exc:
                result.update(self._query_error("invalid_query", str(exc)))
            except Exception as exc:
                result.update(self._query_error("query_failed", str(exc)))

        self.ctx.Replay().BlockInvoke(collect)
        return result

    @staticmethod
    def _query_eid(controller, params):
        eid = _integer(params, "eid", minimum=1)
        # Native SetFrameEvent returns no status and forwards arbitrary IDs to
        # the driver. Validate against actual actions/API events first so a
        # misspelled event cannot produce evidence labeled with a wrong eid.
        pending = list(controller.GetRootActions())
        while pending:
            action = pending.pop()
            if int(action.eventId) == eid or any(int(item.eventId) == eid for item in action.events):
                return eid
            pending.extend(action.children)
        raise ValueError("Event not found in capture: {}".format(eid))

    @staticmethod
    def _query_error(code, message):
        return {"ok": False, "mode": "summary", "data": None,
                "err": {"code": code, "msg": message},
                "meta": {"cap": "active", "truncated": False}}

    @staticmethod
    def _query_resource(controller, params, kind):
        wanted = params.get("rid", params.get("resource_id"))
        if wanted is None:
            raise ValueError("rid is required")
        items = controller.GetTextures() if kind == "texture" else controller.GetBuffers()
        for item in items:
            if _rid_key(item.resourceId) == _rid_key(wanted):
                return item
        raise ValueError("{} not found: {}".format(kind, wanted))

    @staticmethod
    def _texture_record(texture, names):
        return {"rid": str(texture.resourceId), "name": names.get(str(texture.resourceId), ""),
                "width": int(texture.width), "height": int(texture.height), "depth": int(texture.depth),
                "format": texture.format.Name(), "mips": int(texture.mips),
                "arraysize": int(texture.arraysize), "ms_samples": int(texture.msSamp),
                "byte_size": int(texture.byteSize), "type": str(texture.type)}

    @staticmethod
    def _query_names(controller):
        return {str(item.resourceId): str(item.name) for item in controller.GetResources()}

    def get_textures(self, params):
        def collect(controller):
            names = self._query_names(controller)
            return _page(params, controller.GetTextures(), lambda t: self._texture_record(t, names))
        return self._query(params, collect)

    def get_buffers(self, params):
        def collect(controller):
            names = self._query_names(controller)
            return _page(params, controller.GetBuffers(), lambda b: {
                "rid": str(b.resourceId), "name": names.get(str(b.resourceId), ""),
                "length": int(b.length), "creation_flags": str(b.creationFlags)})
        return self._query(params, collect)

    def get_resources(self, params):
        return self._query(params, lambda c: _page(params, c.GetResources(), lambda r: {
            "rid": str(r.resourceId), "name": str(r.name), "type": str(r.type),
            "autogenerated_name": bool(getattr(r, "autogeneratedName", False))}))

    def get_texture_info(self, params):
        return self._query(params, lambda c: (
            self._texture_record(self._query_resource(c, params, "texture"), self._query_names(c)), False))

    @staticmethod
    def _query_subresource(params, texture):
        sub = rd.Subresource()
        sub.mip = _integer(params, "mip", 0)
        sub.slice = _integer(params, "slice_index", params.get("slice", 0))
        sub.sample = _integer(params, "sample", 0)
        depth = max(1, int(texture.depth) >> sub.mip)
        if sub.mip >= int(texture.mips) or sub.slice >= max(int(texture.arraysize), depth):
            raise ValueError("Subresource is outside texture mip/slice range")
        if sub.sample >= max(1, int(texture.msSamp)):
            raise ValueError("sample is outside texture sample range")
        return sub

    @staticmethod
    def _query_cast(params):
        name = str(params.get("type_cast", "Typeless"))
        allowed = ("Typeless", "Float", "UNorm", "SNorm", "UInt", "SInt", "Depth", "UNormSRGB")
        if name not in allowed or not hasattr(rd.CompType, name):
            raise ValueError("Unsupported type_cast: " + name)
        return getattr(rd.CompType, name)

    def get_texture_data(self, params):
        def collect(controller):
            budget = _integer(params, "max_bytes", 4096, MAX_QUERY_BYTES, 1)
            texture = self._query_resource(controller, params, "texture")
            sub = self._query_subresource(params, texture)
            if int(texture.depth) > 1 and sub.slice != 0:
                raise ValueError("3D texture byte reads return the whole mip; slice_index must be 0")
            # Native GetTextureData cannot request a byte range. Cap the response
            # after reading this single subresource, never cut an encoded string.
            data = bytes(controller.GetTextureData(texture.resourceId, sub))
            returned = data[:budget]
            return {"rid": str(texture.resourceId), "eid": int(params["eid"]),
                    "mip": sub.mip, "slice_index": sub.slice, "sample": sub.sample,
                    "length": len(data), "returned_bytes": len(returned),
                    "base64": base64.b64encode(returned).decode("ascii")}, len(returned) < len(data)
        return self._query(params, collect, event=True)

    def get_buffer_contents(self, params):
        def collect(controller):
            offset = _integer(params, "offset", 0, 0xffffffffffffffff)
            length = _integer(params, "length", 4096, MAX_QUERY_BYTES, 1)
            budget = _integer(params, "max_bytes", length, MAX_QUERY_BYTES, 1)
            buffer = self._query_resource(controller, params, "buffer")
            if offset > int(buffer.length):
                raise ValueError("offset exceeds buffer length")
            available = max(0, int(buffer.length) - offset)
            requested = min(length, available)
            read_length = min(requested, budget)
            # A zero native length means read all remaining bytes; never use it.
            data = bytes(controller.GetBufferData(buffer.resourceId, offset, read_length)) if read_length else b""
            data = data[:read_length]
            return {"rid": str(buffer.resourceId), "eid": int(params["eid"]), "offset": offset,
                    "requested_length": length, "length": len(data), "returned_bytes": len(data),
                    "buffer_length": int(buffer.length), "base64": base64.b64encode(data).decode("ascii"),
                    "hex": data[:512].hex()}, len(data) < requested
        return self._query(params, collect, event=True)

    def _query_pixel(self, controller, params):
        texture = self._query_resource(controller, params, "texture")
        sub = self._query_subresource(params, texture)
        x, y = _integer(params, "x"), _integer(params, "y")
        if x >= max(1, int(texture.width) >> sub.mip) or y >= max(1, int(texture.height) >> sub.mip):
            raise ValueError("Pixel coordinate is outside texture mip")
        return texture, sub, x, y, self._query_cast(params)

    def pick_pixel(self, params):
        def collect(controller):
            texture, sub, x, y, cast = self._query_pixel(controller, params)
            return {"rid": str(texture.resourceId), "x": x, "y": y,
                    "value": _json(controller.PickPixel(texture.resourceId, x, y, sub, cast))}, False
        return self._query(params, collect, event=True)

    def get_texture_minmax(self, params):
        def collect(controller):
            texture = self._query_resource(controller, params, "texture")
            sub = self._query_subresource(params, texture)
            minimum, maximum = controller.GetMinMax(texture.resourceId, sub, self._query_cast(params))
            return {"rid": str(texture.resourceId), "min": _json(minimum), "max": _json(maximum)}, False
        return self._query(params, collect, event=True)

    def pixel_history(self, params):
        def collect(controller):
            texture, sub, x, y, cast = self._query_pixel(controller, params)
            history = controller.PixelHistory(texture.resourceId, x, y, sub, cast)
            # History can include later writes; eid is the requested upper bound.
            history = [item for item in history if int(item.eventId) <= int(params["eid"])]
            page, truncated = _page(params, history, _json)
            page.update({"rid": str(texture.resourceId), "x": x, "y": y})
            return page, truncated
        return self._query(params, collect, event=True)

    def _query_debug(self, controller, params, start):
        maximum = _integer(params, "max_steps", 50, MAX_QUERY_ITEMS, 1)
        trace = None
        try:
            trace = start()
            if trace is None or not trace.debugger:
                raise RuntimeError("Shader debugger unavailable for this invocation")
            result = {"eid": int(params["eid"]), "complete": False, "states": [],
                      "details_truncated": False}
            for name in ("inputs", "constantBlocks", "readOnlyResources", "readWriteResources", "samplers"):
                values = list(getattr(trace, name, []))
                result["details_truncated"] |= len(values) > MAX_QUERY_ITEMS
                result[name] = [_json(value) for value in values[:MAX_QUERY_ITEMS]]
            while True:
                batch = controller.ContinueDebug(trace.debugger)
                if not batch:
                    result["complete"] = True
                    break
                remaining = maximum - len(result["states"])
                for state in list(batch)[:remaining]:
                    callstack = list(getattr(state, "callstack", []))
                    source_variables = list(getattr(state, "sourceVars", []))
                    changes = list(getattr(state, "changes", []))
                    details_truncated = any(len(values) > 100 for values in (callstack, source_variables, changes))
                    result["details_truncated"] |= details_truncated
                    result["states"].append({"step": int(state.stepIndex),
                        "next_instruction": int(getattr(state, "nextInstruction", 0)),
                        "flags": str(getattr(state, "flags", "")),
                        "callstack": _json(callstack[:100]),
                        "source_variables": _json(source_variables[:100]),
                        "changes": _json(changes[:100]),
                        "details_truncated": details_truncated})
                if len(batch) > remaining:
                    break
                # An extra ContinueDebug at the exact limit distinguishes a
                # complete trace from a trace stopped before its final state.
                if len(result["states"]) >= maximum:
                    result["complete"] = not bool(controller.ContinueDebug(trace.debugger))
                    break
            result["steps"] = len(result["states"])
            return result, not result["complete"] or result["details_truncated"]
        finally:
            if trace is not None:
                controller.FreeTrace(trace)

    def debug_pixel(self, params):
        def collect(controller):
            x, y = _integer(params, "x"), _integer(params, "y")
            inputs = rd.DebugPixelInputs()
            inputs.sample = _integer(params, "sample", 0)
            inputs.primitive = _integer(params, "primitive", 0xffffffff)
            result, truncated = self._query_debug(controller, params, lambda: controller.DebugPixel(x, y, inputs))
            result.update({"x": x, "y": y, "sample": inputs.sample, "primitive": inputs.primitive})
            return result, truncated
        return self._query(params, collect, event=True)

    def debug_vertex(self, params):
        def collect(controller):
            vertex = _integer(params, "vertex_id")
            instance = _integer(params, "instance_id", 0)
            index, view = _integer(params, "index", 0), _integer(params, "view", 0)
            result, truncated = self._query_debug(controller, params,
                lambda: controller.DebugVertex(vertex, instance, index, view))
            result.update({"vertex_id": vertex, "instance_id": instance, "index": index, "view": view})
            return result, truncated
        return self._query(params, collect, event=True)

    def get_post_vs_data(self, params):
        def collect(controller):
            instance = _integer(params, "instance_id", 0)
            view = _integer(params, "view", 0)
            stage_name = str(params.get("stage", "VSOut"))
            if stage_name not in ("VSOut", "GSOut") or not hasattr(rd.MeshDataStage, stage_name):
                raise ValueError("stage must be VSOut or GSOut")
            mesh = controller.GetPostVSData(instance, view, getattr(rd.MeshDataStage, stage_name))
            return {"eid": int(params["eid"]), "instance_id": instance, "view": view,
                    "stage": stage_name, "mesh": _json(mesh)}, False
        return self._query(params, collect, event=True)

    def get_debug_messages(self, params):
        return self._query(params, lambda c: _page(params, c.GetDebugMessages(), _json))
