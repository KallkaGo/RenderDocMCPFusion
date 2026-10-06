"""Give the Windows Hub its own image name without changing installed Python."""

import hashlib
from pathlib import Path
import shutil
import sys


def prepare_hub_executable(directory):
    # Use the real interpreter, never a venv redirector that spawns pythonw.exe.
    home = Path(sys.base_prefix).resolve()
    prefix = Path(sys.prefix).resolve()
    include_system = True
    if prefix != home:
        # Match site.py's default and interpretation for a real source venv.
        for line in (prefix / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip().lower() == "include-system-site-packages":
                include_system = value.strip().lower() == "true"
    system_site = "true" if include_system else "false"
    source = home / "pythonw.exe"
    if not source.is_file():
        raise RuntimeError("The base Python runtime has no pythonw.exe for hidden Hub startup")
    files = [source, *sorted(home.glob("*.dll"))]
    digest = hashlib.sha256(b"display-name-v4" + str(home).encode() + str(prefix).encode()
                            + system_site.encode())
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    root = Path(directory) / "named-runtime" / digest.hexdigest()[:20]
    executable = root / "Scripts" / "RDCMCP.exe"
    ready = root / "ready"
    if ready.is_file() and executable.is_file():
        return executable
    executable.parent.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copy2(path, executable if path == source else executable.parent / path.name)
    (root / "pyvenv.cfg").write_text(
        "home = " + str(home) + "\ninclude-system-site-packages = " + system_site + "\n",
        encoding="utf-8")
    if prefix != home:
        site_dir = root / "Lib" / "site-packages"
        site_dir.mkdir(parents=True, exist_ok=True)
        original_site = str(prefix / "Lib" / "site-packages")
        (site_dir / "fusion-runtime.pth").write_text(
            "import site; site.addsitedir(" + repr(original_site) + ")\n", encoding="utf-8")
    from .windows_version import set_hub_description
    set_hub_description(executable)
    ready.write_text("1\n", encoding="ascii")
    return executable
