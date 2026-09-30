# 历史事故与验收记录

[文档索引](README.md) · [项目首页](../README.md)

以下内容从旧 README 归档，描述事故发生或验收时的实现与观察，不代表当前运行契约。当前行为见[架构](architecture.md)、[策略配置](configuration.md)及[诊断与恢复](recovery.md)；代码演进以 Git 历史为准。

## 基线提交

仓库在引入异常监督器前先建立了 `4baa479`（`chore: preserve validated automation baseline`）基线提交；监督器实现另存为 `3249301`（`feat: add exception-driven LLM phase supervision`），没有覆盖此前已经通过真实 E2E 的实现。

## 2026-08-25 至 2026-08-28

2026-08-25 的 07:30 timer 实际准时触发，但当时尚未登录 Hyprland，旧启动器因缺少 `WAYLAND_DISPLAY` 在五秒内退出。现在 service 不再依赖 `graphical-session.target`，`auto` surface 契约覆盖这一场景。

同日的完整链路验证还暴露了旧编排会在尝试 Fight 后把“奖励对账”实现为第二次完整 daily，因而重复进入基建。现在 `Award` 已从 `daily.toml` 拆到唯一的 `award-only.toml`：正常成功路径只运行一次 daily，只有失败恢复才按可重入契约重试；最后无条件运行一次 Award-only。静态契约拒绝把 Award 放回 daily。

同日 20:36，maa-cli 的逐任务热更新把 live 资源推进到一个重写基建效率格式的新提交，而已安装稳定 MaaCore 无法解析其中的 `infrast.json`。该提交现已在隔离 dry-run 中稳定复现并被拒绝，live 保留前一份兼容资源。隔离候选提升机制和静态测试防止以后再次由普通 service 吞入未经验证的资源。

2026-08-26 的 03:00 与 07:30 日志证明 MAA 都没有进入训练室，但四间宿舍运行时 `m_notstationed_filter_enabled` 均为 `0`；宿舍因此能选中仍进驻训练室的干员。根因是旧配置把 `dorm_notstationed_enabled` 设为 `false`，以及文档错误地把“Training 不在 facility 白名单”当成了人员不会被跨设施改派。现在默认宿舍流程已被移出普通设施任务，改由未进驻-only 自定义宿舍阶段处理；静态契约与完成日志计数共同防止该假设再次回归。

同日的进程审计还发现，03:00/07:30 正式 service 在接触设备前分别启动 7 个 MaaCore dry-run，而代理 preflight 又为导航和画面确认各启动一次。现在完整 Core 兼容验证只属于 06:30 更新事务，正式 service 和 `doctor` 使用持久化 generation receipt；代理两步合并为一个 Core 任务链；在 operator 明确所有阶段可重入后，daily 允许一次直接重试，恢复代理也允许反复重跑完整链路。后续同一轮审计又移除了 Depot 前独立的 StartUp Core，并把最多三轮相同来源联网刷新收敛为一次。当时单活动关通常约为五个 Core；2026-09-06 改为零体力 preflight 加逐场真实事务后，不再承诺固定进程数，每笔会重新检查并记录代理结果。

2026-08-27 的真实 headless 验收先运行 Award-only，日志只有 StartUp 与 Award。随后 runtime 更新拒绝了仍与 stable Core 不兼容的最新 MaaResource，并把兼容 live overlay、fresh API cache 和当前受管配置密封为 schema 3。完整流程恰好产生 Depot、daily、单笔 Annihilation、单进程 proxy-preflight、Fight、最终 Award 六份 MAA 日志：Depot 在一个 Core 内完成 StartUp 并读取 79 项库存；daily 有两次 Infrast、四间 Dorm、两次 Recruit、一次 Mall 和零次 Training；Core 对四间受保护 Dorm 实际记录了四个 `m_notstationed_filter_enabled: 1`、零个 `0`。来源只联网刷新一轮；剿灭缺少周进度强证据时保留 unknown 并继续普通刷关；AT-6 的客户端 PRTS preflight 和八连三星 Fight 成功；最终 Award 日志没有任何基建、公招、商店、Depot 或 Fight 标记。流程结束后 schema 3 receipt 仍通过校验。

同日随后建立 Git 基线并加入异常驱动的 LLM 监督：正常强证据路径不请求模型；所有 launcher 阶段进入追加式哈希链，任一失败、降级、缺失或非零退出只在 cleanup 后请求一次只读诊断，并保持整轮失败。这样 LLM 接入可被真实探针证明，又不会让正常 daily 为九个阶段重复产生模型调用，也不会取得重跑 daily 或修改安全策略的权限。

