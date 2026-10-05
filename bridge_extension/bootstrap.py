"""Isolated qrenderdoc --python bootstrap; never installs or launches a GUI.
Environment: RENDERDOC_FUSION_CAPTURE, RENDERDOC_FUSION_IPC_DIR,
RENDERDOC_FUSION_BOOTSTRAP_STATUS, RENDERDOC_FUSION_WINDOW_ID.
"""
import importlib.util
import json
import os
import sys
import traceback
import renderdoc as rd

def _fusion_start():
    try:
        # An enabled AppData extension may already have registered a bridge or
        # the previous menu controller in this same RenderDoc process. Stop it
        # before importing the project source, including all cached submodules.
        previous = sys.modules.get('renderdoc_fusion_bridge')
        if previous is not None:
            controller = getattr(previous, '_controller', None)
            if controller is not None and callable(getattr(controller, 'disable', None)):
                controller.disable()
                previous._controller = None
            server = getattr(previous, '_server', None)
            if server is not None and callable(getattr(server, 'stop', None)):
                server.stop()
                previous._server = None
        for name in list(sys.modules):
            if name == 'renderdoc_fusion_bridge' or name.startswith('renderdoc_fusion_bridge.'):
                sys.modules.pop(name, None)
        for name, module in list(sys.modules.items()):
            if name in ('renderdoc_mcp', 'renderdoc_mcp_bridge'):
                for attr in ('_poller', '_server'):
                    poller = getattr(module, attr, None)
                    if poller is not None and callable(getattr(poller, 'stop', None)):
                        poller.stop()
        script_path = globals().get('__file__')
        directory = os.environ.get('RENDERDOC_FUSION_BRIDGE_DIR')
        if not directory:
            if not script_path:
                raise RuntimeError('Set RENDERDOC_FUSION_BRIDGE_DIR when the frontend does not supply __file__')
            directory = os.path.join(os.path.dirname(script_path), 'renderdoc_fusion_bridge')
        spec = importlib.util.spec_from_file_location('renderdoc_fusion_bridge', os.path.join(directory, '__init__.py'), submodule_search_locations=[directory])
        if spec is None or spec.loader is None:
            raise RuntimeError('Fusion bridge could not be loaded: ' + directory)
        module = importlib.util.module_from_spec(spec)
        sys.modules['renderdoc_fusion_bridge'] = module
        spec.loader.exec_module(module)
        capture = os.environ.get('RENDERDOC_FUSION_CAPTURE')
        if capture:
            if not os.path.isfile(capture):
                raise RuntimeError('Capture not found: ' + capture)
            pyrenderdoc.LoadCapture(capture, rd.ReplayOptions(), capture, False, True)
            if not pyrenderdoc.IsCaptureLoaded():
                raise RuntimeError('Capture did not load')
        handler = module.RequestHandler(pyrenderdoc)
        server = module.BridgeServer(handler, bridge_id=os.environ.get('RENDERDOC_FUSION_WINDOW_ID', 'fusion-test'))
        # Initialize QObject above, then use upstream's tested worker path for --python.
        sys.modules['renderdoc_fusion_bridge.server'].QTimer = None
        module._server = server
        server.start()
        result = {'ready': True, 'parent_guard': False, 'window_id': server.bridge_id, 'ipc_dir': os.path.dirname(os.path.dirname(server.instance_dir)), 'capture': pyrenderdoc.GetCaptureFilename() if pyrenderdoc.IsCaptureLoaded() else None, 'python': sys.version}
        if pyrenderdoc.IsCaptureLoaded():
            def _record_frame(controller):
                result['frame_number'] = int(controller.GetFrameInfo().frameNumber)
            pyrenderdoc.Replay().BlockInvoke(_record_frame)
    except Exception as exc:
        result = {'ready': False, 'error': str(exc), 'traceback': traceback.format_exc()}
    path = os.environ.get('RENDERDOC_FUSION_BOOTSTRAP_STATUS')
    if path:
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False))

pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(_fusion_start)
