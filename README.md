# RenderDoc MCP Fusion

RenderDoc MCP Fusion 为 MCP 客户端提供 RDC 抓帧分析工具。多个客户端可以共用一个本地后台服务。

0.1.0 提供 44 个 MCP 工具。它可以查询绘制、着色器、资源和像素，读取 GPU 性能计数器，并导出绘制包及 Unity 导入包。可用能力取决于回放后端、抓帧内容和本机环境。

## 运行要求

| 项目 | 要求或版本 |
| --- | --- |
| Fusion | 0.1.0 |
| 操作系统 | Windows x64 |
| Python | 3.11 或更高版本 |
| GUI 模式 | 本机安装 RenderDoc；已适配 1.46 |
| headless 模式 | 附带引擎 v0.3.0，使用 RenderDoc 1.43 |
| 回放硬件 | 与抓帧兼容的 GPU 和驱动 |

本文使用以下术语：

- **客户端**：发起 MCP 请求的应用，例如 Codex 或 DeepSeek Harness。
- **连接器**：由客户端启动的 stdio 进程。它向共享服务转发请求。
- **共享服务（Hub）**：执行 MCP 请求和管理回放资源的后台进程。
- **GUI 模式**：使用 RenderDoc 窗口回放抓帧。
- **headless 模式**：使用无窗口引擎回放抓帧。
- **捕获句柄**：`open_capture` 返回的 `capture_id`。后续请求用它指定抓帧。

## 安装

1. 安装 Python。
2. 在完整项目目录中执行：

   ```powershell
   python -m pip install .
   ```

3. 如需使用 GUI 模式，安装 RenderDoc。

安装包包含连接器、GUI 桥接和 headless 运行文件。普通安装不需要手动创建 `.venv`，也不需要设置 `RENDERDOC_FUSION_ROOT` 或 `PYTHONPATH`。安装后可以从任意目录启动，原源码目录可以移走。

## 配置客户端

客户端必须支持本地 stdio MCP。安装程序不会自动修改客户端配置。

在安装时使用的 Python 环境中，生成服务配置：

```powershell
python -m renderdoc_mcp_fusion --print-config
```

如果 Python 的 Scripts 目录已加入 `PATH`，也可以执行：

```powershell
renderdoc-mcp-fusion --print-config
```

追加 `--output mcp-config.local.json` 可保存配置。命令只创建新文件，不覆盖已有文件。

生成的配置使用当前安装的绝对路径。下面的路径仅作示例。请使用命令实际生成的路径。

```json
{
  "mcpServers": {
    "renderdoc-fusion": {
      "command": "C:\\Python313\\Scripts\\renderdoc-mcp-fusion.exe",
      "args": []
    }
  }
}
```

如需通过 CC Switch 管理配置：

1. 在 MCP 面板新增自定义服务。
2. 将服务器 ID 设为 `renderdoc-fusion`。
3. 将传输类型设为 `stdio`。
4. 将 `mcpServers.renderdoc-fusion` 内的对象填入单个服务配置。该对象包含 `command` 和 `args`。如需指定 RenderDoc 路径等选项，添加 `env`。
5. 保存配置，并启用目标客户端的同步开关。
6. 在目标客户端重新连接 MCP，或重启客户端。

外层 `mcpServers` 用于完整配置文件。不要将外层对象填入单个服务配置。客户端使用 stdio 连接；不要在配置中填写共享服务内部的 HTTP 地址。

CC Switch 负责转换客户端配置格式。可用的同步目标取决于所安装的 CC Switch 版本。具体操作见 [CC Switch MCP 管理文档](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/zh/3-extensions/3.1-mcp.md)。

在源码目录运行 `python scripts/check_install.py`，可检查两个独立 MCP 客户端是否共用一个服务。此检查不打开 RDC，也不验证所有客户端应用。

## 启动与共享

连接器收到 MCP 初始化请求后，自动启动或连接本机的共享服务。多个连接器同时启动时，进程锁可防止重复创建服务。

每个连接都有自己的连接器。MCP 请求和 RDC 路由由同一个共享服务处理。连接器通过带随机令牌的 Streamable HTTP 访问服务。服务只监听 `127.0.0.1`。请勿将此端口暴露到网络。

Windows 通过同一用户、同一会话中的 Explorer 启动共享服务。此方式用于将服务与连接器的进程回收范围分开。启动需要可用的交互桌面。桌面不可用或用户身份不一致时，启动会报错。

