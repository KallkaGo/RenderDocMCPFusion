"""Regression checks for GUI discovery before the first identity file is ready."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from renderdoc_mcp_fusion.config import Config
from renderdoc_mcp_fusion.errors import FusionError
from renderdoc_mcp_fusion.gui import GuiBackend
from vendor.gui_client import LiveBridgeClient

# Load only the IPC server; the bridge package imports RenderDoc's embedded API.
spec = importlib.util.spec_from_file_location(
    "readiness_bridge_server", ROOT / "bridge_extension/renderdoc_fusion_bridge/server.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def client_at(directory):
    client = LiveBridgeClient()
    client.ipc_dir = directory
    client.requests_dir = directory / "requests"
    client.responses_dir = directory / "responses"
    client.heartbeat_file = directory / "heartbeat"
    return client


def heartbeat(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "heartbeat").write_text(str(time.time()), encoding="utf-8")


def identity(directory):
    (directory / "info.json").write_text(
        json.dumps({"pid": 1234, "started_at": time.time()}), encoding="utf-8")


class DiscoveryChecks(unittest.TestCase):
    def test_heartbeat_without_valid_identity_is_not_discoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = client_at(root)
            instance = root / "instances/test-window"
            heartbeat(instance)
            self.assertEqual(client.list_instances(), [])
            for data in ("{", "[]", "null", "{}", *(
                    json.dumps({"pid": pid}) for pid in
                    (None, True, 0, -1, 2**32, "1234", 1234.5))):
                with self.subTest(info=data):
                    (instance / "info.json").write_text(data, encoding="utf-8")
                    self.assertEqual(client.list_instances(), [])
            identity(instance)
            self.assertEqual(client.list_instances()[0].info["pid"], 1234)

    def test_failed_identity_publication_does_not_advertise_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = client_at(root)
            instance = root / "instances/test-window"
            instance.mkdir(parents=True)
            server = SimpleNamespace(
                info_file=str(instance / "info.json"),
                heartbeat_file=str(instance / "heartbeat"),
                _instance_info=lambda: {"pid": 1234},
                _write_json_atomic=Mock(side_effect=OSError("identity write failed")))
            bridge.BridgeServer._write_liveness(server)
            self.assertFalse((instance / "heartbeat").exists())
            self.assertEqual(client.list_instances(), [])
            server._write_json_atomic = bridge.BridgeServer._write_json_atomic
            bridge.BridgeServer._write_liveness(server)
            self.assertEqual(client.list_instances()[0].info["pid"], 1234)


class StartupChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.backend = GuiBackend(Config(
            root=ROOT, output_dir=root, ipc_dir=root,
            engine_path=root / "unused-engine", cli_path=root / "unused-cli",
            gui_executable=Path(sys.executable), gui_start_timeout=2))
        self.process = Mock()
        self.backend._process_helpers = SimpleNamespace(
            ProcessRef=SimpleNamespace(open=Mock(return_value=self.process)))
        self.launched = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.instance = None

        def launch(window_id, on_spawn):
            self.instance = self.backend.private_ipc_dir / "instances" / window_id
            heartbeat(self.instance)
            self.loop.call_soon_threadsafe(self.launched.set)
            return {"window_id": window_id, "result": {"ready": True}}

        self.backend._launch_gui = Mock(side_effect=launch)

    async def asyncTearDown(self):
        await self.backend.close()
        self.directory.cleanup()

    async def test_first_connect_waits_for_delayed_identity(self):
        connecting = asyncio.create_task(self.backend.connect())
        try:
            await asyncio.wait_for(self.launched.wait(), 1)
            await asyncio.sleep(0.15)
            self.assertFalse(connecting.done(), "Heartbeat alone completed startup")
            self.backend._process_helpers.ProcessRef.open.assert_not_called()
            identity(self.instance)
            await asyncio.wait_for(connecting, 1)
            self.assertEqual(self.backend.window_id, self.instance.name)
            self.backend._launch_gui.assert_called_once()
            self.backend._process_helpers.ProcessRef.open.assert_called_once_with(
                1234, expected_executable=str(self.backend.config.gui_executable),
                started_before=json.loads((self.instance / "info.json").read_text())["started_at"])
        finally:
            if not connecting.done():
                connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)

    async def test_missing_identity_times_out_without_selecting_process(self):
        self.backend.config.gui_start_timeout = 0.1
        with self.assertRaises(FusionError) as caught:
            await asyncio.wait_for(self.backend.connect(), 1)
        self.assertEqual(caught.exception.code, "GUI_START_TIMEOUT")
        self.backend._process_helpers.ProcessRef.open.assert_not_called()
        self.assertIsNone(self.backend.window_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
