# 验证记录

## 定位与录线 MCP / CLI — 2026-11-05（本轮）

### 用户返场后的实机验证

- 用户确认约1米空地并在旁观察后，CLI 起立成功应答；`motion forward --distance 1` 实测0.9754米，`stop_confirmed=true`，1.416秒内确认停车，控制权成功释放。此前停车确认的待验项本次通过。
- 安全重启 ROS 应用清除已有锁存冲突。定位匹配成功后，TF 曾因2秒SLAM心跳超时清除对齐；只将部署配置 `/userdata/1_heng/config/omni_vbot_dog4.yaml` 的 `timeouts.slam_status_s` 改为5秒并重启加载，传感器1秒新鲜度等检查不变。备份位于 `/userdata/1_heng/backups/tf-status-timeout-20261105T064027Z/`。随后正式 MCP 定位返回READY，连续30秒TF与global odom有效。
- 正式 CLI 开始录线返回READY；MCP 丢弃静态测试录线返回IDLE。单点SAVE被工具前置拒绝，未发送保存请求。
- 用户用 OmniDeck 倒退约1米并左右调整后，录线在3点、0.6446米处因运行时4009错误冻结。首次MCP保存遇SSH断连，先查询确认仍为原会话，再经热点重新提交保存；保存成功，核实地图版本/校验和及路线目录，终态IDLE。保存的测试路线为 `mcp-record-20261105T0657`，原有路线未改动。此结果验证中断录线可保存已有数据，尚不能证明整段移动完整录制。
- 原SSH跳板192.168.10.100中断后，PC通过已保存的dog4热点恢复；使用临时SSH跳板覆盖，沿用原主机密钥严格校验，未修改全局SSH配置。
- 第二次CLI录线连续约110秒、221帧均READY，最终12点、3.7021米，由App保存并返回LOCALIZATION_READY。CLI随后请求保存因会话已结束而前置拒绝，未重复写入。只读路线目录确认 `mcp-record-20261105T0711` 已保存，地图三元组匹配。
- 保存后定位期间捕捉到约437毫秒TF未就绪后自行恢复：原始里程计间隔1.432秒，SLAM心跳最大间隔1.109秒，均未超过Mission既有2秒期限。TF诊断显示传感器输入短暂失鲜。录线watchdog复用实时ready判据，会把此类瞬态升级为永久ERROR；旧失败会话因切网缺少失败瞬间记录，不能断言原因完全相同。
- MCP `set_runtime_idle` 实测返回IDLE；随后CLI卧下成功应答、释放控制权。实机JSON及关联采样保存在工作区 `output/dog4_analysis_20261105/`。
- 录线TF瞬态处理已在 `omni_mission_manager` 最小修复：核心源文件+33/-11行，TF未就绪时暂停采点，恢复后续录；不再把单次TF false升级为永久ERROR，原2秒有效输入期限及SLAM/地图检查保留。8项ROS集成回归全部通过，包括短暂恢复、持续TF失效、odom/SLAM中断、地图变化及原SAVE/DISCARD流程。仅增量编译coordinator并链接，ARM产物SHA256为 `4d465e91b554d1a57d8b1d22652a4286da0eca223049f288e5037b995c047e04`，已部署dog4；原二进制及manifest备份位于 `/userdata/1_heng/backups/route-tf-recovery-20261105T0725/`。
- 部署后正式MCP开始静态录线返回READY；45秒实机只读观察中90帧状态持续READY约44秒，无ERROR，实际运行文件SHA匹配新产物。本次未自然出现TF false，短暂恢复行为由本地ROS回归验证。
- 最后正式CLI丢弃静态测试录线成功，运行时回IDLE，无未保存数据。部署后的验证未发送运动指令，保留用户已保存的12点路线。

### 离线验证及用户离场期间的初次尝试

