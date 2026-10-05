"""Verify two real MCP clients share a Hub, without opening any RDC."""
import argparse
import asyncio
import gc
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def unpack(result):
    value = result.structuredContent or json.loads(result.content[0].text)
    if result.isError or not value.get('ok'):
        raise RuntimeError('MCP call failed: ' + str(value))
    return value


async def check(require_headless=False):
    root = Path(__file__).resolve().parents[1]
    python = root / '.venv/Scripts/python.exe'
    if not python.is_file():
        python = Path(sys.executable)
    env = {**os.environ, 'RENDERDOC_FUSION_ROOT': str(root), 'PYTHONPATH': str(root / 'src')}
    params = StdioServerParameters(command=str(python), args=['-m', 'renderdoc_mcp_fusion.relay'],
                                  cwd=str(root), env=env)
    async with stdio_client(params) as (read_a, write_a):
        async with ClientSession(read_a, write_a) as client_a:
            async with stdio_client(params) as (read_b, write_b):
                async with ClientSession(read_b, write_b) as client_b:
                    # On a cold service, B is the creator. Close B first below
                    # to detect Windows Job Objects killing the shared child.
                    initialized = [await client_b.initialize(), await client_a.initialize()]
                    catalogs = await asyncio.gather(client_a.list_tools(), client_b.list_tools())
                    names = {tool.name for tool in catalogs[0].tools}
                    required = {'get_backend_status', 'open_capture', 'get_pipeline_state',
                                'list_backend_tools', 'list_instances', 'release_capture'}
                    if not required <= names or names != {tool.name for tool in catalogs[1].tools}:
                        raise RuntimeError('MCP tool catalog mismatch')
                    statuses = await asyncio.gather(client_a.call_tool('get_backend_status', {}),
                                                    client_b.call_tool('get_backend_status', {}))
                    values = [unpack(result)['data'] for result in statuses]
                    pid = values[0]['service_pid']
                    if not pid or pid != values[1]['service_pid']:
                        raise RuntimeError('The clients did not connect to one shared service')
                    if any(value.get('lifetime') != 'mcp_connectors'
                           or value.get('connector_count', 0) < 2 for value in values):
                        raise RuntimeError('The two connectors did not register service leases')
                    rejected = await client_b.call_tool('list_draws', {'capture_id': 'nonexistent', 'limit': 1})
                    if not rejected.isError:
                        raise RuntimeError('An unknown capture handle was accepted')
            # The initial launcher must not own the shared service lifetime.
            gc.collect()
            still_live = unpack(await client_a.call_tool('get_backend_status', {}))['data']
            if still_live['service_pid'] != pid:
                raise RuntimeError('Disconnecting one client replaced the shared service')
    gc.collect()
    from renderdoc_mcp_fusion.config import Config
    from renderdoc_mcp_fusion.shared_service import read_record, record_is_live
    record = read_record(Config.from_environment())
    if not record or record['pid'] != pid or not record_is_live(record):
        raise RuntimeError('The shared service exited before its disconnect grace period elapsed')
    available = all((root / 'runtime/headless/bin' / name).is_file()
                    for name in ('renderdoc-mcp.exe', 'renderdoc-cli.exe', 'renderdoc.dll'))
    if require_headless and not available:
        raise RuntimeError('Bundled headless runtime files are missing')
    return {'ok': True, 'version': initialized[0].serverInfo.version, 'tool_count': len(names),
            'shared_service_pid': pid, 'two_clients_same_service': True,
            'creator_disconnect_preserved_service': True, 'connector_leases_verified': True,
            'disconnect_grace_preserved_service': True,
            'unknown_handle_rejected': True,
            'headless_runtime_files_present': available, 'capture_opened_by_check': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-headless', action='store_true',
                        help='Also require the bundled headless runtime files; does not start replay')
    args = parser.parse_args()
    try:
        result = asyncio.run(asyncio.wait_for(check(args.require_headless), timeout=90))
    except Exception as exc:
        parser.exit(1, 'Installation check failed: ' + str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
