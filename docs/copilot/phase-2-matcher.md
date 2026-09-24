# Phase 2 — Deterministic Matcher

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：done（2026-09-24）。依赖：Phase 1 验收完成。

## 进度

- [x] P2-01 — done：固定干员、精英化、等级、技能与模组匹配。
- [x] P2-02 — done：group 全局位置分配，避免同一干员重复占位。
- [x] P2-03 — done：exact/support_one/unknown/incompatible 分类。
- [x] P2-04 — done：热度排序和稳定 ID tie breaker。
- [x] P2-05 — done：完整离线测试矩阵。

## 完成记录

2026-09-24 完成 P2-01 至 P2-05：实现位于 `maa_planner/copilot_matcher.py`，离线验收位于 `tests/test_copilot_matcher.py`。21 项测试覆盖下文矩阵，并穷举 432 种三组候选组合（含助战），与独立穷举分配结果比较。完整回归 138 项通过，随后新增助战穷举用例后的 matcher 专项 21 项通过；文档相对链接与 `git diff --check` 通过。未访问账号或启动游戏。

### 实际接口与边界

- `match_candidate(box, candidate, catalog)` 返回 `CompatibilityResult`；`rank_candidates(box, candidates, catalog)` 返回排序后的结果。结果包含每个位置的成员诊断、canonical ID 分配、助战位置/干员和 `support_needed`；不包含 Box 账号身份。固定位置与 group 都必须各占一个不同干员。
- `OperatorCatalog` 接受独立静态 `OperatorIdentity`：名称、canonical ID、从 1 开始的技能/模组编号到 canonical ID 的显式映射。`from_battle_data(data, skills=..., modules=...)` 可读取本机 MAA 名称表；本机表不含技能/模组映射，必须由调用方提供经过核对的映射，不能从 Box 字典顺序推断。未知名称、同名歧义、缺失编号映射均保留 unknown；本阶段不下载或维护另一份游戏数据缓存。
- 精英化、等级、潜能逐项比较下限；技能选择还检查 E0/E1/E2 对应的 1/2/3 技能解锁。技能要求不超过 7 时直接检查主技能等级，超过 7 时按所选 canonical skill 的 `main_skill_level + mastery` 判断。未选技能却要求专精时为 unknown。
- 模组按指定编号映射后的 canonical ID 检查解锁与等级，要求模组但省略等级时至少为 1。没有模组数据或指定记录时为 unknown；明确锁定/等级不足为不满足。只写 module_level 却未指定模组为 unknown。
- requirement 使用 [MAA 作业协议](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev/docs/zh-cn/protocol/copilot-schema.md) 的 `elite`、`level`、`skill_level`、`module`、`module_level`、`potential`。省略或合法的 0 表示未声明该项约束；未知字段、错误类型（含 bool）、越界值为 unknown，不解析说明文字。
- 先寻找完全确认满足的全局二分图匹配；失败后仅允许替换一个已确认缺失或练度不足的位置，且助战身份不能与其他位置重复。这样得到的 support_one 仅是静态尝试资格，不证明真实助战可用。未知条件不能通过“借助战”静默消除；若未知数据可能补齐分配则为 unknown，即使允许一次替换仍无法分配则为 incompatible。
- 排序依次为状态、hot_score、rating_level、rating_ratio、views（均降序），最后 ID 升序；缺失或无效热度按 0，拒绝重复候选 ID。不使用 difficulty 或 LLM 推测成功率。
- 本阶段仅提供离线 Python API；`copilot-run` 仍属于 Phase 3，不接入已有 query/get 命令、daily、定时器或恢复 controller。

离线复验：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_copilot_matcher -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

## 目标

完全离线。

输入：

```text
OperatorBox
+
CopilotCandidate[]
```

输出：

```text
CompatibilityResult[]
+
ranked candidates
```

这是 ZOOTd 自己新增的核心算法。

---

## 固定 opers

对于：

```json
{
  "name": "仇白",
  "skill": 3,
  "requirements": {
    "elite": 2,
    "level": 60,
    "skill_level": 10
  }
}
```

matcher 要判断：

```text
owned?
elite >= required?
level >= required?
skill requirement satisfied?
module requirement satisfied?
```

---

## Skill requirement

如果：

```text
main skill level = 7
mastery = 3
```

则实际技能等级：

```text
10
```

这一映射应集中实现并测试。

---

## Groups

例如：

```text
任意群奶:
  - 夜莺
  - 白面鸮
```

只需要：

```text
exists usable member
```

即满足该 group。

不能要求全部成员存在。

---

## Support

第一版允许：

```text
只缺一个 required slot
→ support_one
```

两个或以上缺失：

```text
incompatible
```

注意：

`support_one` 只是允许尝试，不代表作业一定能通过。

---

## Unknown

例如：

作业要求：

```text
module
```

但 provider 没有 module 数据。

则：

```text
unknown
```

不能：

```text
exact
```

---

## 排序

MVP 只采用：

```text
exact
  >
support_one
  >
unknown
  >
incompatible
```

同档内部：

```text
PRTS hot/rating
+
stable ID tie breaker
```

不要使用 LLM。

---

## 测试矩阵

至少覆盖：

### 1. exact

所有条件满足。

### 2. operator missing

固定干员不存在。

### 3. elite insufficient

```text
E1 < E2
```

### 4. level insufficient

### 5. mastery insufficient

### 6. module insufficient

### 7. group alternative

第一个成员不存在，但第二个存在。

结果应满足。

### 8. support one

仅缺一个固定位置。

### 9. multiple missing

不得进入 support。

### 10. unknown

数据不足不得误判 exact。

---

## 暂不做

不要尝试判断：

> E2 50 比作业要求 E2 60 少十级，但可能其实也能打。

这种推理不属于 MVP matcher。

也不要：

- NLP 解析作业说明；
- LLM ranking；
- ML；
- 历史胜率模型。

---