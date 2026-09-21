# LLM 诊断与整轮恢复

[文档索引](README.md) · [项目首页](../README.md)

说明三个 adapter 的职责、触发条件和审计方式。恢复代理的操作约束以版本化的[恢复 scope 与 FAQ](llm-recovery-scope.md)为准。

## LLM 诊断与整轮恢复

活动更新、MAA 结构化窗口校验、周剿灭排程与完成证明、库存、效率计算、选关、参数校验、代理能力判断和每个阶段的成功条件全部是确定性 Python/Shell 程序。LLM 不在正常热路径中；脚本已经取得完整强证据时，本轮模型调用次数严格为零。

这里有三个职责不同的入口：

- `[agent]` 是规划器的局部顾问。只有确定性 `plan` 已得到 `NOOP`，并且原因指向上游来源不可用、schema 变化、MAA 导航或一图流关卡映射缺失时才调用。它不能把 `NOOP` 改成 `FIGHT`。
- `[supervisor]` 维护 launcher 的整轮确定性账本。每轮先把预期阶段和 Git 版本写入不可覆盖的起始事件，随后只追加阶段终态。非完整模式和合成探针仍可调用原来的只读分类器。
- `[supervisor].recovery_*` 是完整 run 失败后的操作型恢复代理。cleanup 先写死失败终态并释放运行锁，再通过 Python Codex SDK 以 `Sandbox.full_access` 和 `ApprovalMode.deny_all` 启动一次完全无 sandbox、无命令审批的持久 thread；thread ID 写入该事故的忽略态目录，可供续跑和事后审计。它能用 shell/网络直接检查 DNS、Waydroid、ADB、游戏 UI、ANR、journal 和日志，处理游戏内资源更新与官服 APK 强制更新、点安全弹窗、等待/重启、受管 runtime 回滚，修复后反复执行新的完整 launcher。正常整轮仍然零模型调用。

规划顾问 stdout 是受限 JSON：

```json
{
  "schema_version": 1,
  "classification": "stage_mapping",
  "confidence": 0.8,
  "summary": "诊断说明",
  "mappings": [
    {
      "stage_code": "AT-8",
      "yituliu_stage_id": "act44side_08",
      "reason": "映射依据"
    }
  ],
  "evidence_refs": ["引用的证据路径或字段"]
}
```

`classification` 只能是 `stage_mapping`、`source_schema`、`source_conflict` 或 `unknown`；建议中的关卡必须来自确定性候选集。每次 `plan` 都会写入 `decision.evidence.agent`，明确说明是否符合调用条件、是否真的调用。成功建议和错误都只写审计，不改变本次 `NOOP`。

操作型恢复的目标不是“给建议”，而是“得到一轮新的完整成功”。事故输入直接附带失败时/恢复时 runtime receipt、保留上一代 Core、最近一次成功 full run、升级身份差异和逐阶段差异，避免模型只看到当前失败片段。原失败 run 永远保留 `failed`；恢复前后追加 `recovery-started` 和 `recovery-finished`，新 run 另建哈希链。即使模型声称已经修好，Python controller 仍会重新读取新 run 的完整事件链，并要求它在本次 `recovery-started` 之后启动、精确携带父 run ID＋128-bit attempt ID＋slot、使用 `full` 模式、所有预期阶段均 accepted、进程状态 0且最终事件为 `success`。历史成功 run 或只改写 `latest-run.json` 都不能冒充这次恢复。子 run 继承全部恢复身份环境变量，因此不会递归创建另一个恢复代理；同一个恢复线程继续查看新证据并重试。

恢复 Codex 没有 sandbox：模型生成的 shell 在当前用户权限下可直接访问文件系统和网络，也不会遇到 Codex 命令审批；它不会凭空获得 root，但已经存在的 `sudo -n` 能力可用于有证据、可回滚的临时网络修复。允许动作和停止条件由版本化的 [恢复 scope 与 FAQ](llm-recovery-scope.md) 定义，其中明确覆盖“正在获取更新”、游戏 ANR、DNS/CDN 差异、游戏内资源更新、从鹰角官方下载官服 APK 并保留数据覆盖安装、点 `Wait`、重启 Waydroid、受管 runtime 回滚，以及临时网络变更。若需永久修复，agent 可自选本地 repair branch，提交、验证、push 并向默认分支发 PR，但不能直接提交 base、force-push 或 merge。当前事故只允许 controller 应用既有、声明式的 `config/tasks/*.toml` 修复；修改 Python、Shell、执行入口、安全策略或审计生产者的 commit 可以发 PR，但不能用来证明当前事故已恢复。GitHub PR 还会通过 `gh pr view` 校验 open 状态、head branch/commit、base 和仓库归属；不可验证的 PR 会显著记录，却不抹掉已独立证明的运行恢复。它仍禁止无人值守登录/验证码/六星公招确认、清空游戏数据、购买、绕过签名校验、强制降级、切换客户端渠道、持久宿主网络策略改动和修改当前 scope/审计。

