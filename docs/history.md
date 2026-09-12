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
