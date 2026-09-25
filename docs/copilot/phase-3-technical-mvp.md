# Phase 3 — Technical MVP

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：done。依赖：Phase 2 验收完成。

## 进度

- [x] P3-01 — done：实现显式 experimental copilot-run <stage> 入口。
- [x] P3-02 — done：刷新 Box、查询、匹配、选择和下载完整作业。
- [x] P3-03 — done：关卡与 runtime 校验、独占设备锁及有限战斗授权。
- [x] P3-04 — done：MaaCore 自动编队、执行和结构化终态。
- [x] P3-05 — done：保存身份与内容哈希、解释选择与失败。
- [x] P3-06 — done：用户明确选择关卡并授权后的真实设备验收。

## 完成记录

2026-09-25：P3-01–05 实现位于 `maa_planner/copilot_run.py`、`copilot_core.py`、`copilot_static.py` 和 `bin/zootd`。`config/copilot.toml` 提供禁止助战与允许一名助战两个 profile，默认禁止；允许时仍优先 exact。专项离线测试覆盖排序策略、全局编队约束、静态索引、常驻关卡映射、共享锁、缺失/乱序/异设备/历史终态和失败审计。P3-06 已以 NL-8 完成实机验证。完整离线回归 167 项通过（5 项可选环境测试跳过）。

2026-09-25 NL-8 实机验收：用户明确指定 NL-8 并允许借干员，执行 `./bin/zootd copilot-run NL-8 --profile allow-support`。当次 run 为 `20260925-211557-6a40b3176411`，代码提交 `04a0412`，MaaCore v6.18.0；程序自动选择作业 78392，兼容结果为 exact，因此本次未借干员。地图 ID 为 `act13side_08_perm`，作业地图 ID 为经安装数据绑定的 `act13side_08`。五名干员自动编队完成，随后完整战斗执行成功。`result.json` 的 loaded、formation_completed、battle_completed、chain_completed、all_tasks_completed 全部为 true，task_id=4，exit_code=0，errors=[]；371 条当次结构化回调保存在私有运行目录，不提交原始日志或账号数据。整个执行未接入 daily、恢复或能力账本，也未自动尝试第二候选。最初缺少主页到活动入口的开发验收在导航阶段超时，未进入战斗；其失败记录保留，未改写为成功。

阶段到此暂停；本次结果不作为 Phase 5 三星/代理能力证明，助战分支已通过离线策略与分配测试，本次实机未实际借用干员。

实现限定首个查询页最多 50 项、普通难度、一次战斗、20 分钟执行超时；下载后重新匹配，作业变更则拒绝。组内成员固定为全局匹配结果。地图旧 ID 必须由本机关卡表与 Tile overview 同时确认。当前自动导航路线限定 `config/copilot.toml` 中的 NL-8：终端 → 曲谱 → 乐章收录 → 长夜临光 → 进入活动，扫描次数有上限；其他关卡在启动前拒绝。只有前置导航链成功才提交 Copilot。静态技能使用游戏 character_table 的技能槽，模组使用 uniequip_table 的 charEquipOrder，不从 Box 排序推断；静态表地址与内容哈希随实验记录。

协议依据：[MaaCore 集成接口](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev/docs/zh-cn/protocol/integration.md)、[CopilotTask](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev/src/MaaCore/Task/Interface/CopilotTask.cpp)、[结构化消息](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev/src/MaaCore/Common/AsstMsg.h)。单项 copilot_list 启用导航与战斗等待；不把进程退出码或纯文本作业输出当作成功。操作方式见[运维手册](../operations.md#单次-copilot-通关实验phase-3)。

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