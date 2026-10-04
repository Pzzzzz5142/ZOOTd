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

`doctor` 只检查依赖、无人登录条件和已提升 runtime 的 generation receipt，不再重复启动 7 个 MaaCore dry-run。所有 Core/资源/任务兼容 dry-run 只属于 `install-core`/每日 05:30 的隔离更新事务。

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

不刷理智、绕过活动来源同步和材料规划；仍会先校验当前游戏日的库存快照，缺失时读 Depot，以确定无人机应该加速赤金还是贸易站：

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

设备准备阶段复用现有 Waydroid 会话前，会实际连接该会话报告的 Wayland display socket；仅有 `Session: RUNNING` 或残留 socket 文件不足以证明图形服务可用。若图形服务已退出，启动器先有界停止旧会话、确认会话停止且旧 surface owner 已释放锁，再重建并负责本轮退出清理。停止失败或锁仍被占用时拒绝继续，不删除 socket 或强杀其他 compositor。健康的外部会话仍可复用；恢复进程被强制终止留下的失效会话由下一轮设备准备阶段处理。

设备、ADB、官服包或网络本身不可用时没有执行 daily 的基础条件，因此这类错误仍会让启动器失败。daily 首次执行失败或缺少完整证明时会重试一次。每次 daily 执行都会以 0.5 秒间隔检查本次独立日志；出现明确的 `GameOffline` 回调后立即中断 MAA，最多等待 5 秒便强制回收该次进程组，不再等待基建、公招、商店逐项超时。首次离线会先关闭官服游戏进程、停止并重建 Waydroid 会话，再重新检查 ADB、分辨率、官服包和网络，然后执行唯一一次完整 daily 重试。非离线失败保留原来的直接重试。会话修复失败、重试仍离线或缺少完整证明时，才由整轮 LLM 恢复接管；不会清除游戏数据、修改 DNS/代理或自动处理账号验证。首次与重试日志分别保留。

## systemd 定时托管