- 新增 `start_localization`、`start_route_recording`、`finish_route_recording`、`set_runtime_idle`；CLI 为 `localization start`、`routes record start/save/discard`、`runtime idle`，当前共15个MCP工具。
- 完整离线测试 **611项通过**：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p pytest_asyncio.plugin -q`；包括真实 stdio / Streamable HTTP 协议发现、无I/O预览、参数/写权限、ROS操作身份与终态、SAVE落盘目录核实、异常不重试与未保存会话保护。`git diff --check`通过。
- 静态与Mock测试覆盖定位READY、录线采首点、保存/丢弃；SAVE受理前同会话由READY降为ERROR时，兼容底层合法的IDLE终态并继续核实目录。拒绝把建图中的路线区间按独立录线会话收口。
- 停车确认修正最近完整稳定尾窗，保留原2秒上限、0.4秒稳定期、1.5cm容差及下游新鲜零速检查；停车期间维持原控制权，失败附 `stop_feedback` 原因。无新增实机停车验收。
- 本轮 dog4 CLI 实际发出一次恢复空闲请求；请求被受理，但底层运行时转入CONFLICT并返回4004：`external or duplicate cmd_vel publisher detected`。工具正确返回失败及操作编号，没有继续启动定位或录线，没有自动重试或清理。随后的只读图快照未发现速度发布者，冲突状态仍锁存，未据此强行解除。
- dog4 真实 MCP stdio initialize / tools/list / get_robot_status 成功，发现15个工具，读到当前CONFLICT、TF未就绪、无global body odom。原始雷达/IMU仍有新鲜数据（点云约0.20秒、IMU约1毫秒），时间源修复保持有效。
- 当前新增接口的实机成功终态验收被上述运行时冲突阻塞。之前临时脚本通过的定位/静态录线验证不能代替本轮正式接口验收；多点行走录线与SAVE、实际前进停车都留待用户在场。
- 遵照用户要求，本轮未发送任何起立、卧下、速度、导航或巡检启动指令，未修改已有地图/路线，未重启或强杀机器人进程。恢复请求的JSON和MCP只读结果保存在工作区 `output/dog4_analysis_20261105/`。

## 有界前进调试 — 2026-11-04 / 05（北京时间）

- 11 月 4 日离线测试 492 项通过，新增 `move_forward` MCP / CLI；当时实体前进尚未通过。
- 当晚站立成功；第一次前进遇到 SSH 连接异常，用户确认没有移动，之后才进行第二次调用。
- 第二次收到 `success:loco`，但连续 3 秒无有效进展，返回 `motion_stalled`，里程计相对位移 0.0257 m，`stop_confirmed=true`、`control_released=true`。这不是 1 m 前进成功；当晚最后收到卧下成功回包。
- 11 月 5 日核实原生 `LocomotionStatus.motion=0` 的枚举含义是 `MOTION_IDLE`，该字段没有 WALKING 枚举，不能由它推断未进入行走模式。模式检查需结合新鲜的 `posture` 和 `current_action`。
- 用户确认原配遥控器或原来的 App 可以正常前进；厂商 `/vel_cmd` 订阅 QoS 与 Bridge 输出匹配。实际前进距离和模式门禁修复后的结果另行记录。
- 11 月 5 日补充原生 `/locomotion/status` 的双时钟新鲜度和实际站立门禁、最多 8 条行动切换记录、下游速度命令摘要，以及明确的控制权获得记录。离线回归 503 项通过；`motion=0` 正常，行动名仅作记录，不硬编码 `RL_NORMAL`。
- 当日 0.10 m/s 试验：实际行动由 `FIXED_STAND` 切到 `RL`，138 个非零下游速度样本，最大前向命令 0.10 m/s，最终相对位移 0.0254 m，仍 `motion_stalled`。外部只读监控确认 teleop 与 `/vel_cmd` 对应，未发现其他源零速覆盖。
- 当日 0.20 m/s 试验：`RL` 状态，79 个非零下游样本，最大前向命令 0.20 m/s，相对位移 0.0070 m，仍 `motion_stalled`。两次均确认停车和控制权释放；随后卧下收到成功回包。下游命令样本不等于实体速度，均未完成 1 m。
- 用户随后确认原配手机 App 和申请权限后的 OmniDeck 均能让机器狗前进，测试指令期间仅站立。已排除未申请 Bridge 控制权和错误的 MODE_LOCO=2；尚无证据确认最低速度阈值，`motion=0` 本身也不能用于判定未进入行走模式。
- 手机入口为 X5 AP 的 `192.168.50.1:8765`，转发至 S100 Foxglove；原 Cudy 跳板地址一度不可达，PC 已通过保存的 `dog4-0008` 热点恢复连接。临时 SSH 跳板覆盖使用 `/tmp/omni-dog4-ap-bin/ssh`，未修改全局 SSH 配置，主机密钥仍严格校验。
- 厂商 `app_agent` 持续发布 `app_joy` 标记的全零 `/joy`；SDK 内部存在 enable_joy 仲裁，但实际是否覆盖本次命令尚未证实，未调用该开关。发布频率已对齐 OmniDeck 默认 10 Hz，尚待其真机试验。
- 通过 PC 只读采集用户 OmniDeck 成功控制：同一 APP 控制权服务和同一 `/omni/control/teleop → /vel_cmd` 通道，纯前向实际命令最高 0.394 m/s，约 2.2 s 对应里程位移约 0.637 m；另一段最高 0.344 m/s，对应约 0.332 m。实际发布约 9 Hz，Twist 其余三轴为零。整个成功窗口 `/joy` 仍全零，因此零 Joy 不能独自解释失败，未修改 SDK enable_joy。
- 下一次试验参数根据成功样本改为 10 Hz、固定 0.35 m/s，在约 1 m 的位移容差内停车；仍以最终实测距离和确认静止决定成功。首次准备调用因手机 APP 仍持有租约返回 `control_busy, command_sent=false`，等待手机释放后再测，没有抢占。
- **11 月 5 日实体动作链首次跑通**：用户释放手机租约后，CLI 起立收到 `success:stand`；前进使用 10 Hz / 0.35 m/s，依据 App 数据补偿停止尾移，最终里程位移 **0.9773 m**，下游 38 条非零命令，`control_acquired=true`、`control_released=true`。用户明确确认看到约 1 m 前移。
- 前进此次仍返回 `stop_unconfirmed`：自动静止判据没有在停车窗口内通过。已重复发送零速度并释放控制权，但工具没有将这一结果改写为成功。停车确认逻辑仍需排查，不能把“实体已前移”表述为前进工具全部验收通过。
- 随后 CLI 卧下收到 `success:lie_down`、`control_released=true`，用户明确确认实体卧下。由此完成“起立 → 约 1 m 前移 → 卧下”的现场演示。用户已明确当前优先跑通动作，不要求区分 1.0 m 与 1.1 m；前进距离验收容差设为 ±0.15 m，原有异常停车和控制权门禁保留。

## V3 / 0.3.0 — 2026-11-04（北京时间）

- 新增 MCP `stand_up`、`lie_down`，CLI `posture stand|lie` 和六个中文动作别名；默认只预览。
- 设计复用 dog4 已有 Bridge 姿态 Topic 和控制权服务，无需新增机器人部署产物。
- 完整离线测试：380 项通过，包含原有查询/HTTP 操作回归、中文别名、两种 MCP 传输、权限门控、独占租约、异常释放和禁止自动重发。命令：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p pytest_asyncio.plugin -q`。
- Python wheel 与 sdist 构建通过；本地虚拟环境补齐了 `setuptools>=68` 和 `wheel`。
- dog4 真实 CLI：`omni --timeout 30 起立 --execute --json`，2026-11-04 19:19:46（PC 北京时间）收到 `success:stand`，`completion=vendor_acknowledged`、`control_released=true`。
- dog4 真实 MCP stdio：完成 initialize、发现 10 个工具并调用 `lie_down(robot_id="dog4", execute=true)`，2026-11-04 19:20:42（PC 北京时间）收到 `success:lie_down`，`completion=vendor_acknowledged`、`control_released=true`。
- 测试后再次只读查询：runtime/robot 均为空闲，`control_authority=0`、authority available，无残留 APP/人工覆盖租约、无急停。两次各发送一次姿态指令，没有自动重发。
- 上述证明 PC CLI/MCP → SSH → Bridge → 厂商服务应答链路通过；狗端没有权威姿态遥测，因此两次结果均为 `physical_posture_confirmed=false`，实体站立和卧下需现场确认。
- 验证须分别记录厂商接口应答、租约释放结果，以及实体姿态是否有独立观测证据。`physical_posture_confirmed=false` 不能表述为已经观测到实体动作完成。
- HTTP 巡检和导航的实体端到端验收仍未完成。

