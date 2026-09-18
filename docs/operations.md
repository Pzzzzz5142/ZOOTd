# 运维手册

[文档索引](README.md) · [项目首页](../README.md)

涵盖初始化、运行、定时托管、更新和状态检查。所有命令均从项目根目录执行；完整运行的阶段顺序见[架构文档](architecture.md)。

## 初始化

当前 systemd 单元使用 `%h/Projects/zootd`，`config/farming.toml` 中三个 adapter 命令使用绝对路径。迁移目录或用户时要同步调整这些路径。宿主需已有 Waydroid、Docker、Python 3 和 systemd；脚本不会代装游戏或完成账号登录。SDK 登录态需预先可用，`doctor` 会检查。

```bash
./scripts/bootstrap.sh
sudo ./scripts/install-network-fix.sh
sudo loginctl enable-linger "$USER"
./bin/zootd install-core
./bin/zootd doctor
./bin/zootd-planner sync
```

`bootstrap.sh` 安装项目本地的 maa-cli，并在 `.venv/` 安装 `requirements.txt` 声明的官方 Python Codex SDK；SDK 包自带同版本 Codex runtime，不再依赖交互 shell 中的 `codex` 可执行文件。在 Arch 上脚本还会按需安装 `android-tools`、`gamescope` 和 `jq`。`install-network-fix.sh` 一次性写入 Docker 原生的 `ip-forward-no-drop` 配置，并把当前 `FORWARD` 策略切为 `ACCEPT`；它不会重启 Docker。已有 `/etc/docker/daemon.json` 若缺少该选项，脚本会拒绝覆盖；需保留现有字段、手动合并该选项并验证后再运行。以后 Docker 启动时不会再把转发默认策略改回 `DROP`，启动器本身只检查联网，不再动态提权改 iptables。这个 Docker 选项是宿主机级配置，适合当前单网卡的可信家庭 LAN；如果以后把机器用作多网卡/VPN 路由器，应重新审查全局转发策略。Waydroid 需先完成 `waydroid init`，并在其中安装、登录国服官服明日方舟。

`doctor` 只检查依赖、无人登录条件和已提升 runtime 的 generation receipt，不再重复启动 7 个 MaaCore dry-run。所有 Core/资源/任务兼容 dry-run 只属于 `install-core`/每日 06:30 的隔离更新事务。

若新 Core/资源在真实设备上发生 dry-run 无法覆盖的语义回归，可执行 `./bin/zootd runtime-rollback`。它先用当前任务契约验证 `var/cache/MaaRuntime.previous`，再原子交换完整 runtime、复验并重新密封 receipt；失败会交换回原代际。

第一次连接时运行：

```bash
waydroid show-full-ui
waydroid adb connect
./bin/zootd probe
```

Android 弹出调试授权时勾选“始终允许”。`probe` 应确认 ADB 已授权并找到官服包。

## 旧版本名称迁移

项目目录和 systemd 单元前缀现统一为 `zootd`，显示模式变量为 `ZOOTD_DISPLAY_MODE`，Hyprland 窗口标签为 `zootd`。主命令为 `bin/zootd`，规划器为 `bin/zootd-planner`，诊断与恢复入口为 `bin/zootd-codex-*`，MAA 包装入口为 `bin/zootd-maa`；上游 MAA 二进制仍为 `.local/bin/maa`。GitHub 仓库名称和 Git remote 不随本地部署迁移。

已有部署应在任务空闲时按以下顺序迁移；安装器只安装新单元，不会自动清理旧名称的定时器：

1. 使用 `systemctl --user list-timers --all` 和 `systemctl --user list-unit-files` 确认本项目旧名称的单元，记录启用状态并备份单元文件。用 `systemctl --user disable --now <旧 timer 名称...>` 停用本项目所有旧定时器，包括仍存在的旧资源更新定时器；确认对应 service 均已退出且没有手动托管或恢复任务。
2. 将整个项目目录移动到 `~/Projects/zootd`，保留 `.git`、`.local`、`.venv` 和 `var`。不要修改历史日志、追加式审计、live runtime 或 generation receipt 中记录的旧路径。
3. 核对虚拟环境的激活脚本、入口 shebang 和 `pyvenv.cfg`，修正其中指向原目录的绝对路径；如需重建虚拟环境，先保留原环境和依赖版本。将 `config/host.local.env` 或外部启动配置中的旧显示模式变量改为 `ZOOTD_DISPLAY_MODE`。核对三个 adapter 命令指向新目录。
4. 删除已备份且停用的旧项目单元文件，执行 `systemctl --user daemon-reload`，再从新项目根目录执行 `./scripts/install-systemd.sh`。先保持新定时器停用，避免重复调度或 `Persistent=true` 立即追补。
5. 完成代码测试后，在干净工作树运行 `./scripts/run-daily.sh --dry-run`，确认迁移后的静态契约与原 generation receipt 有效；不要通过手改 receipt 绕过失败。最后按迁移前的启用状态恢复新定时器，使用 `systemctl --user list-timers --all` 核对下次触发时间。持久化 timer 的触发记录位于用户数据目录下的 `systemd/timers/stamp-<timer 名称>`；在启用新 timer 前，将原触发记录连同时间戳复制到新名称，保留真实调度历史，并确认不会意外立即追补。