所有受管阶段都按可重入处理，包括 daily：无论父 run 已进入或完成基建、公招、商店，恢复代理都可重新执行一轮完整 launcher；`unsafe-stateful-replay` 不再是 blocker。每个 agent 内部子 run 失败后仍由同一恢复会话继续分析和整轮重试。

手动运行的终端挂断（SIGHUP）也会记录失败并进入整轮恢复；后续输出写入 `var/state/host/<run-id>-hangup.log`，cleanup 和恢复子进程忽略重复 HUP，不再依赖已断开的终端输入输出。只有启动器实际收到 SIGINT（如 Ctrl-C）或 SIGTERM 时才按显式停止处理，不自动重启任务，以保留 systemd 停止和槽位交接的语义。普通命令返回 129/130/143 本身不代表操作者取消，不能据此跳过恢复。嵌套恢复仍由已有恢复线程接管，不递归调用代理。

没有代理作战或关卡未开放属于局部游戏状态：自动模式应跳过该候选并尝试下一候选/常驻回退；用户明确指定的唯一关卡不可用，或所有合规候选/回退都不可用时，才允许以 `proxy-unavailable` 或 `stage-closed` scope blocker 结束。游戏内资源下载和官服 APK 更新都属于恢复范围；若商店要求人工账号操作，代理改用 `https://ak.hypergryph.com/downloads/android_lastest` 动态解析当期官服 APK，并通过 ADB replacement install 保留数据。

两个只读 adapter 的真实连通性仍用合成探针验证：

```bash
./bin/zootd advisor-check
./bin/zootd supervisor-check
jq . var/state/planner/latest-advisor-probe.json
jq . var/state/supervisor/latest-probe.json
```

两个探针都只发送合成故障，不读取真实游戏状态、不启动 Waydroid/MAA，也不修改基建或任何游戏数据；结果原子写入最近状态并按时间归档。恢复 adapter 不提供会触碰真实游戏的合成探针；可用 `./bin/zootd recover RUN_ID morning` 显式恢复一个尚未尝试恢复的失败 full run。`doctor` 只启动 SDK 自带的本地 runtime 并检查 SDK 版本与现有登录态，不自动发起模型请求。三个 adapter 固定使用项目 `.venv`，不再解析或执行外部 `codex` CLI。

三个 adapter 都通过官方 Python SDK 直接提交显式文本输入和 JSON Schema 结构化输出。顾问/分类器继续使用 ephemeral thread、空临时工作区、`Sandbox.read_only`，并关闭执行、联网、MCP、plugin 和 subagent 能力；恢复代理则使用可 resume 的持久 thread，在项目根目录附加本地图片，使用 `Sandbox.full_access` 与 `ApprovalMode.deny_all`，但关闭无关 connector/plugin/subagent。SDK 调用使用隔离后的最小环境，并由异步超时负责取消和关闭 runtime。默认恢复预算为六小时，仍受 service 的十小时总预算约束。返回后 Python 二次验证所有字段；超时、非法输出、虚构成功或未声明的 Git 工作树变化均按失败关闭。这里的 full access 是 operator 明确选择，实际停止条件来自本仓库 scope，而不是 Codex sandbox。[Codex SDK 官方说明](https://learn.chatgpt.com/docs/codex-sdk)

### Codex SDK 定期更新

`requirements.txt` 只声明 `openai-codex`，不设置版本上限或固定版本。`scripts/bootstrap.sh` 与 `scripts/update-codex-sdk.sh` 都通过项目 `.venv` 中的 pip 检查并升级到最新稳定版；SDK 自带的匹配 runtime 一起升级，不依赖全局 `codex` 命令。

`zootd-codex-update.timer` 每天 05:00（Asia/Shanghai，与香港同为 UTC+8）执行更新；关机错过不补跑。更新器取得 MAA 全局锁与 SDK 独占锁，任务或恢复代理忙时本次跳过，下次定时再试。三个 Codex adapter 执行期间持有 SDK 共享锁。更新后执行 `pip check`、SDK 导入和配套 runtime 版本检查，实际版本及错误写入 systemd journal；版本检查不等同于真实模型调用成功。

手动更新：`./scripts/update-codex-sdk.sh`。安装并启用定时器：`./scripts/install-systemd.sh --enable`。查看记录：`journalctl --user -u zootd-codex-update.service`。
