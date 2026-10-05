from dataclasses import dataclass
import os
import math
from pathlib import Path
import tempfile


def env_flag(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    if value.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if value.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(name + " must be 1/0 or true/false")


def gui_start_timeout():
    value = float(os.environ.get("RENDERDOC_FUSION_GUI_START_TIMEOUT", "60"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("RENDERDOC_FUSION_GUI_START_TIMEOUT must be a positive finite number")
    return value


def resource_root():
    package = Path(__file__).resolve().parent
    installed = package / "_resources"
    return installed if installed.is_dir() else package.parents[1]


def default_output_dir(root):
    installed = Path(__file__).resolve().parent / "_resources"
    if root == installed:
        local_data = os.environ.get("LOCALAPPDATA")
        user_data = Path(local_data) if local_data else Path.home() / (
            "AppData/Local" if os.name == "nt" else ".local/share")
        return user_data / "RenderDocMCPFusion" / "artifacts"
    return root / "artifacts"


@dataclass
class Config:
    root: Path
    output_dir: Path
    ipc_dir: Path
    engine_path: Path
    cli_path: Path
    timeout: float = 60.0
    inline_bytes: int = 49152
    gui_executable: Path | None = None
    gui_autostart: bool = True
    gui_visible: bool = True
    gui_start_timeout: float = 60.0

    @classmethod
    def from_environment(cls):
        root = Path(os.environ.get("RENDERDOC_FUSION_ROOT", resource_root())).resolve()
        runtime = root / "runtime" / "headless" / "bin"
        renderdoc = Path(os.environ.get("RENDERDOC_FUSION_RENDERDOC") or
                         (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "RenderDoc" / "qrenderdoc.exe"))
        return cls(
            root=root,
            output_dir=Path(os.environ.get("RENDERDOC_FUSION_OUTPUT_DIR") or default_output_dir(root)).resolve(),
            ipc_dir=Path(os.environ.get("RENDERDOC_FUSION_IPC_DIR", Path(tempfile.gettempdir()) / "renderdoc_mcp_fusion")).resolve(),
            engine_path=Path(os.environ.get("RENDERDOC_FUSION_ENGINE", runtime / "renderdoc-mcp.exe")).resolve(),
            cli_path=Path(os.environ.get("RENDERDOC_FUSION_CLI", runtime / "renderdoc-cli.exe")).resolve(),
            timeout=max(1.0, float(os.environ.get("RENDERDOC_FUSION_TIMEOUT", "60"))),
            inline_bytes=max(1024, int(os.environ.get("RENDERDOC_FUSION_INLINE_BYTES", "49152"))),
            gui_executable=renderdoc.expanduser().resolve(),
            gui_autostart=env_flag("RENDERDOC_FUSION_GUI_AUTOSTART", True),
            gui_visible=env_flag("RENDERDOC_FUSION_GUI_VISIBLE", True),
            gui_start_timeout=gui_start_timeout(),
        )