服务不接收聊天历史。请求结果只返回给发起请求的连接。每个连接必须取得自己的捕获句柄。

不同 RDC 使用不同回放后端。同一 RDC 在相同模式下可以共用回放资源。共用资源上的操作按顺序执行。多个连接使用同一个 GUI 时，事件选择状态也会共享。连接之间的消息隔离不提供操作系统级沙箱。

## 打开与分析抓帧

如果不知道文件路径，先调用 `list_captures`。它只列出指定目录中的 RDC 文件，不打开回放：

```json
{"directory":"C:/Captures","offset":0,"limit":20}
```

确定文件后，按以下步骤打开和分析：

1. 调用 `open_capture`，并指定 RDC 的绝对路径：

   ```json
   {"capture_path":"C:/Captures/scene.rdc","backend":"auto"}
   ```

2. 保存返回的 `capture_id`。
3. 在后续请求中传入这个句柄。例如，调用 `list_draws`：

   ```json
   {"capture_id":"从 open_capture 返回值复制","limit":20}
   ```

4. 查询管线、着色器、绑定或导出数据时，按工具要求传入实际返回的 `event_id`。请求不依赖 GUI 当前选中的事件。

| `backend` | 行为 |
| --- | --- |
| `auto` | 指定窗口或可以自动启动 RenderDoc 时，使用 GUI；否则使用 headless |
| `gui` | 使用 GUI；默认自动启动 RenderDoc 并加载桥接 |
| `headless` | 使用附带的无窗口回放引擎 |

自动启动 GUI 需要启用 `RENDERDOC_FUSION_GUI_AUTOSTART`，且配置的 `qrenderdoc.exe` 路径必须存在。默认路径为 `%ProgramFiles%\RenderDoc\qrenderdoc.exe`。其他安装路径需用 `RENDERDOC_FUSION_RENDERDOC` 指定。

GUI 启动或查询失败时会返回错误，不会自动切换到 headless。指定 `window_id` 时，目标窗口必须已经加载桥接和目标 RDC。连接器不会替换其他窗口正在查看的文件。

每个连接都必须调用 `open_capture`。不要复制其他连接的句柄。出现以下情况后，请重新打开抓帧并取得新句柄：

- 客户端建立了新连接。
- 共享服务已重启。
- RDC 文件内容已变化。
- GUI 已切换抓帧，或对应窗口已关闭。
- 回放资源已被回收。
- 原生回放发生致命错误，工具返回 `SESSION_LOST`。

## 生命周期

### 共享服务

共享服务根据连接器的登记和心跳决定何时退出。

连接器首次请求服务时登记独立身份。之后每 5 秒发送一次心跳，为登记续期。连接空闲时也发送心跳。

| 情况 | 服务行为 |
| --- | --- |
| 连接器正常退出 | 注销自己的登记 |
| 连续 30 秒没有收到心跳 | 使该连接器的登记过期 |
| 仍有活动登记 | 继续运行 |
| 最后一个登记被注销或过期 | 开始 60 秒退出倒计时 |
| 倒计时结束前有连接器接入 | 取消退出倒计时 |
| 退出倒计时结束 | 开始关闭服务并清理资源 |
| 启动后一直没有连接器登记 | 60 秒后开始关闭服务 |

客户端关闭 stdio 输入时，连接器注销并退出。连接器被强制结束时，服务等待心跳超时，再开始退出倒计时。

因此，最后一个连接器正常注销后，服务约 60 秒后开始关闭。若连接器被强制结束，服务在最后一次心跳后约 90 秒开始关闭。关闭时，HTTP 请求最多还可等待 30 秒；资源清理也需要时间。上述时间不是进程消失的硬性期限。

关闭聊天是否结束连接器，由客户端决定。桌面窗口是否仍打开不参与判定。心跳只由存活的连接器发送；连接器退出后，心跳会停止。

手动停止共享服务后，心跳不会重新启动它。下一次 MCP 请求才会启动新服务。

调用 `get_backend_status` 可查看生命周期状态：

| 字段 | 含义 |
| --- | --- |
| `lifetime` | 当前为 `mcp_connectors` |
| `connector_count` | 活动连接器数量 |
| `lease_timeout_seconds` | 心跳超时，默认 30 秒 |
| `shutdown_grace_seconds` | 退出宽限期，默认 60 秒 |
| `shutdown_in_seconds` | 剩余宽限时间；有活动登记时为 `null` |

### 回放资源

headless 回放的空闲回收与共享服务的退出倒计时分别工作。