## V2 / 0.2.0 — 2026-11-03（北京时间）

- 完整离线测试：282 项通过，包括 V1 回归、写操作权限、参数类型、HTTP 合同和入口集成。
- 官方 MCP SDK 实际完成 stdio 和本机 Streamable HTTP 握手，发现 8 个工具；三种操作的默认预览均被证明没有后台请求。
- CLI / MCP → OmniService → HTTPBackend 的集成测试使用内存 Mock 传输，验证巡检创建/启动、导航创建计划/提交目标及取消后的状态查询。
- 测试覆盖禁止写入、机器人不在允许列表、错误 ID、错误地图、过期计划、非法坐标、超时不重试、两阶段部分成功的恢复信息，以及已结束任务取消时的无操作结果。
- 返回 `dispatching`、`pending`、`cancelling` 时保留上游真实状态，不改写为已完成、已到达或已停止。
- 架构图和流程图完成语义审查与 PNG 视觉检查，颜色按职责分组；部署集成工程与运行时模块分别说明。
- V2 未连接真实 HTTP 后台执行操作，未向 dog4 发送巡检、导航、取消、控制权、姿态或速度请求。

第一版推送后的 GitHub Actions 已通过 Python 3.10 / 3.12 测试与打包；第二版也配置相同 CI。
此记录区分实体读取验证与离线操作验证，不能解释为实体巡检或导航已验收。

