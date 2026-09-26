# Phase 4 — Candidate Retry

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：in_progress（4/5，待真实设备重试验收）。依赖：Phase 3 验收完成。

## 进度

- [x] P4-01 — done：候选失败与系统失败分类。
- [x] P4-02 — done：单轮复用 Box 和候选快照。
- [x] P4-03 — done：候选数、真实战斗数及理智预算限制。
- [x] P4-04 — done：离线验证 A 失败 B 成功与系统故障立即停止。
- [ ] P4-05 — todo：授权后的真实设备重试验收。

## 完成记录

2026-09-26：P4-01–04 已实现。`maa_planner/copilot_retry.py` 提供严格预算和新鲜回调失败分类，`copilot_run.py` 复用单轮 Box/静态身份/候选快照，按原排序逐个重新核对并执行，`copilot_core.py` 输出安全的 worker 阶段收据。每次尝试有唯一 ID 和独立文件，Phase 5 三星观察仍逐尝试校验，不写能力账本。

协议核对使用 MaaCore v6.18.0 的 [BattleFormationTask](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/src/MaaCore/Task/Miscellaneous/BattleFormationTask.cpp) 与 [Assistant](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/src/MaaCore/Assistant.cpp)：`BattleFormationOperUnavailable` 只是信息回调，必须与未完成编队的 `OperatorMissing` 及失败链终态组合后才作为重试依据；缺失报告中的 `Unchecked` 不算确定缺失。MaaCore 失败任务同样可能产生 `AllTasksCompleted`，不能据此覆盖 `TaskChainError`。运行时异常、掉线和 ADB 故障优先于候选失败。

`tests/test_copilot_retry.py` 离线构造 A 编队失败 B 成功、候选内容变更后跳过、候选耗尽、三种预算限制、预算结算、系统故障/超时/中断/旧证据停止、跨尝试身份拒绝、实际 OCR/模板要求、重启失败和原始失败记录保留。与既有运行、三星证明测试共 40 项通过；完整离线回归 193 项通过（5 项可选环境测试跳过）。`git diff --check` 与修改文档相对链接通过。未运行游戏或调用账号接口。

默认仍只执行一个候选。显式参数的范围、理智预扣方式和审计路径集中维护在[运维手册](../operations.md#有限候选重试phase-4)。当前真实路线仍仅 NL-8；P4-05 必须在用户授权关卡及预算后验证。一次候选直接成功不冒充“A 失败 B 成功”的真实重试验收，未完成前整个 Phase 4 不标 done。

2026-09-26 预算纠正：核对[国服官方公告](https://ak.hypergryph.com/news/6277)，2025-08-02 起战败/放弃行动已全额返还理智，不限首次。初版“失败也永久占完整理智预算”的实现已修正：`RetryBudget.settle` 对每个执行预留只结算一次；明确未开战的候选失败标 `not_spent`，实际战败标 `refunded`，两者释放理智预留。二星通关和未知结果标 `charged_or_unknown`，不释放。`sanity_settlement` 保留结算依据和释放额，执行次数上限独立保持。测试覆盖 NL-8 18 理智预算内 A 战败后 B 成功、编队失败后 B 成功、二星不释放、掉线/超时/中断不释放、重复结算拒绝及免费失败仍受次数上限约束。Copilot 专项 45 项通过，完整回归 198 项通过（5 项可选环境测试跳过），文档相对链接和 `git diff --check` 通过；未启动游戏。

## 目标

从：

```text
pick one → try once
```

升级成：

```text
rank candidates
      ↓
try A
      ↓
candidate-specific failure
      ↓
reject A
      ↓
try B
```

---

## Failure Classification

至少区分：

```text
formation_missing_operator
formation_requirement_unsatisfied
formation_other_failure

copilot_schema_failure

battle_failed

navigation_failure
runtime_failure
adb_failure

unknown_execution_failure
```

核心原则：

### Candidate failure

可以换下一个：

```text
formation requirement
bad copilot
battle itself failed
```

### System failure

不能疯狂换作业：

```text
ADB down
Waydroid broken
MaaCore runtime broken
navigation broken
```

否则会把 infrastructure failure 伪装成“作业不好”。

---

## MaaCore feedback

优先消费结构化信息，例如：

```text
BattleFormationOperUnavailable
```

以及相应：

```text
oper_name
requirement_type
```

这些 feedback 可以反向补强 candidate rejection reason。

---

## 单轮 snapshot

一次 run 内：

```text
Box fetch once
PRTS query once
```

候选 A 失败之后：

```text
reuse same Box
reuse same candidate list
```

不要每次失败重新请求所有外部 API。

---

## Retry limits

必须有限制。

例如概念上的：

```text
max candidates = N
max real battles = N
```

最终值根据实际实验决定。

禁止无限循环。

---

## 验收标准

构造：

```text
candidate A → 明确失败
candidate B → 成功
```

系统能够：

```text
A
↓
record reject reason
↓
B
↓
success
```

整个过程不需要人工干预。

---