- 默认模式为 `headless_mode="persistent"`。连续 300 秒没有实际操作后，服务释放回放引擎及对应句柄。
- `headless_mode="on_demand"` 在每次实际调用后释放回放引擎。下一次查询会重新加载。捕获句柄仍受 300 秒空闲期限限制。
- 状态查询和目录查询不延长回放资源的空闲期限。
- 正在执行或排队的操作不受空闲回收影响。
- `release_capture` 只释放本连接的句柄。最后一个 headless 句柄释放后，服务可停止对应回放。
- 关闭 RenderDoc 窗口会使对应 GUI 句柄失效，不影响其他 RDC。

关闭连接器或共享服务不会关闭已有的 RenderDoc 窗口。请在不再需要窗口时手动关闭它。

## 查看进程与更新程序

### 查看进程

共享服务在 Windows 任务管理器中使用以下名称：

| 页面 | 名称 |
| --- | --- |
| 进程 | `RenderDoc MCP` |
| 详细信息 | `RenderDocMCP.exe` |

连接器可能显示为 Python 进程。RenderDoc 窗口对应 `qrenderdoc.exe`。请用 `get_backend_status` 返回的 `service_pid` 核对共享服务，避免把连接器退出当成共享服务退出。

共享服务使用基础 Python 的 `pythonw.exe` 和运行库生成专用启动文件。缓存默认位于 `%LOCALAPPDATA%\RenderDocMCPFusion\shared-service\named-runtime`。此过程不修改原 Python 安装。

专用启动文件的描述为 `RenderDoc MCP`。Python 版本和版权信息保留。基础运行库变化后，程序会生成新缓存。`RENDERDOC_FUSION_SERVICE_DIR` 可更改服务状态和缓存目录。安装依赖中的 `pywin32` 提供 Windows 桌面启动和文件描述更新支持。

### 更新程序

**修改源码不会更新已运行的连接器或共享服务。** 普通安装还需要重新安装软件包。请在安装时使用的 Python 环境中执行以下步骤：

1. 在各客户端断开此 MCP 服务。
2. 查看旧服务状态，再停止旧服务：

   ```powershell
   python -m renderdoc_mcp_fusion.shared_service status
   python -m renderdoc_mcp_fusion.shared_service stop
   ```

3. 在项目目录安装新版本：

   ```powershell
   python -m pip install .
   ```

4. 关闭不再使用的旧 RenderDoc 回放窗口。
5. 在各客户端重新连接 MCP，以启动新连接器和新服务。如果工具列表仍是旧版，刷新客户端的 MCP 工具列表或重启客户端。
6. 重新调用 `open_capture`，取得新句柄。GUI 模式下使用新回放窗口加载新桥接。

升级 GUI 桥接后，也需要新建 RenderDoc 回放窗口。旧窗口仍运行加载时的桥接代码。请关闭不再使用的旧窗口；新服务自动启动的窗口会加载新桥接。手动指定旧 `window_id` 不会更新其中的代码。

停止共享服务会影响所有连接到它的客户端。重启前的捕获句柄全部失效。新旧版本不能共用同一个运行中的服务；版本不一致时会报错。

开发时可以执行 `python -m pip install -e .`。可编辑安装需要保留源码目录。修改代码后，仍须重启连接器和共享服务。

## Fusion Bridge

GUI 模式需要 Fusion Bridge 读取 RenderDoc 数据。自动启动流程通过 `qrenderdoc --python` 加载桥接，无需在扩展菜单中手动开启它。

请保留安装包自带的 `bridge_extension`。普通安装将它放在 Python 包的 `_resources` 目录中。可编辑安装直接使用源码目录中的文件。

旧版手动安装可能位于 `%APPDATA%\qrenderdoc\extensions\renderdoc_fusion_bridge`。如果只使用自动启动流程，可以停用或移除这份旧安装。如果仍需通过扩展菜单为手动打开的窗口提供桥接，请保留它。

## 工具与限制

工具列表如下。

