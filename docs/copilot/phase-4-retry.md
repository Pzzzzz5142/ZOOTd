# Phase 4 — Candidate Retry

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：todo。依赖：Phase 3 验收完成。

## 进度

- [ ] P4-01 — todo：候选失败与系统失败分类。
- [ ] P4-02 — todo：单轮复用 Box 和候选快照。
- [ ] P4-03 — todo：候选数、真实战斗数及理智预算限制。
- [ ] P4-04 — todo：离线验证 A 失败 B 成功与系统故障立即停止。
- [ ] P4-05 — todo：授权后的真实设备重试验收。

## 完成记录

尚未完成。每项 done 需记录实现文件、测试/验收证据和日期；整个阶段只有全部验收通过才标 done。

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