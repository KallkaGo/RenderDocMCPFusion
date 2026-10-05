"""Compatibility wrapper; use renderdoc-mcp-fusion --print-config after installation."""
import sys

from renderdoc_mcp_fusion.cli import main


if __name__ == "__main__":
    main(["--print-config", *sys.argv[1:]])
