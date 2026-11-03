# 验证记录

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