该监督器的真实合成异常探针先发现 Codex 结构化输出不接受 `uniqueItems`，失败 probe 被保留；把唯一性改为 Python 二次校验后，下一次真实模型探针成功返回 `safe_to_retry_whole_run=false`。随后在 commit `3249301` 的 clean 工作树上完成两轮 headless 验收：Award-only 账本包含 runtime、device、Award、cleanup 四个 `succeeded`，日志只有 StartUp、Award 和总链完成；完整链路的九个阶段依次为 runtime/device/Depot/daily/source `succeeded`、剿灭 `policy-resolved(weekly-state-unknown)`、farming/Award/cleanup `succeeded`。Depot 仍为 79 项；daily 为 2/2 Infrast、4 Dorm、2 Recruit、1 Mall、0 Training，Core 对四间 Dorm 均记录 `m_notstationed_filter_enabled: 1`；活动候选没有新鲜三星证明时没有猜成功，AP-5 preflight 失败后由 1-7 三次三星回退取得实际掉落证明；最终 Award 无任何基建、公招、商店、Depot 或 Fight marker。两轮最终事件均为 `llm.invoked=false`，证明正常强证据路径没有模型开销；cleanup 后 Waydroid 为 STOPPED，schema-3 readiness 仍有效。

2026-08-28 的 07:30 run 在 Waydroid/ADB/分辨率和通用网络探针均通过后，游戏长期停在“正在获取更新…”，随后 Android 对 `com.hypergryph.arknights/com.u8.sdk.U8UnityContext` 报 input-dispatch ANR。Depot 没有完成，后续阶段因此都未开始。07:36 的 LLM 实际已经被调用且成功返回诊断，但当时 adapter 被固定为 read-only classifier，prompt 和 sandbox 都明确禁止执行命令，所以它只能写 postmortem，无法点 `Wait`、检查游戏实际 CDN DNS、重启 Waydroid或重跑。这个事故直接促成操作型恢复：同类故障现在应按 scope FAQ 修到一轮新 full run 成功，而不是在第一次诊断后退出。

## 2026-09-29：游戏数据导航与零战斗验收

