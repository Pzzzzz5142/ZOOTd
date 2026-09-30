# Phase 5 — Strong Proof + Capability Ledger

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：in_progress（实现与离线验证完成，战后代理实机证据待验收）。依赖：Phase 4 已于 2026-09-28 完成验收。

## 进度

- [ ] P5-01 — in_progress：已实现活动作用域、run/关卡/新鲜战斗证据；账号一致性由用户维护，运行前提示，待实机验收。
- [ ] P5-02 — in_progress：已区分三星观察、首次通关未知和零理智已保存代理证明；待实机验收。
- [ ] P5-03 — in_progress：已接入现有账本锁及幂等观察接口，保护人工隔离；待实机登记验收。
- [x] P5-04 — done：旧日志、错误身份、缺终态拒绝测试。
- [ ] P5-05 — in_progress：普通 Fight 仍走现有零理智 preflight；新增实验检查禁用全部开战/补充理智入口，待实机验收。

## 完成记录

2026-09-26 首批实现：`maa_planner/copilot_core.py` 为新采集的每条回调加上 run ID、连续序号和单调时钟时间；`copilot_proof.py` 在父进程记录的执行时间窗口内校验身份、顺序、作业加载、编队、战斗完成、三星模板识别和完整终态。`copilot_run.py` 将结论放入 `result.json` 的 `battle_proof`，同时保留 Phase 3 的 `execution` 终态语义。

三星识别依据 MaaCore v6.18.0 的 [CopilotTask](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/src/MaaCore/Task/Interface/CopilotTask.cpp) 与 [任务资源](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/resource/tasks/tasks.json)：只接受普通难度 `Copilot@WaitUntilEndOfAction` 链中的 `StageDrops-Stars-3.png` 模板识别完成回调，拒绝 adverse、sandbox、失败及旁路结果。Core 的设备 UUID 和任务 ID 可能跨进程复用，不能代替 recorder 分配的 run 身份。已有历史回调不回填新身份，不追认旧实验为当前证明。

P5-04 离线验收：`tests/test_copilot_proof.py` 覆盖旧日志、越界时间、重复/缺失/乱序事件、异设备/任务/关卡/文件、非法模板与类型、失败终态及助战；`tests/test_copilot_run.py` 验证完整模拟管线写入三星观察但不创建能力账本。两组共 22 项通过；完整回归 175 项通过（5 项可选环境测试跳过）。一次已有 watchdog 子进程退出时序测试失败，独立及完整复跑均通过。文档相对链接与 `git diff --check` 通过；未启动游戏或调用账号接口。

### 2026-09-28 继续实现

显式 `copilot-run --prove-capability` 在同一 Core、同一设备锁内执行：运行前账号提示 → 地图 → Copilot → 返回首页 → 地图/目标关卡 OCR → 已保存代理检查。每个导航和代理任务必须有匹配设备与 task ID 的完整终态，整个回调流仍要求当次 run、连续序号和父进程时间窗口。战斗终态单独截取，后续 Custom 完成不能覆盖战斗结论。2026-09-30 的突袭扩展不进入此证明链：`--raid --prove-capability` 在读取 Box 和启动设备前拒绝，突袭 `battle_proof` 保持 `unproven` 且不登记账本。

账号一致性按用户要求采用提示方式：每次运行前告知用户确保森空岛 Box 与当前游戏为同一国服官服账号，记录归入 `config/farming.toml` 的本地 account 别名；不等待交互确认、不读取游戏 UID、不做战前战后账号比对。已移除 `--bind-account`，不创建或读取旧的账号绑定文件。`account_binding=user_managed` 明确表示账号归属由用户维护，不能解读为自动核验成功。实际发生 Box、启动导航、编队、战斗、代理证明或登记错误时，CLI 输出相应中文提醒及非零退出码，结果 JSON 的 `message` 保存同一提醒。

常驻 SideStory 必须由安装的关卡表和地图索引交叉确认，采用 `permanent-sidestory-v1` 与 canonical zone 派生的独立 24 位作用域，不能复用原限时活动。普通 1-7/AP-5 使用既有 fallback 作用域；其他关卡复用现有新鲜活动快照验证，登记前重新检查活动尚未结束。仍只支持配置已有的导航路线。

`battle_proof.status=observed` 仍仅表示三星画面；`first_clear=unknown`，不追认首次通关。新证明模式仅允许 no-support，全部证据齐全时为 `verified`，`activity_binding`、`saved_proxy` 为 `verified`，`account_binding` 保持 `user_managed`，再经既有 `_locked_ledger` 与 `CapabilityLedger.mark_verified_observation` 登记。观察 ID 按唯一尝试 ID 派生，重复登记不增加成功数，不解除人工隔离。不建立第二套能力账本，不回填历史运行。

零理智代理 overlay 仅在战斗结束后加载，复用现有 `PROXY_RESOURCE` 对开战和药石入口的 Stop 限制；验证关卡 OCR 后再检查 `UsePrtsSuccess.png`。失败停止、不换候选、不额外战斗。后续普通 Fight 的已有 preflight 保持必经；本地能力记录不能替代客户端检查。

已知的 `NotUsePrts` 可忽略探测错误现在只在同一 Copilot、作业加载后、编队开始前豁免一次；其他失败仍拒绝。这与 [v6.18.0 的实现](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/src/MaaCore/Task/Miscellaneous/MultiCopilotTaskPlugin.cpp) 一致。历史 Phase 4 结果不修改或补记。

离线验证覆盖无 UID OCR/绑定即可完成模拟证明、旧绑定文件不影响运行、运行前非阻塞提示、各阶段错误提醒、作用域/设备错误、旧回调、缺少终态、代理缺失、助战拒绝、人工隔离保护、幂等登记、常驻活动映射及完整模拟命令到现有账本的流程。完整回归 221 项通过（5 项可选环境测试跳过），CLI help、Shell 语法、文档相对链接与 `git diff --check` 通过；本次调整未启动游戏。战后代理导航仍待实机验收，P5-01/02/03/05 保持未勾选。

## 目标

从：

```text
Copilot 看起来跑完了
```

升级成：

```text
ZOOTd 可以证明本次三星通关，并归入用户维护的本地账号别名
```

然后接入现有 capability ledger。

---

## 成功证据

至少绑定：

```text
current run
stage identity
fresh MaaCore evidence
battle result
copilot ID/hash
```

只有强证据成立，才能登记能力。

---

## 不允许

禁止：

```text
Copilot author says stable
→ success
```

禁止：

```text
maa-cli exit 0
→ success
```

禁止：

```text
old log contains three star
→ current run success
```

---

## 写入现有能力账本

不要建立第二套：

```text
copilot_success_database
```

Copilot 首通产生的最终事实仍然是：

```text
user-managed account alias X
+
stage Y
=
verified three-star capability
```

与现有代理能力使用同一个事实体系。

---

## 验收标准

首次 Copilot 成功：

```text
no capability
   ↓
Copilot
   ↓
three-star proof
   ↓
capability ledger
```

下一次运行：

```text
capability exists
   ↓
normal saved auto-deploy path
```

---

## 与现有仓库契约对齐

三星通关和已保存代理是不同事实。账本不能替代客户端代理检查；助战执行即使三星也不得直接登记可代理。后续普通 Fight 必须继续通过零理智 preflight，不能仅凭本地记录跳过。
