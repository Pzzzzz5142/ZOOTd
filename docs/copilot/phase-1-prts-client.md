# Phase 1 — PRTS Candidate Retrieval Spike

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：todo。依赖：Phase 0 验收完成。

## 进度

- [ ] P1-01 — todo：确认当前 query/get 协议并集中封装 adapter。
- [ ] P1-02 — todo：规范化轻量候选与错误分类。
- [ ] P1-03 — todo：核对查询结果和完整作业的 canonical stage identity。
- [ ] P1-04 — todo：离线 fixture 测试与真实只读查询验收。

## 完成记录

尚未完成。每项 done 需记录实现文件、测试/验收证据和日期；整个阶段只有全部验收通过才标 done。

## 目标

输入一个明确关卡：

```text
stage
 ↓
PRTS query
 ↓
CopilotCandidate[]
```

暂时完全不考虑玩家 Box。

---

## 输入

例如：

```text
stage code
stage id
```

具体 canonical identity 应复用 ZOOTd/MAA 已有的关卡身份。

---

## 输出

一个集中式 adapter：

```text
PrtsCopilotClient
```

至少支持：

```text
query(stage)
get(copilot_id)
```

---

## 具体任务

### A. 查询候选

使用 PRTS Plus API：

```text
/copilot/query
```

禁止爬 HTML 页面。

### B. 只取轻量信息

搜索阶段读取：

- id；
- stage；
- opers；
- groups；
- requirements；
- title；
- rating/hot metadata。

不要立即下载每一个候选的 `actions`。

### C. Full Copilot

只有选中 candidate 后才：

```text
/copilot/get/{id}
```

### D. 身份校验

远端返回的作业必须再次确认：

```text
candidate.stage == requested.stage
```

不能只相信搜索参数。

### E. 错误分类

至少：

```text
network
empty result
schema error
stage mismatch
copilot not found
```

---

## 验收标准

给一个已知有作业的关卡：

```text
stage
 ↓
打印若干 candidates
```

能看到：

```text
ID
title
opers
groups
requirements
```

然后指定其中一个 ID：

```text
candidate ID
 ↓
完整 Copilot JSON
```

---

## 暂不做

- Box matching；
- ranking；
- MaaCore；
- retry；
- PRTS 全站缓存。

---