The external client requires Python 3.11+. The frontend extension is Python 3.8
compatible and imports RenderDoc from the frontend runtime.

```python
from vendor.gui_client import LiveBridgeClient, LiveBridgeError
client = LiveBridgeClient(timeout=60.0, bridge_id="fusion-test")
response = client.call("inspect_shader", {"eid": 865, "stage": "ps"}, window_id="fusion-test")
```

Both processes use `RENDERDOC_FUSION_IPC_DIR` if supplied; otherwise they use
`tempfile.gettempdir()/renderdoc_mcp_fusion`. Window discovery is
`client.list_windows()`; multiple windows require an explicit ID. The client reads
`RENDERDOC_FUSION_WINDOW_ID` / `RENDERDOC_FUSION_BRIDGE_ID`, never the old plugin's
window environment variables.

File bridge success/error envelope: `{ok, mode, data, err, meta}`. `err` contains
`code` and `msg`. Transport and unknown-handler failures raise `LiveBridgeError`.
This client alone is not a complete MCP server; the project's gateway supplies MCP.
The gateway must verify `get_capture_status.data.path` against its pinned session
capture before queries and exports. Selecting or changing a backend is a gateway
decision; the bridge does not fall back to a different backend.

| Operation | Parameters |
| --- | --- |
| `open_capture` | `path` required, `wait` (seconds, use 180 for loading) |
| `get_capture_status` | optional `directory` |
| `find_events` | `q`, `marker`, `exclude_markers`, `eid_min`, `eid_max`, `limit=50` |
| `inspect_pipeline_state` | `eid` |
| `inspect_shader` | `eid`, `stage=vs/hs/ds/gs/ps/cs` |
| `get_draw_packet` | `eid` |
| `inspect_cbuffer_values` | `eid`, `stage`, optional `slot`, `raw=false` |
| `export_shader_raw_bytes` | `eid`, `stage`, `dest` (fresh file; upstream overwrites) |
| `export_buffer` | `rid`, `dest`, optional `eid`, `offset=0`, `length=0` means remainder, `overwrite=false` |
| `debug_save_texture` | `rid` required, optional `eid`, `dest` file, `format=PNG/HDR/DDS`, `type_cast` RenderDoc component name, `overwrite=false` |
| `export_texture_raw` | `rid`, `eid`, `dest`, `mip=0`, `slice=0`, `sample=0`, `overwrite=false` |
| `export_postvs` | `eid`, `dest` fresh directory, `first_instance=0`, `instance_count=1`, `view=0`, `max_bytes=536870912` |
| `inspect_mesh` / `export_mesh` | `eid`; export also has `dest`, `overwrite=false`; OBJ supports VSIn TriangleList base geometry |

For an owned GUI process, run `qrenderdoc --python bridge_extension/bootstrap.py`
using normal process tools. The script never launches a GUI itself. Set
`RENDERDOC_FUSION_CAPTURE`, `RENDERDOC_FUSION_WINDOW_ID`, and
`RENDERDOC_FUSION_BOOTSTRAP_STATUS` as needed. Set `RENDERDOC_FUSION_BRIDGE_DIR` to
the absolute `bridge_extension/renderdoc_fusion_bridge` path for frontends which do
not set Python `__file__`. Bootstrap stops only auto-loaded original bridge pollers
inside this owned process, initializes on the UI thread, and runs the copied
bridge's worker path. Replay queries use `Replay().BlockInvoke`; capture lifecycle
calls dispatch to the UI thread. It performs no registration of Ruri processors,
extension installation or configuration edits.

Source changes are tested with focused mocks. Actual RenderDoc 1.46 replay and the
MCP gateway need separate integration verification. Native D3D12 fixed-function
fields are returned through `GetD3D12PipelineState`; unsupported APIs/fields have
explicit reasons. No semantic meaning is inferred for stripped constant variables.