移除逐关 `navigation.NL-8` 白名单。关卡目录改用同一 revision 的游戏 stage/zone/activity/retro 数据，按入口和分区生成任务，复用 MAA 原生选关、详情核验与滑动逻辑；本机配置、数据快照和运行证据均保存在忽略的 `var/`。这是当时的验收记录，当前命令见[运维手册](operations.md#单次-copilot-通关实验phase-3)。

- NL-9：`20260928-224229-08174eb2372c`，到达并确认 NL-9 详情页，未提交战斗任务。
- MN-EX-7：`20260928-233600-2854613a4e5b`，识别活动标题、切换 EX 分区、滑动选关并确认目标详情页，未提交战斗任务。两次成功均核对了当次有序 Custom 链与精确目标 OCR，失败尝试记录保留。
- DS-1：`20260928-225127-9b2c2062b649`，进入当期“逐影集趣”，识别“通关DP-1解锁”，返回 `stage_locked`。游戏数据中的目标前置链要求推进至 DP-4，因此未完成 DS-1 详情页验收，也没有自动战斗解锁。

活动艺术字、只显示 EX 图标的分区入口，以及带纹理背景的返回箭头已纳入通用处理。两个目标通过不代表所有特殊活动布局都完成实机验收；当期活动的开放窗口、游戏解锁状态和导航结果分别校验。

## 2026-09-29 至 09-30：有界地图 OCR 与实机验收矩阵

MaaCore v6.18.0 下移除固定坐标 OCR 窗口，地图回退使用上游模板约定、白字 HSV 预处理及反色后的同一预处理。没有添加逐关模板或修改 live runtime；发布版 Core 不暴露独立地图 OCR，推理调用使用同一 runtime 的模型和替换规则。以下均为本次实机 `navigate` 的新运行，证据要求为详情页精确编号、同一 Custom 链完成、最终任务列表和新鲜回调；离线 fixture 不计入矩阵。

| 关卡 | route.kind | 命中分支 | 回退 OCR / 地图滑动 | 整轮秒数 | 运行 ID |
|---|---|---|---|---|---|
| 1-7 | main | `upstream_hsv_white` | 1 / 0 | 32.3 | `20260930-050152-f59adf9bfaad` |
| 1-8 | main | `upstream_hsv_white` | 1 / 0 | 32.4 | `20260930-045059-4c592b112cea` |
| LS-5 | supplies | `maa_task_graph` | — | 23.0 | `20260930-045939-bcf46ba76c92` |
| AP-5 | supplies | `upstream_hsv_white` | 1 / 0 | 28.4 | `20260930-045848-46556db2973e` |
| SK-5 | supplies | `maa_task_graph` | — | 22.5 | `20260930-045917-26f88717de73` |
| NL-8 | archive | `maa_task_graph` | — | 59.2 | `20260930-045240-e62cbc99c106` |
| NL-9 | archive | `maa_task_graph` | — | 62.7 | `20260930-045339-abe6dc91dc5e` |
| MN-EX-6 | archive | `upstream_hsv_white` | 3 / 1 | 67.8 | `20260930-050044-3805a7462adb` |
| MN-EX-7 | archive | `upstream_hsv_white` | 3 / 1 | 69.4 | `20260930-045442-bb379aaff626` |
| DP-1 | activity | `hsv_dark` | 2 / 0 | 未单独记录 | `20260929-235425-ed96c1827f2d` |

`maa_task_graph` 表示原生任务图已确认详情页，表中不把未统计的原生 OCR/滑动写成零。DP-1 地图回退自身耗时 5.901 秒，深色字候选置信度 0.712，详情页编号置信度 0.941；没有地图滑动。运行目录位于本机 `var/state/navigation/<运行 ID>/`，其中保留截图、预处理图、OCR 结果及 `debug/asst.log`；汇总和原始结果哈希位于 `var/state/navigation-acceptance/20260930-0502.json`，均不提交账号运行状态。

失败与边界同样保留：

- AP-5 初次在资源列表偏右的位置找不到入口（`20260930-045156-eef98559baa3`）；增加复用 MAA 滑动几何的双向入口搜索后，上表复验成功。
- NL-EX-1（`20260930-045551-438af6fade0b`）的分区入口仅有图标，任务停留在普通图区；14 次回退 OCR、6 次地图滑动后返回 `stage_not_found_on_map`，未算成功，未添加位置特判。
- 活动于 2026-09-30 03:59:59 关闭，随后 DP-1、DP-2、DS-1 均返回 `stage_unavailable`，没有设备任务。因此本次没有补齐当期普通图/特殊图各多个样本。
- DP-1 实际作业 `20260929-235608-ebe02fbf7a8b` 被首次编队教学阻塞；手动清除教学后重新运行 `20260929-235909-1b10270cf200`，仍停在战场初始化，未留下完整终局证据。导航已验证，通关没有验证，不登记能力。
- 09-30 重登时的一张公告也经过人工关闭，随后从正常首页重新发起完整导航。05:00 SDK 更新短暂占用设备锁，两次请求被拒绝，更新结束后重新验证。前置人工处理与设备锁拒绝不计为导航成功。

本次完整离线测试共 246 项：241 项通过，5 项可选浏览器测试跳过。矩阵只证明列出的路径，不证明所有活动布局通用；图标分区、首次教学和 DP-1 的战斗初始化仍是后续验证范围。

## 2026-09-30：Copilot 突袭实机验收

这是提交 `1621c32`、MaaCore v6.18.0 下的当次观察。用户授权自行选择一关突袭实际验收，按 Box 和第一页候选选择主线 1-12；预选时 1-7 的公开作业查询返回 network 错误，未启动设备，1-12 有 5 个 exact 自有阵容候选。实际执行：

```bash
./bin/zootd copilot-run 1-12 --raid --profile no-support --max-candidates 1 --max-battles 1 --sanity-budget 9
```

运行 `20260930-165555-b9ebccece2fe` 只执行一个候选和一次战斗，作业 93962（difficulty=3），自有娜仁图亚，无助战，药和源石均禁用。所选突袭地图为 `main_01-12#f#`，单场费用/理智预留为 9，未追加其他战斗。原作业的 `stage_name=main_01-12` 保留不变，执行副本绑定到突袭 Tile；`is_raid=true`，两个文件及快照/回调哈希均复核一致。

262 条当次回调证明：详情页目标编号确认后，先识别并点击 `ChangeToRaidDifficulty`，序号 126 完成 `RaidConfirm` 的 `NormalDifficulty.png` 模板识别（score=0.997445），随后才开始编队。task_id=7 的作业加载、编队、战斗、任务链及全部任务完成均为 true，进程退出码为 0，无终态错误；序号 237 另观察到 `StageDrops-Stars-Adverse.png` 突袭通关图标（score=0.983474）。导航回退使用 `upstream_hsv_white`，3 次 OCR、1 次滑动，无人工点击或页面干预。

原作业 SHA-256 为 `aa5240e74091084e4733080fbe691b50a350b1db170ee2d072354d677d09b127`，执行副本为 `7f09ecda58f65d2aaf9cf038ce8220e010a64a53c02b248cf47ebf61dc882955`，原始回调为 `a03e6ce9bfba6db8db2cd7a7c043d52609ed2c19b92f4d2de4bbf6771bcf97d5`。原始文件与最后截图保留在私有 `var/state/copilot/<运行 ID>/`，不提交账号或设备日志；runtime receipt 保持不变。`battle_proof` 按当时契约仍为 `unproven / raid_saved_proxy_not_supported`，不登记普通代理账本。

此项完成 RAID-02 的单关实机验收，仅覆盖主线入口、自有阵容及一次成功战斗。没有验证常驻活动/EX 入口的突袭执行、突袭助战、失败重试/退款或普通模式状态复位，也不证明普通三星/已保存代理能力。
