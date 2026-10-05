"""Bundle the Windows replay runtime and GUI bridge in ordinary wheels."""
from pathlib import Path

from setuptools import Distribution, setup
from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_py import build_py


ROOT = Path(__file__).resolve().parent
RESOURCE_TREES = (
    "runtime/headless",
    "bridge_extension/renderdoc_fusion_bridge",
)
RESOURCE_FILES = (
    "bridge_extension/bootstrap.py",
    "bridge_extension/process_lifecycle.py",
    "vendor/gui_client/__init__.py",
    "vendor/gui_client/bridge_client.py",
    "vendor/gui_client/ATTRIBUTION.md",
    "vendor/gui_client/GUI_INTERFACE.md",
    "scripts/launch_gui.py",
)
EXCLUDED_PARTS = {"__pycache__", ".venv", "artifacts", ".runtime-state"}


def resource_files():
    """Return only runtime resources, with paths relative to the project root."""
    files = [Path(name) for name in RESOURCE_FILES]
    for name in RESOURCE_TREES:
        tree = ROOT / name
        if not tree.is_dir():
            raise FileNotFoundError(f"Required runtime resource directory is missing: {tree}")
        files.extend(
            path.relative_to(ROOT)
            for path in tree.rglob("*")
            if path.is_file()
            and not EXCLUDED_PARTS.intersection(path.relative_to(tree).parts)
            and path.suffix.lower() not in {".pyc", ".pyo"}
        )
    for relative in files:
        if not (ROOT / relative).is_file():
            raise FileNotFoundError(f"Required runtime resource is missing: {ROOT / relative}")
    return sorted(set(files))


class BuildRuntimeResources(build_py):
    def run(self):
        super().run()
        destination = Path(self.build_lib) / "renderdoc_mcp_fusion" / "_resources"
        for relative in resource_files():
            target = destination / relative
            self.mkpath(str(target.parent))
            self.copy_file(str(ROOT / relative), str(target))

    def get_outputs(self, include_bytecode=1):
        outputs = super().get_outputs(include_bytecode)
        destination = Path(self.build_lib) / "renderdoc_mcp_fusion" / "_resources"
        return outputs + [str(destination / relative) for relative in resource_files()]


class WindowsRuntimeDistribution(Distribution):
    def has_ext_modules(self):
        # Native runtime files need the platform library installation scheme,
        # even though they are not Python extension modules.
        return True


class WindowsRuntimeWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        # The bundled EXE/DLL files are Windows x64 binaries, independent of the
        # Python ABI. Install into platlib and reject other platform installers.
        self.root_is_pure = False
        self.plat_name = "win_amd64"

    def get_tag(self):
        return "py3", "none", "win_amd64"


setup(distclass=WindowsRuntimeDistribution, cmdclass={"build_py": BuildRuntimeResources, "bdist_wheel": WindowsRuntimeWheel})