四个 timer 固定按国服时区 `Asia/Shanghai` 运行。`05:00` 更新项目 Codex SDK 与配套 runtime，`05:30` 只做隔离的 Core/资源候选验证，不启动 Waydroid；游戏任务在每天 `06:00` 与 `18:00` 运行。早上取得每日库存，晚上复用当天快照，具体有效性和失败处理见[基建无人机](configuration.md#基建无人机)。两轮都先决定无人机目标，再执行完整 `daily.toml`、刷新完整规划来源、规划剿灭和材料刷图，最后执行 Award-only。游戏仍在 `04:00` 换日。两个槽位都不依赖宿主当前设置的时区。安装并启用：

```bash
./scripts/install-systemd.sh --enable
```

两个游戏槽位使用独立 service，避免同一个 oneshot 吞掉第二次触发。06:00 的 `zootd.service` 使用 `--morning-slot`，timer 保持 `Persistent=true`；18:00 的 `zootd-prereset.service` 使用 `--evening-slot`，timer 保持 `Persistent=false`。晚间单元保留旧文件名以兼容已安装部署，其当前语义是晚间托管。两轮均允许 9 小时 50 分钟运行、另留 4 分钟清理；正常 18:00 触发最晚在次日 03:54 清理结束。原凌晨专用的启动分钟限制、刷图截止和恢复重放截止已取消，恢复受外层 service 的剩余运行时间约束。两个入口都会先停止仍占用设备的另一个定时槽，再获取全局锁；每轮只启动一个 Waydroid 会话，并在结束时统一关闭。05:30 runtime timer 使用 `Persistent=false`，避免开机补跑更新与游戏任务争锁；候选失败只保留 live 旧版。升级后运行 `./scripts/install-systemd.sh` 重装单元即可保留已有 timer 启用状态并加载新时刻。

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
- `var/state/host/*-pre-daily-depot.log`：06:00 在 daily 前取得、同时用于无人机和材料规划的每日 Depot 日志；18:00 复用其快照。
- `var/state/host/*-e2e-award.log`：只领取普通任务奖励的轻量 E2E 探针日志；必须仅含 `Award Completed` 与总链完成证明。
- `var/state/host/*-award-final.log`：正式 service 最后的同一 Award-only 任务日志，同样禁止出现基建、公招、商店、邮件或战斗任务。
- `var/state/host/*-depot.log`：仅在非晚间槽且前置 Depot 尚未尝试时由材料规划补取的库存日志；`*-proxy-preflight-<stage>.log`、`*-annihilation-*.log`、`*-farm-<stage>.log`、`*-daily.log` 分别记录单进程客户端代理 preflight、剿灭、各候选刷图和日常执行。
- `var/state/debug/asst.log`：用于验证本次三星完成的 MaaCore 核心日志。

安全拒绝是正常业务结果，因此 `plan` 写入 `NOOP` 时通常仍退出 0；调用方必须读取 JSON 的 `decision` 和 `reason`。`sync`、`validate-config` 等操作失败才使用非零退出码。

## 日志保留

项目日志按最近修改时间滚动保留 7×24 小时。`zootd-log-cleanup.timer` 每天 04:30（Asia/Shanghai）清理，关机错过后补跑；运行、更新或恢复忙时跳过，下次再试。通过 `./scripts/install-systemd.sh --enable` 安装并启用。

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

## Web 监控面板

内网只读 Web 面板展示早晚托管 systemd 服务状态、MAA Core receipt 版本、历史运行结果和每轮阶段详情。历史直接读取 `var/state/supervisor/runs/`，校验事件哈希链，并以完整阶段及终态判断通过；不以 `latest-run.json` 或进程退出码替代运行证据。恢复后的重跑单独展示，原失败记录保留。阶段数据含已记录的刷图结果和证据文件路径，不提供原始日志下载。

```bash
./bin/zootd dashboard --host 0.0.0.0 --port 8765
```

概览页直接展示最近一轮的完整阶段记录与过程时间线，无需点击跳转；顶部“定位本轮记录”只滚动到该区域。历史查询继续使用“历史记录 → 运行详情”的页面层级。边栏高亮与当前页面同步，详情作为历史记录的子页面展示；顶部标题、面包屑和“返回历史记录”保持同一层级。返回列表保留当前页、搜索和筛选条件，以及本次页面会话内的滚动位置。浏览器前进/后退可切换页面，详情 URL（`#run/运行ID`）可直接打开；手机上保留导航入口。刷新整个网页会重置列表查询条件。

MAA Core 卡片同时显示本地 receipt 中的版本和上游最新稳定版。上游数据来自 [MAA 官方 GitHub Releases](https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases/latest)（GitHub latest release，不含预发布）；使用独立只读接口 `/api/maa-release` 查询，不阻塞本地状态加载。服务进程内缓存成功结果 30 分钟，查询失败后 5 分钟内不重复请求，单次连接超时为 5 秒。失败时保留并标注上次结果和查询时间，不将旧数据称为最新；没有旧结果时显示查询失败，仍可打开官方发布说明。只对标准稳定版号作数值比较，预发布或未知本地版本不作比较。此功能不安装版本或修改运行环境；重启面板会清空内存缓存。

阅读单轮记录时，先查看“已记录 / 应有阶段”和分段状态条，再进入详情：

- 阶段统计分别列出通过、策略完成、无需执行、异常和尚无记录。阶段数表示已保存的终态数量，不表示实际战斗次数、执行中的阶段数或耗时百分比；“无需执行”不会计入“通过”。
- 阶段导航可直接展开对应卡片；支持筛选异常与缺失记录。异常阶段默认展开，设备、关卡、无人机用途等已记录字段直接展示；证据文件路径与原始 JSON 按需展开，自动刷新保留展开与筛选状态。
- 过程时间线按事件时间展示开始、阶段终态、结束和恢复事件。现有审计没有阶段开始或阶段内逐步事件，面板不推测阶段耗时、当前正在执行的阶段或未记录的中间操作。终态时间是记录时间，不是阶段开始时间；恢复事件不改变原始运行结果。
- 证据异常的记录不显示可信阶段统计。“哈希链已校验”仅指事件链校验，不表示重新读取和校验每个证据文件。

交互设计参考 [NN/g 渐进披露](https://www.nngroup.com/articles/progressive-disclosure/)（摘要优先、证据按需展开）、[Carbon 进度反馈](https://carbondesignsystem.com/components/progress-bar/usage/)（仅量化已知进度）和 [Grafana 日志展示](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/logs/)（时间顺序与独立详情）。

内网设备打开 `http://服务器内网IP:8765`，服务器本机也可打开 <http://127.0.0.1:8765>。页面每 10 秒刷新；支持分页、当前页状态筛选、运行 ID 搜索及阶段详情。概览将“当前运行状态”“最近运行结果”和“服务历史失败”分开展示：服务退出后即为空闲，systemd 保留的 failed 状态单列服务名、退出时间、结果及退出码；最近结果始终取全局最新审计记录，不随历史翻页或筛选变化，也不会跳过未结束或证据异常的最新记录。服务状态仅对应 `zootd.service` 与 `zootd-prereset.service`，手动启动的任务不属于该状态；未结束记录不代表进程仍存活。连接失败保留并标记旧快照。历史保留期沿用日志清理策略，默认七天；面板不创建额外历史副本。runtime 版本是最近 receipt 中的记录，不代表实时重新验证。

注册为用户级 systemd 服务（默认仓库路径 `~/Projects/zootd`）：

```bash
install -Dm644 systemd/zootd-dashboard.service ~/.config/systemd/user/zootd-dashboard.service
systemctl --user daemon-reload
systemctl --user enable --now zootd-dashboard.service
systemctl --user status zootd-dashboard.service --no-pager
journalctl --user -u zootd-dashboard.service -n 50
```

`./scripts/install-systemd.sh` 也会安装面板 unit，但不会自动启用面板。如需无人登录时持续运行，按前述部署步骤启用用户 lingering。改端口可用 `systemctl --user edit zootd-dashboard.service` 覆盖 `ExecStart`（先写空 `ExecStart=`，再写 `/usr/bin/python3 -m maa_planner.dashboard --host 0.0.0.0 --port 新端口`），随后重启服务。停止面板：`systemctl --user disable --now zootd-dashboard.service`。

面板默认监听 `0.0.0.0:8765`，允许通过服务器任意 IPv4 地址或主机名访问，无需登录，适用于可信内网；无需新增 Python 或 Node 依赖。如需仅本机访问，将 `--host` 改为 `127.0.0.1`。面板不启动游戏、不修改 runtime 或审计记录，也不提供执行任务的接口。

## 森空岛登录与 Box 同步（experimental）

这是用户单独调用的实验功能；不接入 `run`、daily、定时器或自动恢复，不启动 Waydroid、MAA 或游戏。未来查找并执行 Copilot 也只允许显式实验命令触发，进度见 [Copilot 路线图](copilot/README.md)。`copilot-run` 为单次真实战斗实验，见下文。

在自己的交互终端、项目根目录运行：

```bash
./bin/zootd box-login
./bin/zootd box-sync
```

`box-login` 正常显示手机号和短信验证码输入，方便本机核对；发送一次验证码，完成认证后保存登录 token。手机号和验证码不保存；程序不打印 token、cred、签名 token 或远端错误原文，不接受命令行凭据值和管道输入。不要把这些值粘贴到聊天或代理工具调用中。首次使用前应已在官方森空岛绑定明日方舟国服官服角色。

凭据文件为 `var/secrets/skland.json`（0600），父目录 `var/secrets` 为 0700，均在 Git 忽略目录中；该文件是依靠文件权限保护的本地明文，不是加密保险库。不要把它收集进日志或提交。失败的重新登录保留上次凭据；token 失效时重新执行 `box-login`。不自动发送验证码、不自动重试登录。若服务器要求额外风控验证，先到 [森空岛官网](https://www.skland.com/) 完成，命令本身不会绕过验证。

可选手动导入已有登录凭据（同样在终端隐藏输入，不写到参数中）：

```bash
./bin/zootd box-login --token
# 或已有 Skland cred 时：
./bin/zootd box-login --cred
```

浏览器获取 token 的备用方式：先在森空岛官网登录，再在同一浏览器访问 [鹰角登录凭据接口](https://web-api.skland.com/account/info/hg)，将 `data.content` 只粘贴到 `--token` 的隐藏提示中。[开源客户端操作参考](https://github.com/xjwwjx/skland-auto-sign#1-获取-token)。手机号登录接口与签名依据见 [Phase 0 调研](copilot/phase-0-skland-box.md#协议调研与模型调整)，第三方协议可能变化；当前真实账号验收状态以路线图为准。

`box-sync` 只读取绑定与干员练度；唯一官服角色自动选中，多官服角色必须显式指定 `--uid UID`。成功后仅打印数量、时间、内容哈希和路径，最小化的规范 Box 保存在 `var/state/operator-box.json`（0600）；含账号命名空间但不保存完整 player response。失败返回非零并保留旧文件，旧文件不代表这次同步成功。错误类别区分 authentication、permission、network、protocol、no_official_account、account_selection、account_mismatch、input 和 storage。

真实验收时在本机查看该规范快照，对照游戏核对一个六星的精英化、等级、有专精的技能和已开启模组；记录核对是否通过即可，不上传凭据或完整原始响应。缺失字段 null 表示未知，不能当作 0 或满足作业条件。此命令仅做只读同步，不改变现有 runtime receipt 或能力账本。


## PRTS 作业查询（experimental）

独立只读实验命令，使用已安装 MAA 的关卡表；不启动游戏，不接入 daily、planner、timer 或恢复。无需森空岛登录。

```bash
./bin/zootd copilot-query 1-7 --limit 3
./bin/zootd copilot-query main_01-07 --page 2 --limit 10
# 仅获取用户选中的一个 ID，并重新校验所属关卡
./bin/zootd copilot-get 102455 --stage 1-7
```

query 输出轻量候选 JSON（ID、标题、干员、分组、练度要求、评分和分页信息）；get 输出完整作业 JSON。示例 ID 来自 2026-09-23 的公开验收，远端可删除或修改，应使用当次查询的 ID。命令不会自动保存下载内容或执行作业；如需保存可重定向到 `var/` 下自行指定的文件。

关卡 code 有歧义时使用 MAA canonical stageId；本机关卡表缺失或过旧时按正常 runtime 更新流程处理。`empty_result` 表示当前页为空，`stage_mismatch` / `schema_error` 表示协议或身份验证失败，均非可执行候选；错误 JSON 写入 stderr，退出码为 1。阶段记录见 [Phase 1](copilot/phase-1-prts-client.md)。


### 单次 Copilot 通关实验（Phase 3）

```bash
# 禁止借干员（默认配置）
./bin/zootd copilot-run NL-8 --profile no-support
# 允许缺少一名干员时借助战，仍然优先 exact 自有阵容
./bin/zootd copilot-run NL-8 --profile allow-support
# 显式授权突袭，可同样选择助战策略和有限重试参数
./bin/zootd copilot-run MN-EX-7 --raid --profile allow-support
# 用户明确允许补药时，按需使用理智药，仍禁止源石
./bin/zootd copilot-run DV-EX-1 --profile allow-support --use-sanity-potion
```

两种助战策略内置，默认 `no-support`，本机可通过忽略的 `var/config/copilot.toml` 设置 `default_profile` 和 `profiles`（见[配置说明](configuration.md)）。命令默认授权普通难度战斗，`--raid` 显式授权突袭，可消耗所选难度的关卡理智；默认禁用所有理智药和源石。仅在用户明确允许补药时添加 `--use-sanity-potion`，启用 MAA 原生按需补药（包含普通药，不限制到期天数）；源石入口始终被 Stop overlay 禁用，不能通过参数授权碎石。整轮 `authorization.medicine` 为 `as_needed` 时表示补药授权，不表示已使用数量；理智预算仍限制战斗费用，不限制药品补充的理智。候选数和执行次数限制不变，NL-8 失败注入验收不接受补药。默认不执行第二候选，显式重试参数见下文，不调用自动恢复。保持工作区干净，先完成 `box-login` 并具备已验收的 MAA runtime；命令自动刷新 Box、查询前 50 个结果、匹配、下载选定作业、自动启动设备和编队。PRTS 视频攻略不作为可执行候选。

导航不再读取逐关白名单。游戏关卡、分区、活动及常驻活动数据从同一上游 Git revision 获取，保存到 `var/cache/copilot-navigation/catalog.json`，缓存最多使用 6 小时；过期刷新失败则停止，不回退到旧活动窗口。关卡代码支持大小写、空格和连字符的无歧义别名，例如 `nl 9`、`ds1`、`mn ex 7`。普通难度与突袭身份分开；`--raid` 要求游戏数据确有 `FOUR_STAR` 关卡及匹配的普通版本，仅有 EX 编号或手写 `#f#` 不构成授权。使用相同 code 或普通 stageId 选关时，程序按参数解析到对应难度；未加 `--raid` 时拒绝突袭 stageId。当前扩展不涵盖独立的 `TOUGH` 磨难环境。当期活动和分区必须处于开放窗口；常驻活动从其自己的分区数据生成路线，EX 不沿用普通区。`stages.json` 只用于兼容现有刷图身份，不再决定关卡是否存在；执行作业仍要求安装的 Tile 地图与所选难度身份一致。

候选按 [MAA 作业协议](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev/docs/zh-cn/protocol/copilot-schema.md) 的 `difficulty` 过滤：普通模式保留未声明、0、1、3，突袭只接受明确声明 2 或 3 的作业，下载后再次核对。PRTS 查询仍使用普通关卡搜索 ID，原作业保留不变；执行副本绑定所选难度的精确 Tile stageId，MAA 参数的 `is_raid` 与本轮授权一致。普通模式会在存在突袭版本的关卡详情页确认/切回普通，避免沿用上次突袭状态。

自动执行排除标题中独立的“半自动”标签（如 `【自用/半自动/改良】`），排除原因记录为 `semiautomatic_title`，不占设备执行名额。其余作业仍按确定性匹配和热度排序；未标注的作业不因此被保证可通关。静态数据明确确认无技能的干员，其无技能等级约束的默认 `skill=1` 在执行副本中规范为 0；未知映射不使用这一例外，原作业不改写。

作业替换组在满足作者编码要求的自有干员中，优先所选技能等级、精英化和等级更高者，最后按 canonical ID 排序；仍使用全局分配避免重复占位。缺失专精只保留已知主技能等级作排序，不推断练度要求或自动培养干员。

本轮已用新鲜回调确认战败、零星或二星失败后，执行副本 SHA-256 完全相同的其他候选会在设备执行及理智预留前跳过，记录 `duplicate_execution` 和原作业 ID。跳过仍占候选检查名额；执行次数和理智预算不增加。去重只绑定当前 Box/查询快照，新一轮可以重新尝试同一作业，旧失败记录保持原样。

遇到已知不适配作业时，可重复添加 `--exclude-copilot-id ID` 排除；`--copilot-id ID` 只执行指定候选。选择范围仍是本轮前 50 个、难度匹配且未标注半自动的查询结果，并保留 Box 匹配、下载复核、地图绑定与预算限制。指定 ID 不在范围内时停止，不直接下载绕过查询；指定和排除同一 ID 拒绝。授权和排除原因写入本轮记录，失败注入验收不允许覆盖候选选择。

PRTS 同一列表中的双模式作业可能使用突袭 Tile ID。仅在游戏数据已确认普通/突袭配对时，作业内容解析可将这个 ID 绑定到同一关卡；仍按 `difficulty` 筛选并绑定授权难度的执行地图。此内容别名不开放普通导航命令的突袭 ID 输入；其他关卡、不存在的突袭版本和歧义身份仍拒绝。

突袭需要先三星通关普通关卡以解锁。启动 Copilot 前，程序先执行独立的零战斗模式检查：在同一完成的 Custom 链内确认目标编号和已经切入突袭的模板，再允许追加作业。同设备的具名截屏耗时或帧率通知可在 Custom 完成后才到达，它们只作设备统计，不替代任何关卡、模式或终态证明。入口未解锁、切换点击超限、确认缺失或终态不完整均以 `raid_unconfirmed` 停止，不自动代打普通或换候选；截图保留在本次运行目录。Copilot 内部的切换超限也必须转入 `RaidConfirm`，不能直接当成完成继续编队；战后成功判定仍要求同一 Copilot 链在编队前给出原生确认。突袭不支持 `--prove-capability` 或 NL-8 失败注入验收，`battle_proof` 保持 `unproven`，不登记普通代理能力。1-12 自有阵容单场实机验收的范围见[验收记录](history.md#2026-09-30copilot-突袭实机验收)；MN-EX-7 暴露的旧模式保护缺陷及修复验证范围见[事故记录](history.md#2026-09-30mn-ex-7-未解锁突袭误打普通)。新开战前检查已通过离线回归与真实 Core 资源加载检查，尚未重新执行实机战斗。

可单独验证导航，不查询作业、不编队、不开战：

```bash
./bin/zootd navigate 'nl 9' --plan       # 只解析并保存路线，不启动设备
./bin/zootd navigate DS-1 --plan --refresh
./bin/zootd navigate NL-9
./bin/zootd navigate DS-1
./bin/zootd navigate MN-EX-7
```

`navigate` 与托管、更新、Copilot 共用设备锁。每次计划、任务 overlay、回调和结果保存在 `var/state/navigation/<run>/`；零战斗模式屏蔽开始战斗和用药入口。导航必须收到本次目标关卡详情页的精确 OCR、同一 Custom 链完成及最终任务列表证据，扫描结束或地图前缀不算成功。作业执行复用相同导航，在进入战斗前完成目标核对。地图回退复用已安装上游模板约定、白字 HSV OCR 和深色字分支；每幅图最多两次 OCR，最多 6 次地图滑动，连续两次画面近乎不变会提前停止。`map-vision-<序号>/` 保存截图、预处理图、OCR 结果、命中分支、扫描耗时和失败原因；这些记录用于实机验收，不代表所有关卡已验证。Python 依赖见 `requirements.txt`，worker 使用项目 `.venv`。特殊活动的首次进入费用可能与 `apCost` 不同，预算采用游戏数据中的较高费用。

元数据可覆盖新关卡，页面识别仍受游戏布局、活动解锁及分区可达性影响；有界扫描无法确认目标时停止并保留失败证据，不声称已经到达。活动元数据提供的前置解锁提示会沿前置链收集；页面识别到这些提示时明确返回 `stage_locked`。每次设备运行保留最后截图 `navigation-final.png`（私有运行目录），便于区分入口、分区、锁定和 OCR 问题。

旧活动底部分区选择栏可能只在暗色英文副标题末尾写 `EX`，也可能把字母与装饰图标放在一起。程序优先在底栏内匹配独立的 EX 字形模板，点击框只覆盖字母；未匹配时再用该栏未经白字 HSV 过滤的词 OCR，并统一 EX 大小写。字符 OCR 会把相邻区域图标合并进点击框，已移除这条点击路径。模板只复制到每次导航或 Copilot 的独立资源层，不修改 live runtime。零战斗导航的旧到期药确认别名使用无模板的停止动作，其余原生按钮仍按原来的条件识别后停止。点击一次后留在该区域找关卡，不再重按仍可见的底栏；点击后仍须确认目标关卡详情的精确编号，底栏标识不作为到达或解锁证明。

部分旧活动 S 区只显示棋子图标，没有游戏数据中的分区文字。常驻活动的 S 关卡在文字入口未命中时，可在底栏内识别独立棋子模板并点击一次，然后继续寻找关卡；图标不证明目标已到达，也不替代精确关卡详情与突袭模式检查。模板同样只安装到本轮独立资源层。

`SPECIAL ACCESS CONTENT` 隐藏关详情页使用不同的标题和开始按钮位置。导航须在同一已完成的 Custom 链中依次观察页面标识、可用的“开始行动”按钮和精确关卡编号；单独识别编号或条件按钮不算成功。英文标识使用安装的字符 OCR 模型；StartUp 若将详情页关闭图标识别成公告关闭，关闭后继续检查首页、公告和有限返回。零战斗导航只观察开始按钮。实际执行在该证据成立后，为本轮隔离 Core 调整原生 Copilot 的详情按钮 `StartButton1`、开战前按钮 `BattleStartPre` 和标题识别区域，保留其精确编号校验、开始流程与禁源石保护；不修改 live runtime。

常驻活动中的零理智加密关若仍以 `TOP-SECRET` 隐藏编号，导航最多打开一次可见加密入口并查看条件页。条件未完成则保存本次 `navigation-final.png` 并以 `stage_locked` 停止，不追加 Copilot，不点击解密或开始按钮；条件页出现可用的“事件重构”按钮时最多点击一次，随后仍须重新取得目标关卡的精确详情证据才能开战。匿名入口、条件页和重构点击本身不算成功，零战斗模式依然屏蔽战斗和用药入口。

允许助战只接纳 `support_one`，不执行 `unknown` 或多名缺失；固定每个 group 的已匹配成员后，由 MaaCore 补齐唯一缺失位置。静态匹配和作者省略的练度要求不保证实际可用或通关，助战实际可用性由设备执行决定。查询页内先 exact、再 support_one，各档按已有评分与 ID 稳定排序；不会扫描全站或人工挑选作业。

每次运行在 `var/state/copilot/<run-id>/` 保存一次 `snapshot.json`（候选页、排序、Box 哈希及静态来源）和整轮 `result.json`。每个候选在 `attempts/<run-id>-NN/` 独立保存 `selection.json`、原始/固定编队后的作业、任务参数、`callbacks.jsonl`、`worker-result.json` 和 `result.json`；失败文件不覆盖。目录私有，不提交。结果记录 Box 哈希和时间而非完整账号数据，保留作业、静态数据、回调哈希。

整轮 `success` 要求至少一个候选产生当次相同任务和设备的加载、编队、战斗、任务链及全部任务完成回调，加上正常进程退出；仅代表执行终态，不写代理能力账本。整轮结果中的 `attempts` 保留所有失败及成功，`stop_reason` 解释停止原因；每次尝试的 `failure.category`、`failure.retryable`、`failure_phase` 与日志用于诊断。不能把失败链发出的 `AllTasksCompleted` 当成功。

Phase 5 的 `battle_proof` 位于各尝试结果：默认 `observed` 只表示当次三星模板观察，`unproven` 表示证据不足，不登记账本。显式证明模式如下：

MAA 跳过战中剧情后可能发出 `SkipThePreBattlePlot` 的 ProcessTask 错误。只有同一已开始战斗链先完成跳过按钮与确认按钮的模板识别和点击，才将其视为已完成剧情跳过的尾部错误；无前置识别、错误身份、乱序、重复尾部错误及其他错误仍拒绝。随后仍必须收到本次完整战斗、三星模板和任务终态，剧情跳过不能代替通关证据。

MAA 技能列表向下滑动的辅助 ProcessTask 可能使用默认任务 ID 0。证明模块只在同设备、已加载作业且正在编队时接受该具名 Swipe/JustReturn 辅助回调；它不能代替属于本次真实任务 ID 的编队、战斗、三星或终态证据，错误回调和其他默认 ID 回调仍拒绝。

```bash
./bin/zootd copilot-run NL-8 --profile no-support --prove-capability
```

此模式默认只授权一次战斗、最多 18 理智，药石禁用；仅允许 no-support。每次运行前提示用户确保森空岛 Box 与游戏当前登录的是同一国服官服账号，不等待确认、不识别或比对游戏 UID。能力记录归入 `config/farming.toml` 的本地 account 别名；`account_binding=user_managed` 表示账号归属由用户维护。已移除 `--bind-account`，旧账号绑定文件不再读取或写入。

三星、活动作用域、当次零理智关卡/已保存代理检查齐全才记为 `verified` 并写入现有账本；首次通关保持未知，人工隔离不自动解除。Box 同步、启动导航、编队、战斗、代理证明或登记失败会显示对应中文提醒、返回非零退出码，并在结果 JSON 的 `message` 保存提醒，结合 `failure_phase`、`error` 与 `run_dir` 排查。程序不声称能够自动发现游戏账号不一致。证明失败停止，不追加战斗。战后代理路线仍待实机验收，详情见 [Phase 5](copilot/phase-5-proof.md)。后续普通 Fight 仍须通过既有零理智 preflight。

新回调带唯一尝试 ID、连续序号及单调时钟，旧回调不补写或追认。设备和 runtime 更新整轮共用独占锁，每个 worker 上限 20 分钟；只关闭本命令启动的 Waydroid surface。此功能不接入 daily、timer、planner 或恢复 controller。

中断后可能遗留零星结果页，原生 StartUp 的一次点击不足以离开该页。navigator/Copilot 在 StartUp 前共用独立 Custom：先排除三星及突袭成功图标，两星结果立即 Stop 并结束整轮；明确“任务失败”文字页最多点击一次后检查零星；只有明确零星模板才允许最多三次原生空白处点击。其他画面直接交还原生 StartUp，目标关卡和难度仍须重新证明；这条清理不开始战斗、不用药石、不证明新通关或退款。

#### 有限候选重试（Phase 4）

```bash
# 显式授权：最多考察 3 个候选、进入设备执行 2 次、总理智上限 18
./bin/zootd copilot-run NL-8 --profile allow-support --max-candidates 3 --max-battles 2 --sanity-budget 18
```

候选数范围 1–5、战斗执行次数范围 1–3，默认均为 1。允许两次及以上执行时必须显式给出 `--sanity-budget`（0–999）；省略时预算为所选难度游戏数据的单场 `apCost`，特殊首次费用取较高值。缺失或歧义费用会在 Box/设备启动前拒绝。每次设备执行前仍按完整关卡费用预留一场。只有当次完整失败证据确认编队/作业解析失败且尚未开战，或普通/突袭模式确认实际战败、普通模式确认完成漏怪撤退时，才释放这次理智预留。国服自 2025-08-02 起，失败和放弃行动全额返还理智，不限首次（[官方公告](https://ak.hypergryph.com/news/6277)）；普通和突袭都要求同链原生“任务失败”证据，或从准确作业加载、编队、战斗到零星识别及完整终态齐全的失败链；突袭还要求编队前确认实际模式。通用执行错误或孤立的零星图标不作为退款证明。

零星战败通过每次运行独立资源覆盖层中的灰色星标模板识别，完成原生结算返回后仍判定作业失败；同设备同任务的新鲜模板观察和完整终态齐全，才允许预算内换候选。零星图标必须绑定准确作业加载、完成编队、本次战斗开始与完成、原生任务链及 AllTasksCompleted 终态，才作为明确战败释放预留；不再要求额外出现可能被跳过的“任务失败”文字页。孤立、旧、错误或不完整证据不释放；突袭另须本次模式确认。任务失败/零星与二星或三星并存属于矛盾结果，停止重试。不修改 live runtime 或历史失败记录。

漏怪停止作业时，游戏可能先完成自然战败。新 Custom 优先 Stop 两星及意外成功页，只有本次原生失败文字或零星模板才进入失败返回；仍要求蓝标后红标截图、原作业停止、准确关卡返回及完整终态，记录 `leak_raced_to_defeat`，不伪造退出或放弃点击。

使用助战的零星返还页可能询问添加好友，阻止空白处返回。仅在本次明确零星观察后，再以 OCR 确认询问文字、原生通用叉号模板匹配取消按钮，最多拒绝一次后返回；开始恢复和漏怪返回共用这条路径，两星及成功页不进入。撤退证明另需同链零星及询问证据，不接受孤立取消点击。

普通撤退后的失败页最多点击两次，明确零星页面最多观察并点击空白处三次，每次等待页面切换。若返回地图而非详情，最多以原生字符 OCR 点击一次完全匹配的目标编号，再观察开始按钮和准确关卡标题；不滑图、不开始战斗，地图编号不能替代详情证明。特殊详情开始按钮只规范已知的加号前缀。证明只接受指定的清理动作及次数，超限或未知点击即使有 Custom 完成通知也拒绝退款重试。

二星通关不能按战败释放理智，也不算 Copilot 通关目标完成。普通 Copilot 默认监测 MAA 缓存战斗画面：同链编队和战斗开始后，先观察原生蓝色生命图标，再遇到红色漏怪图标立即停止作业，通过独立、有限步数的 MAA Custom 点击退出和放弃行动。只有截图哈希、时序、原作业停止、新 Custom 的放弃点击或真实战败分支、准确关卡返回及完整终态齐全才释放预留并换候选；`early_abort` 独立记录，原作业仍是失败，不能成为三星证明。原生停止回调使用 `TaskChainStopped=10004`；`10003` 是额外信息，不能作为停止证明。仅本次请求后的同设备同任务停止可接上独立撤退任务，其他停止通知仍拒绝。突袭漏怪会直接导致任务失败，不启用这条普通保护。若保护未赶上、已到二星画面，Stop 覆盖层禁止继续点击结算，停止整轮并保留全额预算；已经产生的游戏扣费不能撤销。轮询无法保证捕获漏怪与结算紧邻的每一种时序，不能声称绝对零浪费。超时、掉线、中断、旧/不完整回调等未知结果保留整场预留并停止。`sanity_reserved` 是已消耗或尚未排除消耗的预算，`sanity_released` 是有依据释放的累计预留，不是直接读取客户端余额。每个 `sanity_settlement` 只结算一次，释放理智不归还 `battle_reservations` 执行名额，因此免费失败也不会无限重试。NL-8 中 A 明确战败、B 成功可在 18 理智预算内完成；若 A 二星通关，再打 B 则仍需额外预算。候选内容变更、删除等在设备执行前被排除时只占候选名额。

整轮只刷新一次 Box、静态身份和候选页，各候选完整作业只下载一次，并重新核对轻量快照和匹配结果。只有可信的编队缺失/练度失败、作业编队解析失败或明确已结算战斗失败才允许自动尝试下一候选；泛化编队错误、BattleProcess 错误、网络/协议/关卡身份错误、导航失败、ADB 故障、掉线、超时、证据缺失或中断均立即停止。未知原因不会换候选。失败分类依据及进度见 [Phase 4](copilot/phase-4-retry.md)。

前一个 worker 正常退出且明确可重试后，下一次设备执行通过 Android `am start -W` 确认客户端位于前台，由新的隔离 Core 执行 StartUp 后独立返回主界面，并核对本次首页模板证据，再重新导航和编队；避免 Waydroid 强制停止游戏后旧进程无法退出的问题。StartUp 或地图证据不通过就停止，不从失败页面直接开战。整轮持有设备锁；预算耗尽时不会启动后续 worker。中断后不会续用历史快照或预算；重新调用是一次新授权和新运行。

原生编队中的助战职业选择、列表移动、刷新和详情确认可产生默认任务 ID 0 的辅助回调。三星证明和失败分类只在本次已加载作业、真实编队执行期间，按同设备、具名入口、前置任务、动作与对应模板校验这些回调；它们不能替代编队、战斗或完整终态。助战未找到仍须收到真实 `OperatorMissing` 编队失败及完整终态，才允许在预算内换候选；泛化错误仍停止。

Phase 4 实机验收可显式使用受控编队失败注入（仅 NL-8）：

```bash
./bin/zootd copilot-run NL-8 --profile allow-support --max-candidates 2 --max-battles 2 --sanity-budget 18 --acceptance-formation-failure
```

此开关默认关闭，只允许上述次数及理智预算。候选 A 必须为 exact，程序在已固定的阵容中选择一名自有五星或以下干员，仅对执行副本设置精二 90 级要求，使 MaaCore 在真实编队时拒绝；候选 B 使用未注入的原作业。无合适干员时在设备执行前停止。原作业、注入描述及执行副本分别保存和绑定哈希，Box、原始回调、live runtime 和历史结果不修改。`acceptance_passed` 只有在 A 被实际归类为练度不足或缺失、失败证据指向注入干员且确认未开战、B 自动执行成功时为 true；单次直接成功不算重试验收。此结果验证受控编队失败恢复，不代表已实测所有自然战败或助战失败分支。