## V1 / 0.1.0 — 2026-11-03（北京时间）

- 离线测试：`env -u PYTHONPATH .venv/bin/python -m pytest -q`，81 项通过。
- 协议：官方 MCP SDK 客户端实际完成 stdio 和本机 Streamable HTTP 的 initialize、tools/list、tools/call；上游 HTTP/业务错误正确返回 `isError`。
- 打包：成功构建 Python wheel 和 sdist。
- 实体连接：`ssh dog4`，读取已有 Mission Manager 的 ROS Humble / Zenoh 环境，不安装或修改机器人文件。
- `get_robot_status`：收到 robot_state、mission_status、runtime_status、navigation_status、slam_status、control_authority_status 六类消息；当前 mode=0，未知电量为 JSON null。
- `list_maps` / `list_routes`：分别返回 2 张地图、1 条路线。本仓库不包含实体地图编号、路线内容或位置坐标。
- `get_task` / `get_inspection_result`：用专用不存在的查询编号验证，分别返回未观测到和空证据/存在性未知，未假定成功或完成。
- 全程未调用启动、取消、导航、控制权、姿态或速度接口。

实体查询只验证读取链路。HTTP 后台的真实地址和令牌未配置，因此 HTTP 适配器由依据现有源码合同构建的离线 Mock 验证。
现有部署产物是否与各工程最新 Git 提交一致，未在本版本中做完整校验。

运行测试时清除宿主机 `PYTHONPATH`，可避免本机 ROS 的 pytest 插件影响独立虚拟环境。CI 使用干净 Python 环境。
