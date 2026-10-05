"""Request routing for the qrenderdoc bridge extension."""

from .observe import CaptureStatusService, ObserveService
from .domains.camera import ReplayCameraService
from .domains.analysis import replay_failure


class RequestHandler:
    """Routes bridge requests to compact extension services."""

    def __init__(self, ctx):
        self._ctx = ctx
        capture_service = CaptureStatusService(ctx)
        observe_service = ObserveService(ctx)
        camera_service = ReplayCameraService(ctx)
        self._handlers = {
            "ping": lambda _params: {"status": "ok", "message": "pong"},
            "get_capture_status": capture_service.run,
            "get_replay_camera_state": camera_service.get_replay_camera_state,
            "configure_replay_camera": camera_service.configure_replay_camera,
            "reset_replay_camera": camera_service.reset_replay_camera,
            "configure_replay_camera_recipe": camera_service.configure_replay_camera_recipe,
            "apply_replay_camera_recipe": camera_service.apply_replay_camera_recipe,
            "open_capture": capture_service.open_capture,
            "close_capture": capture_service.close_capture,
            "find_latest_capture": capture_service.find_latest_capture,
            "load_latest_capture": capture_service.load_latest_capture,
            "wait_for_new_capture": capture_service.wait_for_new_capture,
            "find_events": observe_service.run,
            "list_passes": observe_service.list_passes,
            "get_frame_packet": observe_service.get_frame_packet,
            "get_pass_packet": observe_service.get_pass_packet,
            "get_draw_packet": observe_service.get_draw_packet,
            "debug_resource_ctx": observe_service.debug_resource_ctx,
            "debug_resource_info": observe_service.debug_resource_info,
            "debug_save_texture": observe_service.debug_save_texture,
            "export_buffer": observe_service.export_buffer,
            "export_texture_raw": observe_service.export_texture_raw,
            "save_event_output_texture": observe_service.save_event_output_texture,
            "debug_save_overlay": observe_service.debug_save_overlay,
            "inspect_pipeline_state": observe_service.inspect_pipeline_state,
            "inspect_shader": observe_service.inspect_shader,
            "get_target_shader_encodings": observe_service.get_target_shader_encodings,
            "apply_shader_edit": observe_service.apply_shader_edit,
            "revert_shader_edit": observe_service.revert_shader_edit,
            "export_shader_raw_bytes": observe_service.export_shader_raw_bytes,
            "inspect_cbuffer_values": observe_service.inspect_cbuffer_values,
            "read_buffer": observe_service.read_buffer,
            "get_shader_disasm": observe_service.get_shader_disasm,
            "get_shader_source": observe_service.get_shader_source,
            "get_shader_code": observe_service.get_shader_code,
            "inspect_texture_usage": observe_service.inspect_texture_usage,
            "inspect_mesh": observe_service.inspect_mesh,
            "export_mesh": observe_service.export_mesh,
            "export_postvs": observe_service.export_postvs,
            "export_capture_inventory": observe_service.export_capture_inventory,
            "export_pixel_history": observe_service.export_pixel_history,
            "export_shader_debug_trace": observe_service.export_shader_debug_trace,
        }
        for method in (
            "get_textures", "get_buffers", "get_resources", "get_texture_info",
            "get_texture_data", "get_buffer_contents", "pick_pixel", "get_texture_minmax",
            "pixel_history", "debug_pixel", "debug_vertex", "get_post_vs_data",
            "get_debug_messages", "get_bound_textures", "analyze_lighting",
            "identify_drawcalls", "enumerate_counters", "fetch_counters", "get_action_timings",
            "find_draws_by_shader", "find_draws_by_resource", "find_draws_by_texture",
            "debug_vulkan_bindings", "export_drawcall", "export_to_unity",
        ):
            self._handlers[method] = getattr(observe_service, method)
        self._capture_service = capture_service

    def _replay_failure(self):
        if not self._ctx.IsCaptureLoaded():
            return None
        failure = [None]
        def check(controller):
            failure[0] = replay_failure(controller)
        self._ctx.Replay().BlockInvoke(check)
        if failure[0] is None:
            return None
        return {"ok": False, "mode": "summary", "data": None,
                "err": {"code": "replay_fatal_error", "msg": failure[0]},
                "meta": {"cap": "invalid", "truncated": False}}

    def describe_instance(self):
        """Return lightweight metadata for bridge discovery."""
        try:
            status = self._capture_service.run({})
            data = status.get("data") or {}
            return {
                "loaded": bool(data.get("loaded")),
                "capture_path": data.get("path"),
                "api": data.get("api"),
            }
        except Exception as exc:
            return {
                "loaded": False,
                "status_error": str(exc),
            }

    def handle(self, request):
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        if method not in self._handlers:
            return {
                "id": request_id,
                "error": {
                    "code": "method_not_found",
                    "message": "Unknown method: {}".format(method),
                },
            }

        try:
            # A fatal native error can leave a loaded capture backed by a dummy
            # driver. Its empty arrays are not valid analysis results.
            recovery = method in {"ping", "open_capture", "close_capture", "load_latest_capture", "wait_for_new_capture"}
            failure = None if recovery else self._replay_failure()
            if failure is not None:
                return {"id": request_id, "result": failure}
            result = self._handlers[method](params)
            if method != "ping":
                failure = self._replay_failure()
                if failure is not None:
                    return {"id": request_id, "result": failure}
            return {"id": request_id, "result": result}
        except Exception as exc:
            return {
                "id": request_id,
                "error": {
                    "code": "request_failed",
                    "message": str(exc),
                },
            }
