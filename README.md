# MAA + Waydroid 自动托管与材料规划

面向明日方舟 **国服官服（Official）** 的无人值守启动器：在 Waydroid 中运行 MAA，完成基建、公招、信用商店、每周剿灭、材料刷图和奖励领取。根据活动窗口、库存目标和一图流效率确定刷图关卡，默认目标为每种活动蓝材料 200 个等价库存；允许使用两天内到期的理智药，禁用普通药和源石。

定时任务每天 02:00、07:30（Asia/Shanghai）执行完整流程，支持无人登录时的 headless 模式。正常流程由确定性程序执行和验收，失败时调用 LLM 恢复代理。

## Quick Start

前提：Linux + systemd，已完成 Waydroid 初始化，并安装、登录国服官服客户端；已具备项目 Codex SDK 可用的登录态。当前 systemd 单元与 adapter 路径按 `~/Projects/maa-waydroid` 配置，使用其他目录需先调整这些路径。首次部署细节见[运维手册](docs/operations.md)。

在项目根目录执行：

```bash
./scripts/bootstrap.sh
sudo ./scripts/install-network-fix.sh
sudo loginctl enable-linger "$USER"
./bin/maa-host install-core

waydroid show-full-ui
waydroid adb connect
./bin/maa-host probe
```

首次连接时，在 Android 中允许 ADB 调试并勾选“始终允许”，再确认 `probe` 通过。网络修复脚本会持久配置 Docker 转发策略，适用于当前可信家庭 LAN，详见运维手册。

```bash
./bin/maa-host doctor
./bin/maa-host run
```

受管运行要求 Git 工作树干净。手动运行成功后，启用定时托管：

```bash
./scripts/install-systemd.sh --enable
```

## 文档

- [运维手册](docs/operations.md)：部署、运行、定时器、更新回滚与排障。
- [策略与配置](docs/configuration.md)：库存目标、基建、剿灭与代理隔离。
- [完整文档索引](docs/README.md)：架构、开发、恢复契约与历史记录。
