"""Absolute-file entry point for Explorer's same-user detached Hub launch."""

import json
import os
from pathlib import Path
import sys


ENVIRONMENT_KEYS = frozenset((
    "RENDERDOC_FUSION_HUB_SPEC", "RENDERDOC_FUSION_SERVICE_DIR",
    "RENDERDOC_FUSION_ROOT", "RENDERDOC_FUSION_OUTPUT_DIR", "RENDERDOC_FUSION_IPC_DIR",
    "RENDERDOC_FUSION_ENGINE", "RENDERDOC_FUSION_CLI", "RENDERDOC_FUSION_TIMEOUT",
    "RENDERDOC_FUSION_INLINE_BYTES", "RENDERDOC_FUSION_RENDERDOC",
    "RENDERDOC_FUSION_GUI_AUTOSTART", "RENDERDOC_FUSION_GUI_VISIBLE",
    "RENDERDOC_FUSION_GUI_START_TIMEOUT",
))


def main():
    path = Path(sys.argv[1]).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)
    if not isinstance(payload, dict) or set(payload) != {"environment"}:
        raise ValueError("Invalid Hub bootstrap specification")
    environment = payload["environment"]
    if not isinstance(environment, dict) or set(environment) != ENVIRONMENT_KEYS:
        raise ValueError("Invalid Hub bootstrap configuration")
    if not all(isinstance(value, str) for value in environment.values()):
        raise ValueError("Invalid Hub bootstrap configuration value")
    directory = Path(environment["RENDERDOC_FUSION_SERVICE_DIR"]).resolve()
    if path.parent != directory:
        raise ValueError("Hub bootstrap file must belong to its private service directory")
    os.environ.update(environment)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with (directory / "hub.log").open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        sys.argv = ["renderdoc_mcp_fusion.shared_service", "serve"]
        from renderdoc_mcp_fusion.shared_service import main as serve
        try:
            serve()
        except BaseException:
            import traceback
            traceback.print_exc()
            raise


if __name__ == "__main__":
    main()
