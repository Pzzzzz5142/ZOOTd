# Phase 3 — Technical MVP

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：todo。依赖：Phase 2 验收完成。

## 进度

- [ ] P3-01 — todo：实现显式 experimental copilot-run <stage> 入口。
- [ ] P3-02 — todo：刷新 Box、查询、匹配、选择和下载完整作业。
- [ ] P3-03 — todo：关卡与 runtime 校验、独占设备锁及有限战斗授权。
- [ ] P3-04 — todo：MaaCore 自动编队、执行和结构化终态。
- [ ] P3-05 — todo：保存身份与内容哈希、解释选择与失败。
- [ ] P3-06 — todo：用户明确选择关卡并授权后的真实设备验收。

## 完成记录

尚未完成。每项 done 需记录实现文件、测试/验收证据和日期；整个阶段只有全部验收通过才标 done。

## 目标

第一次真正跑通：

```text
Skland
  ↓
OperatorBox
  ↓
PRTS
  ↓
candidates
  ↓
matcher
  ↓
best Copilot
  ↓
MaaCore
  ↓
formation
  ↓
battle
  ↓
terminal result
```

这是第一个真正意义上的 MVP。

做到这里应该主动暂停一次，而不是继续堆功能。

---

## CLI

建议增加显式开发入口，例如：

```bash
./bin/zootd copilot-run <stage>
```

具体子命令风格跟随现有 CLI。

它始终由用户显式调用；本路线图不接 daily、timer 或自动恢复触发。

---

## 执行流程

### Step 1

刷新 Box。

### Step 2

查询当前关卡候选。

### Step 3

本地 matcher。

### Step 4

选择第一个：

```text
exact
```

如果不存在，再根据明确策略决定是否允许：

```text
support_one
```

MVP 可以先不自动执行 `unknown`。

### Step 5

获取完整 Copilot。

### Step 6

再次校验关卡身份。

### Step 7

保存到 runtime 临时路径。

禁止写入 Git 工作树中的受管配置。

### Step 8

调用 MaaCore/maa-cli：

```text
auto formation
+
Copilot execution
```

### Step 9

收集终态证据。

---

## 本次执行至少记录

```text
stage
box snapshot hash
box fetched_at
copilot ID
copilot content hash
compatibility result
MAA execution result
```

不要把完整 Box 到处复制进日志。

---

## 成功定义

不能只使用：

```text
process exit code == 0
```

需要 MaaCore/Copilot 有明确成功终态。

Phase 3 暂时可以只记录：

```text
success
failed
```

更严格的三星证明放 Phase 5；此处 success 仅指执行终态，不得登记代理能力。

---

## 验收标准

人工选择一关。

执行一条命令之后，不再人工选作业：

```text
自动读 Box
→ 自动搜作业
→ 自动匹配
→ 自动选择
→ 自动编队
→ 自动执行
→ 输出终态
```

日志可以回答：

> 为什么选择这个作业？

以及：

> 如果失败，失败在哪一步？

---

## 暂不做

Phase 3 明确不做：

- 第二候选自动 retry；
- capability ledger；
- daily integration；
- LLM recovery enhancement；
- dashboard；
- provider fallback。

---