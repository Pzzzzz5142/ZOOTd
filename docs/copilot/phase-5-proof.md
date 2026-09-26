# Phase 5 — Strong Proof + Capability Ledger

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：in_progress。原依赖：Phase 4 验收完成；2026-09-26 按用户要求先实现 Phase 5 的单次运行证据层，Phase 4 重试仍为 todo。

## 进度

- [ ] P5-01 — in_progress：绑定账号、活动实例、当前 run、关卡和新鲜战斗证据。
- [ ] P5-02 — in_progress：区别三星首通与已保存代理，助战结果不得冒充代理能力。
- [ ] P5-03 — todo：通过现有受控接口登记能力，不建立第二套账本。
- [x] P5-04 — done：旧日志、错误身份、缺终态拒绝测试。
- [ ] P5-05 — todo：后续正常代理仍通过零理智 preflight。

## 完成记录

2026-09-26 首批实现：`maa_planner/copilot_core.py` 为新采集的每条回调加上 run ID、连续序号和单调时钟时间；`copilot_proof.py` 在父进程记录的执行时间窗口内校验身份、顺序、作业加载、编队、战斗完成、三星模板识别和完整终态。`copilot_run.py` 将结论放入 `result.json` 的 `battle_proof`，同时保留 Phase 3 的 `execution` 终态语义。

三星识别依据 MaaCore v6.18.0 的 [CopilotTask](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/src/MaaCore/Task/Interface/CopilotTask.cpp) 与 [任务资源](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/resource/tasks/tasks.json)：只接受普通难度 `Copilot@WaitUntilEndOfAction` 链中的 `StageDrops-Stars-3.png` 模板识别完成回调，拒绝 adverse、sandbox、失败及旁路结果。Core 的设备 UUID 和任务 ID 可能跨进程复用，不能代替 recorder 分配的 run 身份。已有历史回调不回填新身份，不追认旧实验为当前证明。

P5-04 离线验收：`tests/test_copilot_proof.py` 覆盖旧日志、越界时间、重复/缺失/乱序事件、异设备/任务/关卡/文件、非法模板与类型、失败终态及助战；`tests/test_copilot_run.py` 验证完整模拟管线写入三星观察但不创建能力账本。两组共 22 项通过；完整回归 175 项通过（5 项可选环境测试跳过）。一次已有 watchdog 子进程退出时序测试失败，独立及完整复跑均通过。文档相对链接与 `git diff --check` 通过；未启动游戏或调用账号接口。

当前 `three_star=true` 仅表示这次作业执行中的三星画面观察，不证明首次通关、游戏内账号、活动实例或代理已保存。`account_binding`、`activity_binding`、`saved_proxy` 保留 `unknown`，`ledger_recorded=false`；助战和无助战均不能凭这个观察登记能力。`execution.status=success` 仍可能对应 `battle_proof.status=unproven`，调用方必须分开读取。

尚待实现/验收：游戏内账号与 Box UID、现有本地 account 别名的可信绑定；关卡对应活动实例（包括常驻 SideStory）的作用域；当次零理智客户端代理证明；在这些证明齐全后通过现有锁和 CapabilityLedger 接口登记。不能用 Box UID 或本地 account 配置单独推断当前登录账号。普通 Fight 继续使用现有零理智 preflight。P5-01/02/03/05 保留未勾选，整个阶段不标 done；真实战斗仍需另行指定关卡并授权。

## 目标

从：

```text
Copilot 看起来跑完了
```

升级成：

```text
ZOOTd 可以证明这个账号已经三星通过该关
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
account X
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