| 用途 | 工具 |
| --- | --- |
| 服务和捕获状态 | `get_backend_status`、`list_instances`、`get_capture_status` |
| 查找、打开和释放抓帧 | `list_captures`、`open_capture`、`release_capture` |
| 查询帧和绘制 | `list_draws`、`get_frame_summary`、`get_draw_call_details`、`get_pipeline_state` |
| 查询着色器和绑定 | `get_shader_info`、`get_bindings`、`get_bound_textures`、`get_cbuffer_data`、`debug_vulkan_bindings` |
| 查询资源 | `get_textures`、`get_buffers`、`get_resources`、`get_texture_info`、`get_texture_data`、`get_buffer_contents` |
| 查询像素和调试 | `pick_pixel`、`get_texture_minmax`、`pixel_history`、`debug_pixel`、`debug_vertex`、`get_post_vs_data`、`get_debug_messages` |
| 分析绘制 | `analyze_lighting`、`identify_drawcalls`、`find_draws_by_shader`、`find_draws_by_texture`、`find_draws_by_resource` |
| 查询 GPU 性能 | `enumerate_counters`、`fetch_counters`、`get_action_timings` |
| 导出数据 | `export_buffer`、`export_texture`、`export_mesh`、`export_render_target` |
| 导出绘制包 | `export_drawcall`、`export_to_unity` |
| 访问允许的后端接口 | `list_backend_tools`、`call_backend_tool` |

所有抓帧分析调用都需要本连接的 `capture_id`。顶层工具使用 `event_id`、`resource_id`、`shader_id` 和 `output_path`。原生 GUI 接口使用 `eid`、`rid`、`sid` 和 `dest`。请按工具目录中的参数说明调用，不要混用两套名称。

资源 ID 必须使用字符串，例如 `"ResourceId::30588"`。部分 ID 超过常见客户端的整数精度范围，不能改成 JSON 数字。

### 后端选择

| 场景 | 入口和限制 |
| --- | --- |
| 查询服务或查找 RDC | `get_backend_status`、`list_instances`、`list_captures`；无需先打开抓帧 |
| 基本回放查询 | 两个后端均支持 `list_draws`、`get_pipeline_state`、`get_shader_info` 和 `get_bindings`；返回内容和完整程度不同 |
| 资源查询、像素调试、绘制反查、光照分析、计数器和绘制包导出 | 对应的顶层工具使用 GUI |
| headless 原生能力 | 先调用 `list_backend_tools`，再通过 `call_backend_tool` 调用目录中的工具；其中包含像素历史和调试等能力 |
| 原始缓冲和纹理文件 | `export_buffer`、`export_texture` 使用 GUI |
| 绘制后网格 | `export_mesh` 支持两个后端；输出格式不同 |
| headless 渲染目标 | `export_render_target` 使用独立 CLI 导出 PNG |

GUI 专用调用在 headless 捕获句柄上会返回 `UNSUPPORTED_OPERATION`。需要这些能力时，请重新调用 `open_capture` 并指定 `backend="gui"`。工具不会自动切换后端。

### 调用示例

以下示例中的 `capture_id` 和事件编号需要替换为当前抓帧的实际值。先通过 `list_draws` 获取事件编号。

调用 `get_shader_info`，读取顶点着色器的前 100 行反汇编：

```json
{"capture_id":"当前连接的捕获句柄","event_id":120,"stage":"vs","mode":"disasm","offset":0,"max_lines":100}
```

调用 `identify_drawcalls`，比较一个绘制前后的颜色数据。此示例将像素数上限设为 4,194,304，可容纳 1920 × 1080 的目标：

```json
{"capture_id":"当前连接的捕获句柄","eid_min":120,"eid_max":120,"include_diff":true,"max_pixels":4194304}
```

调用 `export_to_unity`，将一个绘制导出到新目录：

```json
{"capture_id":"当前连接的捕获句柄","event_id":120,"output_path":"C:/Exports/draw-120"}
```

输出目录必须尚不存在。省略 `output_path` 时，程序在默认输出目录下分配新路径。导出后先检查结果中的 `complete`、`errors` 和 `warnings`，再查看 `manifest.json`。

`list_captures` 列出指定目录中的 RDC 文件，使用 `offset` 和 `limit` 分页，不递归扫描。它不需要捕获句柄。`list_instances` 列出已打开的回放后端。`open_capture` 打开已有 RDC，不启动游戏或创建新抓帧。`mode="code"` 不会从机器码还原原始高级着色器源码。

### 数据范围和错误处理

