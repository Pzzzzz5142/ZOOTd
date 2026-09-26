# Copilot 共同设计约定

[总进度](README.md) · [文档索引](../README.md)

本目录描述待实现的 experimental 功能，Phase 0 的 `box-login` / `box-sync` 与 Phase 1 的 `copilot-query` / `copilot-get` 已实现；Phase 2 离线 matcher API 已实现，Phase 3 显式 `copilot-run` 已完成 NL-8 实机验收，Phase 4 有限候选重试已实现并通过离线验证，待授权实机重试验收；Phase 5 已有单次三星观察层，仍未登记能力。详情见阶段记录。外部端点和概念模型需在各阶段以当前协议验证。

## 触发边界

Phase 1 的独立只读查询/获取由用户显式调用。用户显式运行 `./bin/zootd copilot-run <stage>` 才能启动搜索、匹配和执行链路；不接入 daily、planner、timer 或自动恢复。缺少代理能力不能自动触发实验。有限重试仅在这次命令的授权内。协议或证据失败时保存审计并退出，不自动启动完整 daily 恢复。

LLM 不参与正常选择和成功判定。Phase 0 先验证真实玩家数据，Phase 3 完成后暂停评估，Phase 6 是独立命令的运行保障，Phase 7 后续可选。

## 设计原则

### 1. 先跑通纵向链路

不要一次实现完整自动通关系统。

每个阶段都必须可以：

- 独立开发；
- 独立测试；
- 独立验收；
- 在 Codex quota 用尽时安全停下；
- 下一次 session 不需要重新推翻上一个阶段。

### 2. 外部数据必须隔离

下游代码不应该知道森空岛、PRTS 或一图流的原始字段。

统一边界：

```text
BoxProvider → OperatorBox
CopilotProvider → CopilotCandidate
Executor → ExecutionResult
```

未来森空岛接口变化，只修改 `SklandBoxProvider`。

### 3. 正常路径保持确定性

以下判断不能交给 LLM：

- 有没有某个干员；
- 精英化是否满足；
- 等级是否满足；
- 技能等级/专精是否满足；
- 模组是否满足；
- group 是否至少存在一个可用成员；
- 缺多少个必需干员。

### 4. 静态匹配不是成功证明

即使 Box 完全满足作业要求：

```text
compatible != guaranteed success
```

潜能、费用、帧率、轴、作业本身质量等仍可能导致失败。

最终只相信真实战斗证据。

### 5. Unknown 不等于满足

如果作业要求某个字段，而当前 Box 数据源没有提供：

```text
unknown != satisfied
```

应明确标记为 `unknown`，不能静默升级成 compatible。

### 6. Fail closed

以下情况不得自动猜测：

- Box 获取失败；
- 森空岛协议异常；
- PRTS schema 异常；
- 作业关卡身份不一致；
- requirement 无法解释；
- MaaCore 没有明确终态；
- 战斗没有三星强证据。

### 7. Credential 不进入仓库

森空岛 credential/token：

- 不提交 Git；
- 不进入 fixture；
- 不写普通日志；
- 不保存完整原始 player response；
- runtime 只保留真正需要的规范化 Box。

---

# 核心内部模型

## OperatorBox

以下为原始概念示意；当前 schema=1 的字段与技能 ID 约定见 [Phase 0 模型调整](phase-0-skland-box.md#协议调研与模型调整)和代码，不把示意中的技能编号当成已实现映射。

所有 Box provider 最终输出同一个内部结构。

概念模型：

```json
{
  "source": "skland",
  "fetched_at": "2026-09-23T00:00:00+08:00",
  "operators": {
    "char_xxx": {
      "id": "char_xxx",
      "name": "仇白",
      "elite": 2,
      "level": 70,
      "potential": 1,
      "main_skill_level": 7,
      "skills": {
        "1": {
          "mastery": 0
        },
        "2": {
          "mastery": 0
        },
        "3": {
          "mastery": 3
        }
      },
      "modules": {
        "uniequip_xxx": {
          "type": "X",
          "level": 3
        }
      }
    }
  }
}
```

约束：

- matcher 只能读取这个模型；
- provider-specific 原始字段不能泄漏到 matcher；
- 未知字段必须保持 unknown；
- 不允许用 `0` 表示“没有读取到数据”。

---

## CopilotCandidate

搜索阶段不需要完整 `actions`。

```json
{
  "id": 12345,
  "stage": "stage-id",
  "title": "...",
  "hot_score": 0.0,
  "operators": [],
  "groups": [],
  "source": "prts-plus"
}
```

只有真正决定执行某个 candidate 后，才获取完整 Copilot。

---

## CompatibilityResult

```json
{
  "copilot_id": 12345,
  "status": "exact",
  "missing_operators": [],
  "unsatisfied_requirements": [],
  "unknown_requirements": [],
  "support_needed": false
}
```

第一版只定义四档：

```text
exact
support_one
unknown
incompatible
```

含义：

### exact

所有已声明 requirement 均确认满足。

### support_one

仅一个位置已确认缺失或练度不足，其他位置确认满足，且可指定一个不重复的助战身份。静态分类不证明实际助战可用。

### unknown

数据或映射不足，无法确认是否能完全满足或通过一次替换满足；不得升级为 exact/support_one。

### incompatible

即使考虑未知数据和一次助战替换，仍无法为所有位置分配不同干员。

---


# 外部风险

## 1. Skland 属于非正式 API

这是当前最大的外部协议风险。

可能变化：

- auth；
- credential；
- refresh；
- signature；
- header；
- endpoint；
- response schema。

解决方式不是避免使用，而是：

```text
把风险全部限制在 SklandBoxProvider
```

下游 matcher 不受影响。

---

## 2. PRTS requirements 不保证完整

作业作者可能：

```text
没有写 requirement
```

也可能要求写得偏低。

因此：

```text
requirements satisfied
```

只能说明：

> 没有违反作者声明的约束。

不能说明：

> 一定能过。

真实执行仍然是最终验证。

---

## 3. Potential / DP / timing

即使同样：

```text
E2 60 S3M3
```

不同潜能可能改变：

- 部署费用；
- redeploy；
- attack；
- timing。

第一版不试图静态建模所有这些影响。

通过：

```text
execute
→ observe
→ retry
```

解决。

---

## 4. Groups schema

Copilot 的：

```text
groups
```

比固定 opers 更复杂。

parser 与 matcher 必须有独立 fixture。

不要仅依赖 PRTS server-side operator index。

---

## 5. 外部 schema 演进

任何未知 requirement：

```text
unknown
```

而不是：

```text
satisfied
```

---
