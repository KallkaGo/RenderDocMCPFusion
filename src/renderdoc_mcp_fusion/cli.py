"""Installed command and portable MCP configuration output."""
import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import sys


def make_config():
    # RECORD tracks the actual launcher for venv, system and --user installs.
    name = "rdc-mcp-fusion.exe" if os.name == "nt" else "rdc-mcp-fusion"
    try:
        files = metadata.files("renderdoc-mcp-fusion") or ()
    except metadata.PackageNotFoundError:
        files = ()
    launcher = next((Path(item.locate()).resolve() for item in files
                     if item.name == name and Path(item.locate()).is_file()), None)
    service = ({"command": str(launcher), "args": []} if launcher else
               {"command": str(Path(sys.executable).resolve()),
                "args": ["-m", "renderdoc_mcp_fusion"]})
    return {"mcpServers": {"rdc-fusion": service}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=
        "Run the RDC MCP stdio connector, or print its CC Switch JSON.")
    parser.add_argument("--print-config", action="store_true",
                        help="Print generic MCP JSON using this installation's absolute path")
    parser.add_argument("--output", type=Path,
                        help="With --print-config, write a new JSON file without overwriting")
    args = parser.parse_args(argv)
    if args.output is not None and not args.print_config:
        parser.error("--output requires --print-config")
    if not args.print_config:
        from .relay import main as run
        return run()
    try:
        text = json.dumps(make_config(), ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(text, end="")
        else:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(text)
            print("Configuration written to " + str(args.output.resolve()), file=sys.stderr)
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")
