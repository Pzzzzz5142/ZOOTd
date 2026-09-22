# Phase 2 — Deterministic Matcher

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：todo。依赖：Phase 1 验收完成。

## 进度

- [ ] P2-01 — todo：固定干员、精英化、等级、技能与模组匹配。
- [ ] P2-02 — todo：group 全局位置分配，避免同一干员重复占位。
- [ ] P2-03 — todo：exact/support_one/unknown/incompatible 分类。
- [ ] P2-04 — todo：热度排序和稳定 ID tie breaker。
- [ ] P2-05 — todo：完整离线测试矩阵。

## 完成记录

尚未完成。每项 done 需记录实现文件、测试/验收证据和日期；整个阶段只有全部验收通过才标 done。

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