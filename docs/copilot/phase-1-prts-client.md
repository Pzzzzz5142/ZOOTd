# Phase 1 — PRTS Candidate Retrieval Spike

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：done（2026-09-23）。依赖：Phase 0 验收完成。

## 进度

- [x] P1-01 — done：确认当前 query/get 协议并集中封装 adapter。
- [x] P1-02 — done：规范化轻量候选与错误分类。
- [x] P1-03 — done：核对查询结果和完整作业的 canonical stage identity。
- [x] P1-04 — done：离线 fixture 测试与真实只读查询验收。

## 完成记录

2026-09-23：P1-01–P1-03 实现在 `maa_planner/prts.py`，显式入口为 `maa_planner/prts_cli.py` 和 `bin/zootd`。P1-04 以 `tests/test_prts.py` 的合成 fixture 离线验证，并完成真实公开 API 只读验收：

- `./bin/zootd copilot-query 1-7 --limit 3`：canonical stage 为 `main_01-07`，当时 total=299，返回 ID 102455、101217、97661；输出 title、operators、groups 和每位干员的 requirements。
- `./bin/zootd copilot-get 102455 --stage 1-7`：重新校验 ID/关卡，返回完整 JSON，含 11 条 actions。未启动游戏、未读取 Box 或账号凭据。
- 完整本地 unittest 验证：118 项通过；shell 语法与文档相对链接检查通过。
- 离线测试覆盖 query 不调用 get、别名/歧义、错误关卡、分组及未知要求保留、损坏 JSON、分页、网络错误、空结果、找不到作业和 full 二次校验。

### 当前协议与边界

查询为 `GET https://prts.maa.plus/copilot/query?level_keyword=<canonical-stageId>&page=1&limit=10&order_by=id&desc=true`；获取为 `GET /copilot/get/<id>`。响应 envelope 为 `status_code` / `data`，分页数据含 `page`、`has_next`、`total`、`data`。每条作业的 `content` 是 JSON 字符串；query 只投影轻量模型，get 才要求完整 actions。无自动翻页、重试或全站缓存，用户可指定 page（正整数）与 limit（1–50）。

协议依据：[MAA 服务端参数定义](https://github.com/MaaAssistantArknights/MaaCopilotServer/blob/main/src/MaaCopilotServer.Application/CopilotOperation/Queries/QueryCopilotOperations/QueryCopilotOperationsQuery.cs)、[MAA 作业协议](https://docs.maa.plus/zh-cn/protocol/copilot-schema.html)及上述部署端点实测。公开源码 DTO 与部署响应存在差异，因此 adapter 按部署实测结构严格校验；不直接套用源码 DTO。实测 `stageName`、`stage_name` 和 `level_keyword=1-7` 均未正确过滤，必须先转换为 `main_01-07`。不爬 HTML。

身份复用本机 `var/data/MaaResource/resource/stages.json` 的 `stageId` / `code`（若存在也支持 `levelId`）。重复 code 拒绝猜测，要求显式 stageId；未知关卡拒绝请求。返回的每份作业再次解析 identity，错误或未知 identity 使整页失败；不会凭搜索参数或标题授权。difficulty 保留原值，不把普通与突袭视为执行兼容性证明。

候选模型保留 operators/groups 的 name、role、skill、requirements，以及 difficulty 和热度/评分元数据；未知 requirement 原样保留，缺失 skill 为 null，未做 Box 匹配或要求满足判定。错误 category 包含 `network`、`empty_result`、`schema_error`、`stage_mismatch`、`copilot_not_found`，另有本地 `stage_identity` / `input`。HTTP 限时 20 秒、响应上限 8 MiB，拒绝重定向、重复 JSON key、非有限数和畸形结构。

操作命令见[运维手册](../operations.md#prts-作业查询experimental)。

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