- `get_shader_info` 在 GUI 中支持 `reflect`、`disasm`、`source` 和 `code`。文本用 `offset` 和 `max_lines` 分页，结果报告是否截断。缺少调试源码时，`source` 不会生成虚构源码。
- 资源列表和像素历史用 `offset`、`limit` 分页。原始字节以 Base64 返回。纹理字节默认最多返回 4 KiB，最高 1 MiB；原生回放仍会先读取所选子资源。3D 纹理读取整个 mip 层，不能只读取一个深度切片。
- `debug_pixel` 和 `debug_vertex` 用 `max_steps` 限制返回的调试步数。结果报告调试是否完成以及数据是否截断。指定像素没有着色器调用，或回放 API 不支持调试时，调用会报错。
- `analyze_lighting` 读取着色器、绑定和常量证据。光照模型和纹理用途是启发式判断，不是已验证的场景语义。
- `identify_drawcalls` 可比较每个绘制前后的颜色数据，并返回变化像素数和包围框。颜色没有变化不代表没有几何体。压缩格式、数组纹理和多重采样等不支持的情况会逐项报告。默认每次分析 16 个绘制，并限制纹理像素数。
- 着色器和资源反查返回实际绘制事件。着色器扫描达到上限时返回 `next_after_eid`；下一次调用将它传入 `after_eid`。
- 性能计数器来自回放设备。`get_action_timings` 使用 GPU Duration 计数器。设备不支持时返回错误，不用零值代替真实测量。
- Windows 上的 D3D12 计数器要求启用系统开发者模式。检测到该设置未启用时，调用会说明原因，不执行计数器回放。Fusion 不修改这个系统设置。原生回放发生致命错误时，捕获句柄失效；请重新打开抓帧。
- 绘制包包含完整着色器文本、纹理、管线状态、可用网格和 `manifest.json`。先检查 `complete`、`errors` 和 `warnings`。部分文件导出成功不代表整个包完整。
- Unity 包包含 OBJ、纹理、材质映射建议和 Editor 导入脚本。将包复制到 Unity 的 `Assets` 下，检查 `material.json`，再执行包内说明指定的菜单。它不恢复原始材质、骨骼、动画或场景。
- GUI 可读取实际资源绑定和常量缓冲值。
- 附带 headless 引擎的部分绑定结果只表示声明，不能当作实际纹理绑定。它不支持常量值读取。
- `export_mesh` 导出 GPU 变换后的数据。绘制包优先导出捕获的顶点输入；没有位置输入时可回退到绘制后的位置预览。请检查网格的坐标说明和 UV 限制。这些数据都不等于原始骨骼模型。
- 原生接口只开放工具目录中允许的操作。会绕过本服务捕获生命周期管理的操作被阻止。
- 普通安装默认将输出写入 `%LOCALAPPDATA%\RenderDocMCPFusion\artifacts`。源码运行默认写入项目的 `artifacts` 目录。程序不覆盖已有文件。
- 超出内联大小限制的响应会完整写入 JSON 文件，并返回文件大小及 SHA256。

计数器前置检查失败时，返回错误详情中的 `developer_mode_required`，现有回放仍可继续查询。若系统设置无法读取，程序不会假定它已启用；后续原生致命错误仍会使句柄失效。

## 项目文件与输出

运行时输出按需创建。`artifacts/` 用于源码运行时的导出文件和大型响应；它不需要预先存在。`artifacts/`、`/tests/`、构建目录和本地运行状态均由 Git 忽略。清理生成文件后，后续调用可以重新创建所需的输出目录。

## 常用环境变量

| 变量 | 用途 |
| --- | --- |
| `RENDERDOC_FUSION_RENDERDOC` | 指定 `qrenderdoc.exe` 路径 |
| `RENDERDOC_FUSION_GUI_AUTOSTART` | 控制 GUI 自动启动 |
| `RENDERDOC_FUSION_GUI_VISIBLE` | 控制 GUI 窗口可见性 |
| `RENDERDOC_FUSION_GUI_START_TIMEOUT` | 设置 GUI 启动等待时间，单位为秒 |
| `RENDERDOC_FUSION_OUTPUT_DIR` | 指定输出目录 |
| `RENDERDOC_FUSION_SERVICE_DIR` | 指定共享服务的状态和缓存目录 |

## 许可证

本项目采用 [MIT 许可证](LICENSE)。第三方组件保留各自的版权声明和许可证。

## 鸣谢

感谢以下开源项目及其贡献者：

- [RenderDoc](https://github.com/baldurk/renderdoc)：提供图形捕获、回放和调试能力。
- [JiaboLi-GitHub/renderdoc-mcp](https://github.com/JiaboLi-GitHub/renderdoc-mcp)：提供本项目使用的 headless 引擎。
- [Hengle/RenderDocMCP2](https://github.com/Hengle/RenderDocMCP2)：为查询和导出功能提供参考。
- [stb](https://github.com/nothings/stb)：提供 headless 引擎使用的 `stb_image` 和 `stb_image_write` 图像读写库。
