# Phase 5 — Strong Proof + Capability Ledger

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：todo。依赖：Phase 4 验收完成。

## 进度

- [ ] P5-01 — todo：绑定账号、活动实例、当前 run、关卡和新鲜战斗证据。
- [ ] P5-02 — todo：区别三星首通与已保存代理，助战结果不得冒充代理能力。
- [ ] P5-03 — todo：通过现有受控接口登记能力，不建立第二套账本。
- [ ] P5-04 — todo：旧日志、错误身份、缺终态拒绝测试。
- [ ] P5-05 — todo：后续正常代理仍通过零理智 preflight。

## 完成记录

尚未完成。每项 done 需记录实现文件、测试/验收证据和日期；整个阶段只有全部验收通过才标 done。

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
