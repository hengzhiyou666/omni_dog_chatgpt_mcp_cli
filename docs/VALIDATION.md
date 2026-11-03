# 验证记录

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