## 手动运行与检查

受管运行要求 Git 工作树干净；手动完整运行成功后再启用定时器。配置修改涉及受管任务时，需要重新运行 runtime 更新事务并验证 receipt，详见[开发指南](development.md)和[运行时契约](architecture.md#maa-core-与资源一致性)。

```bash
./bin/zootd run
```

不刷理智、绕过活动来源同步和材料规划；仍会先读 Depot，以确定无人机应该加速赤金还是贸易站：

```bash
./scripts/run-daily.sh --no-farm
# 或
./scripts/run-daily.sh --farm off
```

其他入口：

```bash
# 只核对静态契约和持久化 generation receipt；不启动 MaaCore 或设备
./scripts/run-daily.sh --dry-run

# 只检查 Waydroid、ADB、720p 和网络，不运行游戏任务
./scripts/run-daily.sh --check-device

# 强制验证无人登录时使用的 headless surface
./scripts/run-daily.sh --display-mode headless --check-device

# 开发期轻量 E2E：只领取普通任务奖励，不进入基建、公招、商店、邮件或战斗
./scripts/run-daily.sh --display-mode headless --e2e-award

# 兼容标志：仍执行完整流程，使用零体力预检和相同的自动连战/失败重试规则
./scripts/run-daily.sh --verify-proxy

# 人工关卡覆盖：仍执行 Depot、daily 和最终 Award，刷图使用同一重试流程
./scripts/run-daily.sh --stage AT-8

# 仅在明确需要绕过统一编排时直跑旧式 MAA task
./bin/zootd raw-run [task] [profile]
```

设备、ADB、官服包或网络本身不可用时没有执行 daily 的基础条件，因此这类错误仍会让启动器失败。daily 首次执行失败或缺少完整证明时会直接重试一次；重试仍失败则返回非零，由整轮恢复机制处理。

## systemd 定时托管

四个 timer 固定按国服时区 `Asia/Shanghai` 运行。`06:00` 更新项目 Codex SDK 与配套 runtime，`06:30` 只做隔离的 Core/资源候选验证，不启动 Waydroid；游戏任务在每天 `02:00` 与 `07:30` 运行。两个游戏槽位的业务步骤完全相同：先做 Depot 和无人机决策，再运行同一份完整 `daily.toml`，刷新同一组完整规划来源，规划剿灭和材料刷图，最后只执行 Award-only。明日方舟在 `04:00` 切换游戏日，因此 02:00 清即将结束的游戏日，07:30 清重置后的新游戏日。为了不让 02:00 的旧游戏日任务跨过重置，它的刷图阶段共用 02:25 硬截止；这只缩短可用执行窗口，不改变任务顺序、无人机策略、候选回退或来源范围。剿灭只有在截止前仍容得下完整 30 分钟事务时才启动，不会为了旧周补救而中途打断一场。两个槽位都不依赖宿主当前设置的时区。安装并启用：

```bash
./scripts/install-systemd.sh --enable
```

两个游戏槽位使用独立 service，避免同一个 oneshot 吞掉第二次触发。02:00 service 使用 `--pre-reset-slot`（隐含 `--daily-first`）；普通启动只接受 `02:00`–`02:04`，休眠后的过时触发会直接跳过。若本轮确定性审计失败，controller 注入 `MAA_RECOVERY_ACTIVE=true` 的同一 `--pre-reset-slot` 命令可在 `02:05`–`02:24` 做一轮受控恢复重放；`02:25` 起拒绝新重放。原 service 的 50 分钟运行上限和 4 分钟清理上限仍保证在 04:00 前退出。07:30 service 使用 `--post-reset-slot` 和 `Persistent=true`，但在 `02:00`–`04:00` 保护窗内不做追补。两个入口都会先停止仍占用设备的另一个定时槽，再获取全局锁；每个槽位内部只启动一个 Waydroid 会话，并在整轮结束时统一关闭。06:30 runtime timer 使用 `Persistent=false`，避免开机补跑更新与 07:30 游戏任务争锁；候选失败只保留 live 旧版，不影响游戏 service。

定时任务不要求当时已经登录 Hyprland。安装器会确认 systemd user lingering 已启用，使 user manager 和 timer 能在开机后、登录桌面前运行。`ZOOTD_DISPLAY_MODE=auto` 会在存在有效 Hyprland socket 时显示原有的 1280×720 浮动窗口；无人登录时使用 Gamescope 官方 `headless` backend 提供同尺寸 Wayland surface。该 compositor 只属于本轮 service，结束时与 Waydroid 会话一起回收，不修改 Waydroid 系统脚本或防火墙。可用 `headless` 强制无人值守模式，或用 `desktop` 在没有图形会话时明确报错。

无人托管依赖以下持久条件，`./bin/zootd doctor` 会一起检查：

- `loginctl show-user "$USER" -p Linger` 为 `yes`；否则未登录时 user timer 不会运行。
- 当前运行内核存在 `/usr/lib/modules/$(uname -r)`。Arch 更新内核包但尚未重启时，Waydroid 可能因无法加载 `nft_masq` 而在网络初始化阶段失败；这不是 Docker `ip-forward-no-drop` 配置回退，重启进入新内核即可。
- `/etc/docker/daemon.json` 持久包含 `"ip-forward-no-drop": true`；启动器不在每轮动态改防火墙。

```bash
systemctl --user start zootd.service
systemctl --user status zootd.service
systemctl --user list-timers zootd-codex-update.timer zootd-runtime-update.timer zootd-prereset.timer zootd.timer
./bin/zootd logs
```

停用：

```bash
systemctl --user disable --now zootd-codex-update.timer zootd-runtime-update.timer zootd-prereset.timer zootd.timer
```

## 更新与回滚

```bash
./bin/zootd runtime-update
./bin/zootd runtime-rollback
./scripts/update-codex-sdk.sh
```

Core 和资源按完整代际验证、原子提升或回滚，详见[运行时一致性](architecture.md#maa-core-与资源一致性)。SDK 更新和模型探针见[诊断与恢复](recovery.md)。

## 排障入口

先查看 `./bin/zootd logs`、对应 service 的 journal 和下面的阶段账本；以失败 run 的日志和证据定位问题。

- 初始化或无人登录失败：检查 `doctor` 输出及上面的 systemd 持久条件。
- 活动关返回 `NOOP`：检查决策的 `reason`，对照[拒绝条件](architecture.md#fail-closed-条件)和[库存及代理策略](configuration.md)。
- 更新后回归：检查 runtime receipt，使用受管 `runtime-rollback`。
- 登录、游戏更新、ANR 或 DNS/CDN 故障：参照[恢复 scope 与 FAQ](llm-recovery-scope.md)。

## Planner 命令与审计状态

```bash
./bin/zootd-planner validate-service-readiness
./bin/zootd-planner validate-runtime-contracts
./bin/zootd-planner sync
./bin/zootd-planner sync-calendar
./bin/zootd-planner plan --offline
./bin/zootd-planner plan-annihilation --offline
# 仅在游戏中核对已满额后执行，YYYY-MM-DD 必须是当前游戏周的周一
./bin/zootd-planner confirm-annihilation-complete --week-start-game-day YYYY-MM-DD --reason user-verified-in-game-weekly-cap
./bin/zootd-planner capabilities
./bin/zootd-planner quarantine --stage AT-8 --reason manual-investigation
```

自定义配置是全局参数，必须放在子命令前：

```bash
./bin/zootd-planner --config config/farming.toml plan --offline
```

主要状态：

- `var/state/planner/latest-sources.json`：最近来源快照、窗口、效率和哈希证据。
- `var/state/planner/latest-activity-calendar.json`：剿灭独立使用的 MAA 活动日历及哈希证据，不依赖一图流。
- `var/state/planner/inventory.json`：最近完整 Depot 快照。
- `var/state/planner/capabilities.json`：账号、活动实例、关卡维度的三星审计和非三星隔离账本。
- `var/state/planner/annihilation.json`：客户端观测到的本游戏周进度/满额证明，或显式的当前周人工满额确认。
- `var/state/planner/annihilation-confirmations/`：按周绑定的人工满额确认审计副本；不包含伪造的客户端进度。
- `var/state/planner/latest-annihilation-decision.json` 与 `annihilation-decisions/`：当前及历史剿灭排程证据。
- `var/state/planner/latest-decision.json`：手工规划的最近决策。
- `var/state/planner/launcher-<timestamp>.json`：启动器本次使用的决策。
- `var/state/planner/decisions/`：按生成时间归档的历史决策。
- `var/state/planner/latest-advisor-probe.json` 与 `advisor-probes/`：最近一次及历史 LLM 真实连通性探针；探针不启动 Waydroid 或游戏。
- `var/state/supervisor/runs/<run-id>/events/`：每轮阶段终态、证据文件路径/大小/哈希、代码 Git HEAD 与工作树身份；事件只允许新增并由 `previous_event_sha256` 串成哈希链。
- `var/state/supervisor/latest-run.json`：最近一轮索引；完整历史始终以对应 `runs/<run-id>` 目录为准。
- `var/state/supervisor/latest-probe.json` 与 `probes/`：异常监督器的合成连通性探针，不启动 Waydroid、MAA 或游戏。
- `var/state/runtime/maa-resource.json`：最近候选/当前 Core 版本、资源提交、组合选择、验证结果、generation fingerprint 及回滚原因。
- `var/cache/planner/http/`：响应正文和带请求身份、ETag、时间及 SHA-256 的 metadata。
- `var/state/host/*-pre-daily-depot.log`：02:00 与 07:30 在 daily 前取得、同时用于无人机和材料规划的本轮唯一 Depot 日志。
- `var/state/host/*-e2e-award.log`：只领取普通任务奖励的轻量 E2E 探针日志；必须仅含 `Award Completed` 与总链完成证明。
- `var/state/host/*-award-final.log`：正式 service 最后的同一 Award-only 任务日志，同样禁止出现基建、公招、商店、邮件或战斗任务。
- `var/state/host/*-depot.log`：仅在前置 Depot 尚未尝试时由材料规划补取的库存日志；`*-proxy-preflight-<stage>.log`、`*-annihilation-*.log`、`*-farm-<stage>.log`、`*-daily.log` 分别记录单进程客户端代理 preflight、剿灭、各候选刷图和日常执行。
- `var/state/debug/asst.log`：用于验证本次三星完成的 MaaCore 核心日志。

安全拒绝是正常业务结果，因此 `plan` 写入 `NOOP` 时通常仍退出 0；调用方必须读取 JSON 的 `decision` 和 `reason`。`sync`、`validate-config` 等操作失败才使用非零退出码。

## 日志保留

项目日志按最近修改时间滚动保留 7×24 小时。`zootd-log-cleanup.timer` 每天 05:30（Asia/Shanghai）清理，关机错过后补跑；运行、更新或恢复忙时跳过，下次再试。通过 `./scripts/install-systemd.sh --enable` 安装并启用。

清理范围包括 `host/` 日志、`debug/` 日志与截图、历史规划和探针，以及已结束的 supervisor run 和对应 recovery 目录。审计按整轮删除，不截断或改写单条事件。持续追加的 `asst.log` / `asst.bak.log` 在空闲时移入 `debug/archive/`，保留原始内容和修改时间，下次 MAA 运行重新创建日志；归档同样按 7 天过期。刚启用时，已有聚合日志内部可能包含更早的行，随整个文件过期清除。

当前状态、库存、能力账本、runtime/receipt 和缓存不清理。未结束的 run、最近一次成功 full run、当前状态或保留审计引用的证据及关联 run 会延长保留；带 repair worktree 的事故也保留，避免删除未交付的修复。存在未结束的恢复时整次清理延后。孤立的 recovery 目录保守保留，需人工确认归属。系统 journal 由宿主 journald 管理，不更改其他服务的保留策略。

预览和手动执行（不启动游戏）：

```bash
./bin/zootd log-cleanup --dry-run
./bin/zootd log-cleanup
systemctl --user status zootd-log-cleanup.timer
```

## 上游资料

- [maa-cli 使用文档](https://docs.maa.plus/zh-cn/manual/cli/usage.html)
- [maa-cli 配置文档](https://docs.maa.plus/zh-cn/manual/cli/config.html)
- [MAA 理智作战文档](https://docs.maa.plus/zh-cn/manual/introduction/combat.html)
- [MaaCore Fight 参数协议](https://docs.maa.plus/zh-cn/protocol/integration.html)
- [Waydroid 文档](https://docs.waydro.id/)
- [Waydroid 网络问题排查](https://docs.waydro.id/debugging/networking-issues)
- [Docker 转发策略配置](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [明日方舟一图流](https://ark.yituliu.cn/)
