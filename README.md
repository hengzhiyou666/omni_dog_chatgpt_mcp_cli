# Omni Tools：机器狗 MCP 与 CLI

统一封装 Omni 巡检机器人的现有接口，供 MCP 客户端、终端和自动化脚本调用。
工程名为 `omni_tools`，安装命令为 `omni` 和 `omni-mcp`。Python 3.10+。

## V1：只读查询

| MCP 工具 | CLI | 功能 |
|---|---|---|
| `get_robot_status` | `omni robot status dog4 --json` | 状态、电量、运行模式 |
| `list_maps` | `omni maps list --robot dog4 --json` | 地图及版本 |
| `list_routes` | `omni routes list --robot dog4 --json` | 路线与地图关联 |
| `get_inspection_result` | `omni inspection result <任务编号> --robot dog4 --json` | 巡检结果与证据元数据 |
| `get_task` | `omni task status <任务编号> --robot dog4 --json` | 任务状态 |

V1 没有启动、导航、取消、速度控制接口。所有工具标记为 MCP `readOnlyHint`。

## 安装

```bash
git clone https://github.com/hengzhiyou666/omni_dog_chatgpt_mcp_cli.git
cd omni_dog_chatgpt_mcp_cli
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/omni --help
```

使用官方 MCP Python SDK 的受维护 1.x 分支（`mcp>=1.30,<2`）；版本边界避免 SDK 2.x 的不兼容升级。

## 连接 dog4：SSH 只读适配器

默认配置是 `OMNI_BACKEND=ssh`、`OMNI_SSH_HOST=dog4`、`OMNI_ROBOT_ID=dog4`。
电脑上须先有可用的 SSH 别名、密钥和可信主机记录；工具采用非交互认证，不接受未知主机或修改 SSH 配置。

```bash
.venv/bin/omni robot status dog4 --json
.venv/bin/omni maps list --robot dog4 --json
.venv/bin/omni routes list --robot dog4 --json
```

只读探针从正在运行的 Mission Manager 进程获取 ROS 环境，短暂订阅状态或调用允许的查询服务；执行结束即退出。
机器人需要已有 Python 3、rclpy、接口消息包和 Mission Manager。无需在机器人上安装本工程或更改 `/userdata/1_heng`。
一个 SSH 配置绑定一个机器人；传入其他机器人编号会被拒绝。可用 `--ssh-host` 或 `OMNI_SSH_HOST` 切换已配置的 SSH 别名。

## 连接巡检后台：HTTP 适配器

```bash
export OMNI_BACKEND=http
export OMNI_BASE_URL=https://your-inspection-server.example
export OMNI_TOKEN='从巡检后台取得的令牌'
export OMNI_ROBOT_ID='后台中的机器人编号'
.venv/bin/omni robot status --json
```

`OMNI_BASE_URL` 支持站点地址、路径前缀或以 `/api/v1` 结束的地址。远端要求 HTTPS；本机测试可用 HTTP。
令牌只通过环境传入，不放在命令参数、仓库或日志里。适配器不跟随重定向。
全局选项放在子命令之前，例如 `omni --backend http --timeout 20 maps list --json`。

## MCP 接入

本地 MCP 客户端配置进程命令为本仓库的绝对路径 `.venv/bin/omni-mcp`，参数 `--transport stdio`，环境变量与 CLI 相同。
可参考 [stdio 配置示例](examples/mcp-stdio.json)，其中 `mcpServers` 格式需按具体客户端调整。

```bash
.venv/bin/omni-mcp --transport stdio
# 另一种传输：本机 Streamable HTTP，地址 http://127.0.0.1:8766/mcp
.venv/bin/omni-mcp --transport streamable-http --port 8766
```

HTTP 入口仅监听本机，没有内置公网登录服务。远程 ChatGPT 接入还需要受支持的账户入口、可访问的 HTTPS 地址和认证网关（例如 OAuth）；这些部署配置不在本次自动安装范围内。不能将本机地址直接填成 ChatGPT 可访问的公网服务。
所有 MCP 工具返回结构化 JSON；业务错误使用 `isError=true`，标准输出仅用于 MCP 协议。

## 数据与任务语义

- 所有成功结果含 `ok/backend/robot_id/observed_at/data`。`observed_at` 是本次查询时间，不保证后台缓存中的数据刚刚更新；请同时检查源状态时间、有效性和观测信息。
- 电量 `NaN` 等非有限数值转换为 JSON `null`，不能解释成 0% 或正常。
- SSH 使用机器人原生地图、路线和任务编号；HTTP 使用云端编号。云任务对应 ROS mission ID 通常为 `cloud-<task_id>`，工具不会擅自转换。
- SSH `get_task` 只能观察当前发布的任务/操作状态；历史任务未观测到不表示不存在或已完成。历史检查点可通过 `get_inspection_result` 查询，但空结果也不能证明任务成功。
- HTTP 路线按机器人云地图绑定过滤，不代表路线已在机器人上验证可执行。
- ROS 地图/路线提供方可能在上游失败时返回空列表，工具会保留这一限制提示。
- CLI 成功退出码为 0；配置、参数、连接、上游业务错误为 2；手动中断为 130。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m build
```

默认测试完全离线：HTTP Mock、SSH 子进程 Mock，以及真正的 MCP stdio / Streamable HTTP 客户端协议交互。测试不会连接 dog4。
实体只读查询的记录见 [验证记录](docs/VALIDATION.md)；设计边界见 [架构说明](docs/ARCHITECTURE.md)。

协议实现参考 [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。
