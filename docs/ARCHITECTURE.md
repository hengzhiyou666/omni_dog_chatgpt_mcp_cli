# 架构与数据语义

V1 提供五项只读查询：机器人状态、地图目录、路线目录、任务状态、巡检结果。V2 增加默认预览的巡检启动、导航与取消操作。V3 增加默认预览、经 SSH 调用 Bridge 的站立、卧下和定距前进操作，共 11 个 MCP 工具。CLI 与 MCP 共用 `OmniService`，返回一致的结构化结果；不提供建图、回充或任意速度控制工具。

```text
CLI                         MCP 客户端
 │                          │ stdio / 本机 Streamable HTTP
 └────────── OmniService ────┘
                 │
        ┌────────┴────────┐
     HTTPBackend       SSHBackend
        │                 │
  omni-inspection API   机器人现有 Python / ROS 2
```

## 两种适配器

`OMNI_BACKEND=http` 通过 Bearer token 调用已有平台 API。查询只发送 GET 请求；V2 操作在本地执行条件全部通过后才发送 POST / DELETE。远端地址要求 HTTPS，本机测试可使用 HTTP。适配器核对资源所属机器人、解析平台错误，并保留原始业务数据。地图属于指定机器人；平台路线按该机器人的云端地图目录筛选，这种关联不保证路线已同步到机器人或可立即执行。

`OMNI_BACKEND=ssh` 使用用户已有的 SSH 主机配置及已验证主机密钥，以非交互方式运行一次性探针。固定的启动命令通过标准输入接收源码和 JSON 参数，在机器人现有 Python / ROS 2 环境中运行。原查询探针仍只订阅状态或调用白名单查询服务；动作使用独立执行路径，只允许站立、卧下和有界定距前进。无需在机器人安装本项目、常驻 MCP 服务或部署探针文件；用户输入不会拼接进远端 shell 命令。它仍需要机器人已具备对应 ROS 接口、运行环境及 Bridge。

MCP 默认通过 stdio 与本机客户端连接。可选 Streamable HTTP 仅监听 `127.0.0.1`；这个入口没有提供公网访问所需的身份认证和部署层。CLI 输出 JSON，失败时返回非零退出码；MCP 将失败表示为 `isError=true` 和结构化错误。

## 标识符不可互换

HTTP 返回的平台 `task_id`、`map_id`、`route_id` 属于云端记录。地图和路线查询额外标注 `cloud_map_id` / `cloud_route_id`；如存在 `robot_map_id` / `robot_route_id`，它们才是机器人端资产标识，不能根据名称自行假定二者相同。

SSH 查询使用机器人端原生标识。任务查询匹配运行中的 ROS 任务状态；巡检结果按原生 `mission_id` 查询。将平台任务 ID 直接传给 SSH 查询不保证能找到对应任务。工具不创建或推测跨端 ID 映射。

## 未知、陈旧与成功

- 顶层 `ok=true` 只表示查询成功，不表示机器人在线、任务完成或可以运动。
- 未知电量保留 `null`；`battery_valid=false` 不能解释为满电、零电或正常。
- `observed_at` 是本工具读取数据的时间，不能证明原始状态仍然新鲜。上游提供的时间戳、有效性和陈旧标记必须一起判断；ROS 时间与机器人系统时间也可能不同。
- SSH 任务查询是当前状态快照，不是历史任务库。`found=false` / `not_observed` 表示本次未观察到，不推断任务成功、失败或不存在。
- 空巡检结果表示未取得证据，不证明巡检完成。平台目录关联也不证明机器人当前已加载地图或持有路线。
- 超时、接口缺失、身份不匹配、平台拒绝均返回明确错误，不生成模拟成功数据。

## 验证范围

协议测试使用官方 MCP SDK，分别启动真实 stdio 与 Streamable HTTP 服务子进程，执行初始化、发现工具和调用工具。数据来自绑定在 `127.0.0.1` 的本地 HTTP 测试服务；子进程阻止非本机网络连接和再启动外部进程，测试不会连接真实机器人。该测试证明两种 MCP 传输及适配结果可用，不替代真实机器人上的 ROS 接口、网络与数据新鲜度验收。

## V2 操作边界

默认 execute=false，只生成本地预览，不读取后台状态、不创建任务或计划。巡检启动、导航、取消的执行必须使用 HTTP 后台、启用 OMNI_ALLOW_WRITES=1、目标位于 OMNI_ALLOWED_ROBOTS，并且本次 execute=true。业务层与 HTTP 适配器都检查执行配置；这些任务操作不通过 SSH 执行。

巡检按创建任务、启动任务两步执行；导航按创建计划、校验地图/目标/期限、提交目标执行。写请求不自动重试，部分成功或超时保留已知编号和后续读取路径。取消已结束任务时不会发出取消命令。

权限条件约束工具的可执行范围，不能代替用户对具体操作的授权、后台权限检查和机器人本地控制权仲裁。V2 的写路径验证使用隔离模拟，未验收实体巡检和导航。

## V3 姿态操作边界

`stand_up` 与 `lie_down` 使用相同业务入口和 SSH 执行实现，CLI 的英文子命令及中文别名只负责映射动作：站立、起立、站起来映射 `stand`；坐下、卧下、趴下映射 `lie_down`。MCP 工具与 CLI 命令都默认预览，预览不访问网络。真实执行只允许 SSH 后端，并在业务层和 SSH 适配器检查写开关、机器人允许列表；每次调用仍须明确 `execute=true`。

```text
MCP stand_up / lie_down ─┐
CLI posture / 中文别名 ──┴─ OmniService
                               │ SSH 固定执行探针
                               ▼
                 Bridge /omni/control/authority
                               │ 独占 APP 短租约、续租
                               ▼
                 /rosdeck/posture_command
                               │ stand:<client_id> / lie_down:<client_id>
                               ▼
                     厂商 SDK / 底层动作接口
                               │
                               ▼
                 /rosdeck/posture_status
```

执行前读取实时状态，拒绝活动任务、其他控制者或急停状态；不会使用人工覆盖租约借用 Mission 控制权。执行期间维持自己的短租约，单次发布姿态命令，等待回包后释放租约。动作不自动重试，超时不能推断命令未执行。

Bridge 的 `success:stand` / `success:lie_down` 证明厂商接口应答成功，不构成实体姿态测量。VBot 当前没有可用于此工具的权威姿态遥测，因此结果保留 `physical_posture_confirmed=false`；不得把回包转换为“已观察到站起/卧下”。真实 SSH 链路验证与实体动作确认范围分别见 [VALIDATION.md](VALIDATION.md)。

## 定距前进执行边界

MCP `move_forward(distance_m=1.0, robot_id=None, execute=False)` 对应 CLI `motion forward --distance 1`，中文入口为 `向前 --distance 1`。参数范围为 0.1–2.0 米，默认只生成无网络预览；实际执行只允许 SSH，业务层与适配器均检查写入开关和机器人允许列表，并要求显式执行。此能力继续位于 MCP/CLI 工程中，不依赖新增 `omni_harness` 运行逻辑。

狗端执行器获取并续租独占 APP 控制权，以 10 Hz（与 OmniDeck 默认频率一致）在本地运行低速机体前向控制，用厂商/里程计的相对 XY 位移闭环测距。反馈过期等异常要求停止运动；结束时显式发送零速度并确认静止，随后释放控制权。PC 模型只下发有界动作，不参与高频速度循环。

测量结果表示反馈坐标中的相对距离，受反馈、控制延迟和停车过程影响；不作为 canonical 定位坐标下精确到点的证明。实现与真机验收范围见 [VALIDATION.md](VALIDATION.md)。
