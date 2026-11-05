# Omni Tools：机器狗 MCP 与 CLI

统一封装 Omni 巡检机器人的现有接口，供 MCP 客户端、终端和自动化脚本调用。
工程名为 `omni_tools`，安装命令为 `omni` 和 `omni-mcp`。Python 3.10+。

## 当前能力

第一版 [`v0.1.0`](https://github.com/hengzhiyou666/omni_dog_chatgpt_mcp_cli/tree/v0.1.0) 提供下面五项查询，已完成 dog4 只读验证。第二版 `0.2.0` 增加默认预览的任务操作接口；第三版 `0.3.0` 增加通过 SSH 执行的站立、卧下和定距前进接口。当前工作区再增加四项定位与录线操作，共 15 个 MCP 工具（5 个查询、10 个操作）。实机验证范围见[验证记录](docs/VALIDATION.md)。

| MCP 工具 | CLI | 功能 |
|---|---|---|
| `get_robot_status` | `omni robot status dog4 --json` | 状态、电量、运行模式 |
| `list_maps` | `omni maps list --robot dog4 --json` | 地图及版本 |
| `list_routes` | `omni routes list --robot dog4 --json` | 路线与地图关联 |
| `get_inspection_result` | `omni inspection result <任务编号> --robot dog4 --json` | 巡检结果与证据元数据 |
| `get_task` | `omni task status <任务编号> --robot dog4 --json` | 任务状态 |

以上五项工具标记为 MCP `readOnlyHint=true`。以下操作工具标记为 `readOnlyHint=false`，默认 `execute=false`：

| MCP 工具 | CLI | 行为 |
|---|---|---|
| `start_inspection` | `omni inspection start --robot dog4 --route <云路线编号> --json` | 预览巡检启动 |
| `navigate_to` | `omni navigation start --robot dog4 --map <云地图编号> --map-version 1 --x 1 --y 2 --yaw 0 --json` | 预览导航；米、弧度 |
| `cancel_task` | `omni task cancel <任务编号> --robot dog4 --kind inspection --json` | 预览取消任务 |
| `stand_up` | `omni posture stand --robot dog4 --json` | 预览站立；SSH 执行时等待 Bridge 回包 |
| `lie_down` | `omni posture lie --robot dog4 --json` | 预览卧下；SSH 执行时等待 Bridge 回包 |
| `move_forward` | `omni motion forward --distance 1 --robot dog4 --json` | 预览机体向前定距运动；默认 1 米，可设 0.1–2.0 米 |
| `start_localization` | `omni localization start ...` | 加载指定地图，等待定位、TF 与新鲜里程计就绪 |
| `start_route_recording` | `omni routes record start ...` | 开始录线，返回录制操作编号 |
| `finish_route_recording` | `omni routes record save/discard --operation <录制操作编号>` | 保存或丢弃指定录制会话 |
| `set_runtime_idle` | `omni runtime idle` | 关闭定位或恢复空闲，拒绝自动丢弃未收口作业 |

默认预览只验证参数并返回意图，**没有网络访问，也不验证路线实际存在或目标可达**。`get_task` / `omni task status` 另支持 `kind="navigation"` / `--kind navigation` 查询云导航目标。

## 架构与调用流程

![Omni Tools 系统架构](docs/architecture.svg)

![Omni Tools 查询、预览与执行流程](docs/control-flow.svg)

两张图使用相同的职责配色和图例；[图示说明](docs/DIAGRAMS.md) 包含阅读顺序与边界。PNG 预览位于同目录，SVG 可继续编辑。图示保留 V2 任务操作链路，新增的 SSH 姿态执行链路见下方说明和[架构说明](docs/ARCHITECTURE.md)。

## 安装

```bash
git clone https://github.com/hengzhiyou666/omni_dog_chatgpt_mcp_cli.git
cd omni_dog_chatgpt_mcp_cli
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/omni --help
```

使用官方 MCP Python SDK 的受维护 1.x 分支（`mcp>=1.30,<2`）；版本边界避免 SDK 2.x 的不兼容升级。

## 连接 dog4：SSH 查询与动作适配器

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
SSH 的查询路径保持只读；独立写入路径提供站立、卧下、定距前进及定位与录线。巡检启动、导航、取消仍只能经 HTTP 后台进入现有 Mission Manager 调度链。

## 定位与路线录制

四项操作与姿态动作共用 SSH 配置、写入开关和机器人允许列表，默认仅本地预览；执行需要 `--execute`（MCP 为 `execute=true`）。无需修改机器人部署产物。

`MAP_ID`、`MAP_VERSION`、`MAP_CHECKSUM` 必须取自 `omni maps list --json` 的同一行；初始位置 `--initial-x/y/z` 单位为米，`--initial-yaw` 为弧度，默认均为 0。

```bash
# 以下均为预览；实际执行时添加 --execute。
omni --timeout 60 localization start --map "$MAP_ID" --map-version "$MAP_VERSION" --map-checksum "$MAP_CHECKSUM" --json
omni --timeout 60 routes record start --route "$ROUTE_ID" --map "$MAP_ID" --map-version "$MAP_VERSION" --map-checksum "$MAP_CHECKSUM" --json

# 使用开始录线返回的 recording_operation_id，明确选择保存或丢弃。
omni --timeout 60 routes record save --operation "$RECORDING_OPERATION_ID" --json
omni --timeout 60 routes record discard --operation "$RECORDING_OPERATION_ID" --json
omni --timeout 60 runtime idle --json
```

执行通过固定的 Mission ROS 服务完成，并等待同一 `operation_id` 的 READY/IDLE；定位与录线还检查 TF 和新鲜 canonical odometry。保存至少需要两个点，成功后核实路线目录中的地图绑定、点数及 checksum；丢弃只针对指定会话。超时/断连不会自动重试或自动丢弃，使用返回的操作编号查询状态。

`runtime idle` 拒绝活动任务、未结束建图或录线，不能代替运动停车。`get_robot_status` / `omni robot status` 另返回 `tf_ready_status` 和 `body_odom_global` 及时间新鲜度；TF ready 为保留的状态消息，应结合里程计判断。

## 站立与卧下

MCP 的 `stand_up` 和 `lie_down` 与 CLI 共用业务实现，默认只生成本地预览。真实姿态执行需要 SSH 后端、`OMNI_ALLOW_WRITES=1`、目标位于 `OMNI_ALLOWED_ROBOTS`，以及本次 `execute=true`（CLI 为 `--execute`）。

```bash
export OMNI_BACKEND=ssh
export OMNI_SSH_HOST=dog4
export OMNI_ROBOT_ID=dog4
export OMNI_ALLOWED_ROBOTS=dog4
export OMNI_ALLOW_WRITES=1
export OMNI_TIMEOUT=30

# 预览：不连接机器狗，不发送姿态指令
.venv/bin/omni posture stand --json
.venv/bin/omni posture lie --json

# 执行：站立，然后卧下
.venv/bin/omni posture stand --execute --json
.venv/bin/omni posture lie --execute --json
```

CLI 也接受中文顶层命令，使用相同的参数与执行保护：

| CLI 命令 | 归一化动作 |
|---|---|
| `omni 站立`、`omni 起立`、`omni 站起来` | 站立，等同 `omni posture stand` |
| `omni 坐下`、`omni 卧下`、`omni 趴下` | 卧下，等同 `omni posture lie`；这里“坐下”按项目约定执行卧下 |

例如 `.venv/bin/omni 站起来 --execute --json` 和 `.venv/bin/omni 趴下 --execute --json`。在接入工具的 Codex 对话中，可用这些自然语言短语表达操作意图，由模型选择对应工具；普通 shell 中需要输入 `omni` 命令。

执行器复用狗端已有 `/omni/control/authority`、`/rosdeck/posture_command` 和 `/rosdeck/posture_status`：检查当前任务、控制权和急停状态，申请独占 APP 短期租约并续租，发送一次姿态指令，等待结果后释放租约。不会强占他人租约，也不会自动重发姿态动作。探针通过 SSH 标准输入运行，无需在 `/userdata/1_heng` 安装新服务或重新交叉编译 Bridge。

Bridge 返回成功表示厂商动作接口已应答，**不能据此确认机器狗的实体姿态**；结果保留 `physical_posture_confirmed=false`。超时或断连时应先核对机器狗与控制权状态，再决定后续操作。

## 向前定距运动

`move_forward(distance_m=1.0, robot_id=None, execute=False)` 与 CLI 共用执行实现，距离范围为 0.1–2.0 米，默认仅预览。真实执行与姿态动作使用相同的 SSH 后端、写入开关、机器人允许列表和显式执行条件；沿用上节环境配置后可运行：

```bash
# 预览：不访问机器狗
.venv/bin/omni motion forward --distance 1 --json

# 执行约 1 米的低速前进
.venv/bin/omni --timeout 40 motion forward --distance 1 --execute --json
# 等价中文入口
.venv/bin/omni --timeout 40 向前 --distance 1 --execute --json
```

前进期间由狗端执行器持有独占 APP 租约，在本地以 10 Hz（与 OmniDeck 默认频率一致）维持低速指令，并使用厂商/里程计反馈的相对 XY 位移闭环测距。该距离受反馈精度、延迟和停车过程影响，表示约定距离的相对运动，不构成 canonical 定位坐标中的精确到点导航验收。反馈过期等异常触发停车；结束时显式发送零速度并确认静止，随后释放租约。过程不会依赖 PC 每次模型调用维持控制。

此接口的真机结果以[验证记录](docs/VALIDATION.md)为准。

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
可参考默认只读的 [stdio 配置示例](examples/mcp-stdio.json)，或允许 dog4 SSH 动作执行的[独立配置示例](examples/mcp-dog4-posture.json)，其中 `mcpServers` 格式需按具体客户端调整。该配置允许站立、卧下和前进写入，但工具每次仍默认预览，实际动作必须传入 `execute=true`；前进可将 `OMNI_TIMEOUT` 调整为 40 秒。

```bash
.venv/bin/omni-mcp --transport stdio
# 另一种传输：本机 Streamable HTTP，地址 http://127.0.0.1:8766/mcp
.venv/bin/omni-mcp --transport streamable-http --port 8766
```

HTTP 入口仅监听本机，没有内置公网登录服务。远程 ChatGPT 接入还需要受支持的账户入口、可访问的 HTTPS 地址和认证网关（例如 OAuth）；这些部署配置不在本次自动安装范围内。不能将本机地址直接填成 ChatGPT 可访问的公网服务。
所有 MCP 工具返回结构化 JSON；业务错误使用 `isError=true`，标准输出仅用于 MCP 协议。

## HTTP 任务操作与默认保护

巡检启动、导航与取消的真实执行同时需要以下条件：HTTP 后台已配置、`OMNI_ALLOW_WRITES=1`、目标机器人列在 `OMNI_ALLOWED_ROBOTS`，以及本次调用明确传入 `execute=true`（CLI 为 `--execute`）。任一条件缺失都不会发送写请求。配置值不能通过 MCP 工具参数更改。站立、卧下、定距前进使用上节说明的独立 SSH 路径。

默认配置保持 `OMNI_ALLOW_WRITES=0`。以下 HTTP 任务示例保持禁用，不代表已经配置真实后台或完成实体任务验收：

```bash
export OMNI_BACKEND=http
export OMNI_BASE_URL=https://your-inspection-server.example
export OMNI_TOKEN='自己的后台令牌'
export OMNI_ROBOT_ID='已授权的云端机器人编号'
export OMNI_ALLOWED_ROBOTS='已授权的云端机器人编号'
export OMNI_ALLOW_WRITES=0  # 默认禁用
```

- 巡检：使用既有云路线，先创建任务，再请求启动；响应中的 `task_id` 用于后续查询。
- 导航：明确云地图编号与版本，先创建导航计划；核对地图、目标、可执行状态与过期时间后，才提交导航目标。真实规划在机器人端完成，云端计划不是可行路径的证明。
- 取消：巡检调用 cancel 接口，导航取消目标；不会删除巡检记录。取消请求被接受也不代表机器人已经停止。
- 写请求不自动重试。两阶段中途失败会返回已知任务或计划编号，以及恢复查询信息；超时可能意味着结果尚未确认，不能直接重复创建任务。
- 工具不提供任意速度、急停解除或控制权强占接口。站立、卧下复用 Bridge 的姿态动作；定距前进的速度循环由狗端执行器管理。

HTTP 任务操作的成功链路、失败链路、超时和取消仅在离线模拟中验证；尚未完成实体巡检、实体导航或真实 HTTP 后台端到端验收。SSH 姿态接口的验证状态单独记录在[验证记录](docs/VALIDATION.md)。

## 数据与任务语义

- 所有成功结果含 `ok/backend/robot_id/observed_at/data`。`observed_at` 是本次查询时间，不保证后台缓存中的数据刚刚更新；请同时检查源状态时间、有效性和观测信息。
- 电量 `NaN` 等非有限数值转换为 JSON `null`，不能解释成 0% 或正常。
- SSH 使用机器人原生地图、路线和任务编号；HTTP 使用云端编号。云任务对应 ROS mission ID 通常为 `cloud-<task_id>`，工具不会擅自转换。
- SSH `get_task` 只能观察当前发布的任务/操作状态；历史任务未观测到不表示不存在或已完成。历史检查点可通过 `get_inspection_result` 查询，但空结果也不能证明任务成功。
- HTTP 路线按机器人云地图绑定过滤，不代表路线已在机器人上验证可执行。
- ROS 地图/路线提供方可能在上游失败时返回空列表，工具会保留这一限制提示。
- CLI 成功查询、生成预览或收到操作成功回包的退出码为 0；配置、参数、连接、上游业务错误为 2；手动中断为 130。`executed=true` 只表示执行请求已发送并取得接口响应，不表示任务已经完成；姿态接口还会明确区分厂商应答与实体姿态确认。

## 验证

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p pytest_asyncio.plugin -q
.venv/bin/python -m build
```

默认测试完全离线：HTTP Mock、SSH 子进程 Mock，以及真正的 MCP stdio / Streamable HTTP 客户端协议交互。测试不会连接 dog4。
实体只读查询的记录见 [验证记录](docs/VALIDATION.md)；设计边界见 [架构说明](docs/ARCHITECTURE.md)。

协议实现参考 [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。
