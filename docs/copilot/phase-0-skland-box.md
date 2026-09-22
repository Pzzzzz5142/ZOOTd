# Phase 0 — Skland Box Spike

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：done（实现、离线测试、真实同步与人工练度核对均通过，2026-09-23）。依赖：无。

## 进度

- [x] P0-01 — done：定义 OperatorBox 和 BoxProvider，未知字段用 null。
- [x] P0-02 — done：实现认证、refresh、签名与官服角色选择。
- [x] P0-03 — done：实现 player/info 规范化和错误分类。
- [x] P0-04 — done：实现本机交互式手机号/验证码 box-login 与秘密存储。
- [x] P0-05 — done：实现独立只读 box-sync。
- [x] P0-06 — done：添加合成脱敏 fixture 与离线测试。
- [x] P0-07 — done：真实账号同步，并人工核对六星、精英化、等级、专精和模组。

## 完成记录

2026-09-23：P0-01 至 P0-06 实现完成；20 项新增离线测试、106 项完整本地测试、shell 语法与文档相对链接检查通过。实现为 [内部模型](../../maa_planner/operator_box.py)、[Skland adapter](../../maa_planner/skland.py)、[交互 CLI](../../maa_planner/box_cli.py)；验证为 [离线测试](../../tests/test_skland.py) 与 [合成 fixture](../../tests/fixtures/skland/README.md)。登录操作见[运维手册](../operations.md#森空岛登录与-box-同步experimental)。

P0-07：用户已在本机终端完成短信登录；真实只读同步成功。首次同步发现共享技能 ID 带方括号，已修正规则并增加合成 fixture 回归验证。用户已人工确认六星干员的精英化、等级、技能专精和已解锁模组与实际一致。P0-07 验收通过（2026-09-23），不在仓库保存该账号的具体练度。不会读取凭据文件或完整原始响应到代理上下文；不把合成 fixture 当成真实验收。后续 Phase 1 状态见[总进度](README.md)。

## 协议调研与模型调整

协议参考是开源客户端实现，不是官方稳定 API 保证，真实可用性需 P0-07 验收：

- [认证与 refresh 实现](https://github.com/FrostN0v0/nonebot-plugin-skland/blob/886d19decf32a7bfb2980adba9254b41d1aadb2f/nonebot_plugin_skland/api/login.py)：鹰角 token → grant code → Skland cred → refresh signing token。
- [签名与 player/info](https://github.com/FrostN0v0/nonebot-plugin-skland/blob/886d19decf32a7bfb2980adba9254b41d1aadb2f/nonebot_plugin_skland/api/request.py)：path + query + timestamp + 有序 header JSON，HMAC-SHA256 后 MD5；只请求绑定与玩家数据。
- [干员字段](https://github.com/FrostN0v0/nonebot-plugin-skland/blob/886d19decf32a7bfb2980adba9254b41d1aadb2f/nonebot_plugin_skland/schemas/arknights/models/chars.py) 和 [模组字段](https://github.com/FrostN0v0/nonebot-plugin-skland/blob/886d19decf32a7bfb2980adba9254b41d1aadb2f/nonebot_plugin_skland/schemas/arknights/models/base.py)。
- [手机号/验证码协议](https://github.com/ProbiusOfficial/Skland_API)：`send_phone_code`（type=2）与 `token_by_phone_code`；用户明确要求后纳入 P0-04，不自动发短信，不保存手机号/验证码。

内部模型 schema=1：潜能由 `potentialRank` 的 0–5 转为显示值 1–6；技能按实际 ID 保存专精，**不把远端列表顺序当成技能编号**。模组以 ID 保存 level 和 unlocked，未读取到 locked 时 unlocked=null。名字、技能编号、X/Y 类型映射留给经验证的静态游戏数据，当前不猜测。字段缺失为 null，已知空数组规范化为 {}；错误类型、重复 ID、账号不一致、缺 chars 均拒绝。快照绑定 `Official:<uid>`；fetched_at 是获取时间，不保证森空岛数据与游戏实时同步。

## 目标

完全不接 PRTS 和 MaaCore。

只验证：

```text
森空岛 credential
        ↓
auth / refresh
        ↓
player binding
        ↓
player/info
        ↓
normalize
        ↓
OperatorBox
```

这是整个项目当前最重要的外部 dependency spike。

Phase 0 不成功，不进入后续阶段。

---

## 输入

- runtime secret 中的森空岛 credential/token；
- 已绑定明日方舟国服官服角色；
- 网络。

---

## 输出

至少留下：

```text
BoxProvider protocol/interface
SklandBoxProvider
OperatorBox model
Skland fixtures
unit tests
```

以及一个独立 CLI，例如：

```bash
./bin/zootd box-sync
```

实际命名可以根据现有 CLI 结构调整。

---

## 具体任务

### A. 定义 provider boundary

概念接口：

```python
class BoxProvider(Protocol):
    def fetch_box(self) -> OperatorBox:
        ...
```

第一版只有：

```text
SklandBoxProvider
```

暂时不要实现其他 provider。

### B. 集中实现森空岛协议

同一个模块负责：

```text
credential/token
↓
refresh
↓
signature
↓
bindings
↓
official Ark UID
↓
player/info
```

森空岛属于非正式 API。

auth、签名、header、endpoint 都不能散落在 planner 其他模块。

### C. Normalize chars

至少获取：

- operator ID；
- elite；
- level；
- potential；
- main skill level；
- skills/mastery；
- modules；
- module level。

干员中文名等静态 metadata 可以通过项目已有/MAA game data 补全，不必相信远端重复维护的显示字段。

### D. Schema validation

错误必须区分：

```text
authentication error
permission error
protocol/schema error
network error
no bound official account
```

特别注意：

```text
schema error != empty box
```

### E. 安全

禁止：

```text
logger.info(credential)
logger.debug(full_response)
```

fixture 必须脱敏。

---

## 验收标准

用真实账号运行一次：

```bash
./bin/zootd box-sync
```

可以得到规范化 Box。

人工核对至少：

- 一个真实拥有的六星；
- elite；
- level；
- 一个有专精的技能；
- 一个已经开启的模组。

同时：

- credential 错误时明确失败；
- 网络失败不会产生空 Box；
- fixture tests 可以完全离线运行。

---

## 暂不做

Phase 0 明确不做：

- PRTS；
- Copilot；
- matcher；
- MaaCore；
- daily integration；
- 无人值守发送短信或保存手机号/验证码（用户明确要求的本机交互登录纳入本阶段）；
- Web UI；
- 一图流；
- MAA OperBox OCR。

---

## Phase 0 停止点

完成之后可以安全暂停开发。

此时项目已经拥有：

```text
real player state
      ↓
stable internal OperatorBox
```

下一次 session 不需要再考虑森空岛细节。

---