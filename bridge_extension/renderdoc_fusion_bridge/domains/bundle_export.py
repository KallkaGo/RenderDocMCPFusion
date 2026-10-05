"""Fresh-directory draw bundles and an explicit Unity editor import helper.

The bundle workflow follows the export ideas in RenderDocMCP's MIT-licensed
renderdoc_mcp_bridge/__init__.py. Implementation uses Fusion's replay services;
no legacy IPC, global replay controller, or external mesh converter is used.
"""

import hashlib
import json
import math
import os
import re
import shutil
import struct
import tempfile

import renderdoc as rd

from .inventory import binding_location, _plain


class BundleExportServiceMixin:
    @staticmethod
    def _bundle_plain(value):
        def finite(item):
            if isinstance(item, float) and not math.isfinite(item):
                return str(item)
            if isinstance(item, dict):
                return {key: finite(val) for key, val in item.items()}
            if isinstance(item, list):
                return [finite(val) for val in item]
            return item
        return finite(_plain(value))

    @staticmethod
    def _bundle_reply(data=None, error=None, code="bundle_export_failed"):
        return {"ok": error is None, "mode": "summary", "data": data,
                "err": None if error is None else {"code": code, "msg": str(error)},
                "meta": {"cap": "active", "truncated": False}}

    @staticmethod
    def _bundle_child(root, name):
        # All asset names are generated locally. Never trust capture filenames.
        if not name or os.path.isabs(name) or ".." in name.replace("\\", "/").split("/"):
            raise ValueError("Unsafe bundle child path")
        root = os.path.realpath(root)
        path = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath([root, path]) != root or path == root:
            raise ValueError("Bundle child path escapes its directory")
        return path

    @staticmethod
    def _bundle_name(name):
        return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "asset"))[:100].strip(".") or "asset"

    @staticmethod
    def _bundle_valid_rid(rid):
        return rid is not None and str(rid) not in ("", "0", "ResourceId::0") and "Null" not in str(rid)

    def _bundle_write(self, root, name, content):
        path = self._bundle_child(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        return path

    def _bundle_publish(self, root, name, source):
        path = self._bundle_child(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Existing exporters may use write mode. They write to private staging;
        # final publication is exclusive even if a child file appeared meanwhile.
        with open(source, "rb") as incoming, open(path, "xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        return path

    @staticmethod
    def _bundle_role(name):
        text = str(name or "").lower()
        roles = (("normal", ("normal", "bump")), ("albedo", ("albedo", "basecolor", "base_color", "diffuse")),
                 ("emission", ("emission", "emissive")), ("occlusion", ("occlusion", "ambient_occlusion")),
                 ("metallic", ("metallic", "metalness")))
        for role, terms in roles:
            if any(term in text for term in terms):
                return role
        return "unknown"

    def _bundle_texture_bindings(self, controller, pipe, stage_name, stage_enum, errors=None):
        textures = {str(tex.resourceId): tex for tex in controller.GetTextures()}
        refl = pipe.GetShaderReflection(stage_enum)
        result = []
        for usage, getter, reflection_key in (("srv", "GetReadOnlyResources", "readOnlyResources"),
                                               ("uav", "GetReadWriteResources", "readWriteResources")):
            reflected = list(getattr(refl, reflection_key, []) or [])
            method = getattr(pipe, getter, None)
            if not callable(method):
                if errors is not None:
                    errors.append("{} {} bindings: accessor unavailable".format(stage_name, usage))
                continue
            try:
                try:
                    bindings = method(stage_enum, False)
                except TypeError:
                    bindings = method(stage_enum)
            except Exception as exc:
                if errors is not None:
                    errors.append("{} {} bindings: {}".format(stage_name, usage, exc))
                continue
            for binding in bindings or []:
                location = binding_location(binding, reflected)
                # Older RenderDoc versions return BoundResourceArray objects.
                resources = list(getattr(binding, "resources", []) or []) or [binding]
                for array_index, item in enumerate(resources):
                    descriptor = getattr(item, "descriptor", item)
                    rid = getattr(descriptor, "resource", getattr(descriptor, "resourceId", None))
                    tex = textures.get(str(rid))
                    if tex is None:
                        continue
                    name = location.get("name") or ""
                    if not name and location.get("slot") is not None:
                        declaration = next((r for r in reflected if getattr(r, "fixedBindNumber", None) == location["slot"]), None)
                        name = getattr(declaration, "name", "")
                    resource_name = self._resource_display_name(rid) or str(rid)
                    loc = dict(location)
                    if resources != [binding]:
                        loc["array_element"] = array_index
                    result.append({"stage": stage_name, "usage": usage, "rid": str(rid),
                                   "slot": loc["slot"], "location": loc, "name": name,
                                   "resource_name": resource_name, "width": int(tex.width), "height": int(tex.height),
                                   "depth": int(getattr(tex, "depth", 1)), "mips": int(getattr(tex, "mips", 1)),
                                   "array_size": int(getattr(tex, "arraysize", 1)),
                                   "samples": int(getattr(tex, "msSamp", 1)), "format": tex.format.Name(),
                                   "role": self._bundle_role(name + " " + resource_name), "role_inferred": True})
        return result

    def get_bound_textures(self, params):
        if not self.ctx.IsCaptureLoaded():
            return self._no_capture()
        try:
            eid = int(params["eid"])
            stage = str(params.get("stage", "ps")).lower()
            stage_enum = self._stage_enum_from_name(stage)
            if stage_enum is None:
                return self._bundle_reply(error="Unsupported stage", code="bad_stage")
            if self.ctx.GetAction(eid) is None:
                return self._bundle_reply(error="Event not found", code="bad_event")
            result = []
            errors = []
            def collect(controller):
                controller.SetFrameEvent(eid, True)
                result.extend(self._bundle_texture_bindings(controller, controller.GetPipelineState(), stage, stage_enum, errors))
            self.ctx.Replay().BlockInvoke(collect)
            return self._bundle_reply({"eid": eid, "stage": stage, "textures": result, "errors": errors, "complete": not errors})
        except Exception as exc:
            return self._bundle_reply(error=exc, code="bound_textures_failed")

    def export_drawcall(self, params):
        return self._export_bundle(params, unity=False)

    def export_to_unity(self, params):
        return self._export_bundle(params, unity=True)

    def _export_bundle(self, params, unity):
        if not self.ctx.IsCaptureLoaded():
            return self._no_capture()
        result = None
        try:
            if params.get("eid") is None or not params.get("dest"):
                return self._bundle_reply(error="eid and explicit dest directory are required", code="missing_args")
            eid = int(params["eid"])
            if self.ctx.GetAction(eid) is None:
                return self._bundle_reply(error="Event not found", code="bad_event")
            root = os.path.abspath(str(params["dest"]))
            # No exist_ok: destination, including an existing empty directory,
            # must be created exclusively by this call.
            os.makedirs(root, exist_ok=False)
            result = {"schema_version": 1, "eid": eid, "path": root,
                      "kind": "unity" if unity else "drawcall", "files": [], "shaders": [],
                      "textures": [], "pipeline": None, "mesh": None, "warnings": [], "errors": [],
                      "complete": False, "limitations": [
                          "OBJ contains captured VS input geometry, not an original authored or rigged mesh.",
                          "Skinning, animation, transforms, instancing and shader effects are not reconstructed.",
                          "Texture roles are name-based suggestions; the original material is not reconstructed."]}
            with tempfile.TemporaryDirectory(prefix=".bundle-staging-", dir=root) as staging:
                def collect(controller):
                    controller.SetFrameEvent(eid, True)
                    pipe = controller.GetPipelineState()
                    self._bundle_pipeline(controller, pipe, root, result)
                    seen = {}
                    for stage_name, stage_enum in self._binding_stages():
                        short = stage_name.lower()
                        try:
                            shader = pipe.GetShader(stage_enum)
                            if not self._bundle_valid_rid(shader):
                                continue
                            self._bundle_shader(controller, pipe, short, stage_enum, shader, root, result)
                        except Exception as exc:
                            result["errors"].append("{} shader: {}".format(short, exc))
                        try:
                            for texture in self._bundle_texture_bindings(controller, pipe, short, stage_enum, result["errors"]):
                                rid = texture["rid"]
                                if rid not in seen:
                                    ext = "png" if unity else "dds"
                                    name = "textures/texture_{:04d}.{}".format(len(seen), ext)
                                    source = os.path.join(staging, os.path.basename(name))
                                    saved = self._save_texture_resource(controller, rid, ext.upper(), source, eid, False, "bundle")
                                    if saved.get("path"):
                                        path = self._bundle_publish(root, name, saved["path"])
                                        result["files"].append(path)
                                        seen[rid] = path
                                    else:
                                        seen[rid] = None
                                        result["errors"].append("Texture {}: {}".format(rid, saved.get("error")))
                                texture["path"] = seen[rid]
                                texture["export_format"] = "PNG" if unity else "DDS"
                                if unity:
                                    texture["subresource"] = {"mip": 0, "slice": 0}
                                    if texture["array_size"] > 1 or texture["depth"] > 1 or texture["samples"] > 1:
                                        result["warnings"].append("Texture {} PNG is a mip0/slice0 preview; array/cube/volume/MSAA data is not fully exported.".format(rid))
                                result["textures"].append(texture)
                        except Exception as exc:
                            result["errors"].append("{} texture bindings: {}".format(short, exc))
                self.ctx.Replay().BlockInvoke(collect)
                mesh = self.export_mesh({"eid": eid, "dest": os.path.join(staging, "mesh.obj"), "overwrite": False})
                if not mesh.get("ok"):
                    original_error = (mesh.get("err") or {}).get("msg", "VS input export failed")
                    mesh = self._bundle_postvs_mesh(eid, os.path.join(staging, "postvs.obj"))
                    if mesh.get("ok"):
                        result["warnings"].append("VS input mesh unavailable: {}. Exported a post-transform preview instead.".format(original_error))
                        result["limitations"][0] = "OBJ contains a post-transform VS output preview, not original authored, object-space or rigged geometry."
                    else:
                        mesh["err"]["msg"] = "VS input: {}; post-transform fallback: {}".format(original_error, mesh["err"]["msg"])
                if mesh.get("ok"):
                    metadata = dict(mesh["data"])
                    metadata["path"] = self._bundle_publish(root, "mesh.obj", metadata["path"])
                    with open(metadata["path"], "r", encoding="utf-8") as stream:
                        has_uvs = any(line.startswith("vt ") for line in stream)
                    metadata["has_uvs"] = has_uvs
                    if not has_uvs:
                        result["warnings"].append("Mesh has no exported UV coordinates; texture placement must be supplied manually.")
                    result["warnings"].extend(metadata.get("warnings", []))
                    result["mesh"] = metadata
                    result["files"].append(metadata["path"])
                else:
                    result["errors"].append("Mesh: {}".format((mesh.get("err") or {}).get("msg", "Export failed")))
            if unity:
                self._bundle_unity(root, result)
            result["complete"] = not result["errors"]
            manifest = self._bundle_child(root, "manifest.json")
            result["manifest"] = manifest
            result["files"].append(manifest)
            result["assets"] = [self._bundle_asset_record(root, path) for path in result["files"] if path != manifest]
            self._bundle_write(root, "manifest.json", json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            return self._bundle_reply(result)
        except Exception as exc:
            # Preserve partial assets for inspection and return their location.
            return self._bundle_reply(result, exc)

    @staticmethod
    def _bundle_asset_record(root, path):
        digest = hashlib.sha256()
        length = 0
        with open(path, "rb") as stream:
            for content in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(content)
                length += len(content)
        return {"path": os.path.relpath(path, root).replace("\\", "/"),
                "bytes": length, "sha256": digest.hexdigest()}

    def _bundle_postvs_mesh(self, eid, path):
        """Decode a supported replay POSITION stream, with explicit preview scope."""
        result = {}
        def collect(controller):
            try:
                controller.SetFrameEvent(eid, True)
                mesh = controller.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
                if mesh.topology != rd.Topology.TriangleList:
                    raise ValueError("PostVS OBJ preview requires TriangleList")
                fmt = mesh.format
                special = getattr(fmt, "Special", None)
                if (fmt.compType != rd.CompType.Float or int(fmt.compByteWidth) != 4 or int(fmt.compCount) != 4
                        or (callable(special) and special())):
                    raise ValueError("PostVS OBJ preview requires unpacked float32x4 POSITION")
                count = int(mesh.numIndices)
                stride = int(mesh.vertexByteStride)
                offset = int(mesh.vertexByteOffset)
                if not 0 < count <= 3000000 or stride < 16 or offset < 0 or not self._bundle_valid_rid(mesh.vertexResourceId):
                    raise ValueError("Invalid or oversized postVS vertex stream")
                budget = 256 * 1024 * 1024
                index_stride = int(mesh.indexByteStride)
                indexed = self._bundle_valid_rid(mesh.indexResourceId)
                if indexed:
                    if index_stride not in (1, 2, 4):
                        raise ValueError("Unsupported postVS index width")
                    index_offset = int(mesh.indexByteOffset)
                    index_size = count * index_stride
                    if index_offset < 0 or index_offset + index_size > 0xffffffffffffffff or index_size > budget:
                        raise ValueError("Invalid postVS index extent")
                    declared = int(getattr(mesh, "indexByteSize", 0xffffffffffffffff))
                    if declared != 0xffffffffffffffff and index_size > declared:
                        raise ValueError("PostVS index extent exceeds MeshFormat byte size")
                    raw = bytes(controller.GetBufferData(mesh.indexResourceId, index_offset, index_size))
                    if len(raw) != index_size:
                        raise ValueError("Incomplete postVS index buffer")
                    indices = list(struct.unpack("<{}{}".format(count, {1: "B", 2: "H", 4: "I"}[index_stride]), raw))
                else:
                    indices = list(range(count))
                # A restart begins a new primitive segment. Never bridge faces
                # across a restart, even when a preceding segment is incomplete.
                restart = int(getattr(mesh, "restartIndex", 0xffffffff))
                if indexed:
                    restart &= (1 << (index_stride * 8)) - 1
                base = int(getattr(mesh, "baseVertex", 0)) if indexed else 0
                segments, segment = [], []
                for index in indices:
                    if indexed and bool(getattr(mesh, "allowRestart", False)) and index == restart:
                        segments.append(segment)
                        segment = []
                    else:
                        index += base
                        if index < 0:
                            raise ValueError("Negative postVS vertex index after baseVertex")
                        segment.append(index)
                segments.append(segment)
                warnings = []
                trailing = sum(len(segment) % 3 for segment in segments)
                triangles = [tuple(segment[n:n + 3]) for segment in segments for n in range(0, len(segment) - 2, 3)]
                if not triangles:
                    raise ValueError("No complete postVS triangles")
                if trailing:
                    warnings.append("Ignored {} trailing indices across restart segments".format(trailing))
                unique = list(dict.fromkeys(index for triangle in triangles for index in triangle))
                lowest, highest = min(unique), max(unique)
                read_offset = offset + lowest * stride
                read_size = (highest - lowest) * stride + 16
                if read_size > budget or read_offset > 0xffffffffffffffff or read_offset + read_size > 0xffffffffffffffff:
                    raise ValueError("PostVS preview buffer range exceeds 256 MiB budget or integer bounds")
                declared = int(getattr(mesh, "vertexByteSize", 0xffffffffffffffff))
                if declared != 0xffffffffffffffff and highest * stride + 16 > declared:
                    raise ValueError("PostVS vertex extent exceeds MeshFormat byte size")
                raw = bytes(controller.GetBufferData(mesh.vertexResourceId, read_offset, read_size))
                if len(raw) != read_size:
                    raise ValueError("Incomplete postVS vertex buffer")
                divide = bool(getattr(mesh, "unproject", False))
                positions = []
                for index in unique:
                    value = struct.unpack_from("<4f", raw, (index - lowest) * stride)
                    if not all(math.isfinite(component) for component in value) or (divide and value[3] == 0):
                        raise ValueError("Non-finite postVS POSITION or zero clip-space w")
                    position = tuple(component / value[3] for component in value[:3]) if divide else value[:3]
                    positions.append(position)
                index_map = {index: n + 1 for n, index in enumerate(unique)}
                with open(path, "x", encoding="utf-8", newline="\n") as obj:
                    obj.write("# PostVS first-instance preview; coordinate space: {}\n".format("NDC" if divide else "post_vs_xyz"))
                    obj.write("o postvs_preview_eid{}\n".format(eid))
                    for position in positions:
                        obj.write("v {:.9g} {:.9g} {:.9g}\n".format(*position))
                    for triangle in triangles:
                        obj.write("f {} {} {}\n".format(*(index_map[index] for index in triangle)))
                warnings.extend(["Post-transform VSOut preview for instance 0/view 0; later shader stages, other instances and world transforms are not reconstructed.",
                                 "Only POSITION was decoded; UV coordinates, normals, skinning and original mesh semantics are unavailable.",
                                 "Preview coordinates are {}. Review Unity scale, orientation and placement manually.".format("NDC after homogeneous division" if divide else "raw postVS xyz; w was omitted")])
                result["data"] = {"eid": eid, "path": path, "stage": "vsout", "fallback": True,
                                  "coordinate_space": "ndc" if divide else "post_vs_xyz", "instance": 0, "view": 0,
                                  "topology": "TriangleList", "indexed": indexed, "vertices": len(unique),
                                  "indices": len(triangles) * 3, "triangles": len(triangles), "has_uvs": False,
                                  "attributes": ["POSITION"], "warnings": warnings, "mesh_format": self._bundle_plain(mesh)}
            except Exception as exc:
                result["error"] = str(exc)
        self.ctx.Replay().BlockInvoke(collect)
        return self._bundle_reply(result.get("data"), result.get("error"), "postvs_mesh_failed")

    def _bundle_shader(self, controller, pipe, short, stage_enum, shader, root, result):
        refl = pipe.GetShaderReflection(stage_enum)
        info = {"stage": short, "rid": str(shader), "entry": str(pipe.GetShaderEntryPoint(stage_enum)), "files": []}
        sources = self._source_files(getattr(refl, "debugInfo", None))
        for index, source in enumerate(sources):
            name = "shaders/{}_source_{:03d}_{}.txt".format(short, index, self._bundle_name(source.get("filename")))
            path = self._bundle_write(root, name, source["text"])
            info["files"].append({"kind": "source", "original_name": source.get("filename"), "path": path})
            result["files"].append(path)
        disasm = self._shader_disasm(controller, pipe, stage_enum, refl)
        if disasm.get("text"):
            path = self._bundle_write(root, "shaders/{}_disassembly.txt".format(short), disasm["text"])
            info["files"].append({"kind": "disassembly", "target": disasm.get("target"), "path": path})
            result["files"].append(path)
        elif not sources:
            result["errors"].append("{} shader text unavailable: {}".format(short, disasm.get("error")))
        result["shaders"].append(info)

    def _bundle_pipeline(self, controller, pipe, root, result):
        snapshot = {"eid": result["eid"], "api": str(self.ctx.APIProps().pipelineType), "state": {}, "unsupported": {}}
        getters = ("GetPrimitiveTopology", "GetVertexInputs", "GetVBuffers", "GetIBuffer", "GetOutputTargets",
                   "GetDepthTarget", "GetViewports", "GetScissors")
        for name in getters:
            try:
                snapshot["state"][name] = self._bundle_plain(getattr(pipe, name)())
            except Exception as exc:
                snapshot["unsupported"][name] = str(exc)
        for plural, singular in (("GetViewports", "GetViewport"), ("GetScissors", "GetScissor")):
            if plural in snapshot["unsupported"] and callable(getattr(pipe, singular, None)):
                try:
                    snapshot["state"][singular + "(0)"] = self._bundle_plain(getattr(pipe, singular)(0))
                except Exception as exc:
                    snapshot["unsupported"][singular + "(0)"] = str(exc)
        api = snapshot["api"].split(".")[-1]
        raw_getter = {"D3D11": "GetD3D11PipelineState", "D3D12": "GetD3D12PipelineState",
                      "Vulkan": "GetVulkanPipelineState", "OpenGL": "GetGLPipelineState"}.get(api)
        if raw_getter:
            try:
                snapshot["api_state"] = self._bundle_plain(getattr(controller, raw_getter)())
            except Exception as exc:
                snapshot["unsupported"][raw_getter] = str(exc)
        path = self._bundle_write(root, "pipeline.json", json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        result["pipeline"] = path
        result["files"].append(path)

    def _bundle_unity(self, root, result):
        property_map = {"albedo": "_MainTex", "normal": "_BumpMap", "metallic": "_MetallicGlossMap",
                        "occlusion": "_OcclusionMap", "emission": "_EmissionMap"}
        properties, assigned = [], set()
        for texture in result["textures"]:
            prop = property_map.get(texture["role"])
            if prop and prop not in assigned and texture["path"] and texture["usage"] == "srv":
                properties.append({"property": prop, "role": texture["role"], "path": os.path.relpath(texture["path"], root).replace("\\", "/")})
                assigned.add(prop)
        material = {"schema_version": 1, "shader": "Standard", "properties": properties,
                    "mesh": "mesh.obj" if result["mesh"] else "", "eid": result["eid"],
                    "notes": "Suggested texture mapping only. Verify roles, color space, coordinates, scale and UVs in Unity."}
        result["material"] = self._bundle_write(root, "material.json", json.dumps(material, indent=2, allow_nan=False) + "\n")
        # Multiple bundles in one project must not define duplicate C# classes
        # or menu entries. The identifier stays stable for this output directory.
        identifier = hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
        menu = "Tools/RenderDoc/Import Bundle {} {}".format(result["eid"], identifier)
        script = _UNITY_IMPORTER.replace("namespace RenderDocFusion {", "namespace RenderDocFusion.Bundle" + identifier + " {")
        script = script.replace("Tools/RenderDoc/Import Selected Bundle", menu)
        result["import_script"] = self._bundle_write(root, "Editor/RenderDocBundleImporter.cs", script)
        result["files"].extend([result["material"], result["import_script"]])
        result["warnings"].append("Copy the bundle under Unity Assets, select material.json, and run {}. Texture property mappings require review.".format(menu))


_UNITY_IMPORTER = '''// Generated RenderDoc bundle helper. Run explicitly after reviewing material.json.
#if UNITY_EDITOR
using System;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace RenderDocFusion {
public static class BundleImporter {
    [Serializable] private class Property { public string property; public string role; public string path; }
    [Serializable] private class Definition { public string shader; public Property[] properties; public string mesh; }
    [MenuItem("Tools/RenderDoc/Import Selected Bundle")]
    private static void Import() {
        var selected = Selection.activeObject as TextAsset;
        var file = selected == null ? "" : AssetDatabase.GetAssetPath(selected);
        if (Path.GetFileName(file) != "material.json") throw new InvalidOperationException("Select the bundle material.json in Assets.");
        var folder = Path.GetDirectoryName(file).Replace('\\\\', '/');
        var definition = JsonUtility.FromJson<Definition>(selected.text);
        var shader = Shader.Find(definition.shader);
        if (shader == null) throw new InvalidOperationException("The suggested shader is unavailable. Edit material.json for your render pipeline.");
        var materialPath = AssetDatabase.GenerateUniqueAssetPath(folder + "/RenderDocMaterial.mat");
        var material = new Material(shader);
        foreach (var property in definition.properties ?? new Property[0]) {
            // Only local relative paths are accepted from the editable definition.
            if (Path.IsPathRooted(property.path) || property.path.Contains("..")) throw new InvalidOperationException("Unsafe texture path.");
            var path = folder + "/" + property.path;
            if (property.role == "normal") {
                var importer = AssetImporter.GetAtPath(path) as TextureImporter;
                if (importer != null) { importer.textureType = TextureImporterType.NormalMap; importer.SaveAndReimport(); }
            }
            var texture = AssetDatabase.LoadAssetAtPath<Texture2D>(path);
            if (texture != null && material.HasProperty(property.property)) material.SetTexture(property.property, texture);
            if (property.role == "normal") material.EnableKeyword("_NORMALMAP");
            if (property.role == "metallic") material.EnableKeyword("_METALLICGLOSSMAP");
            if (property.role == "emission") { material.EnableKeyword("_EMISSION"); material.SetColor("_EmissionColor", Color.white); }
        }
        AssetDatabase.CreateAsset(material, materialPath);
        if (!String.IsNullOrEmpty(definition.mesh)) {
            if (Path.IsPathRooted(definition.mesh) || definition.mesh.Contains("..")) throw new InvalidOperationException("Unsafe mesh path.");
            var model = AssetDatabase.LoadAssetAtPath<GameObject>(folder + "/" + definition.mesh);
            if (model != null) {
                var instance = UnityEngine.Object.Instantiate(model);
                try {
                    foreach (var renderer in instance.GetComponentsInChildren<Renderer>()) renderer.sharedMaterial = material;
                    PrefabUtility.SaveAsPrefabAsset(instance, AssetDatabase.GenerateUniqueAssetPath(folder + "/RenderDocMesh.prefab"));
                } finally { UnityEngine.Object.DestroyImmediate(instance); }
            }
        }
        AssetDatabase.SaveAssets();
        Selection.activeObject = material;
    }
}
}
#endif
'''
