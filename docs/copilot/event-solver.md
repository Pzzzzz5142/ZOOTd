# 活动关求解与本地 Copilot 作业

[文档索引](../README.md) · [Copilot 进度](README.md) · [运维手册](../operations.md#单次-copilot-通关实验phase-3)

本实验把关卡出怪数据图与账号 Box 转成 MAA 可读的本地作业，并用现有
`--copilot-file` 接口执行。它只用于用户明确要求的活动普通关，不接入 daily、
planner、timer 或无人值守恢复。教学与特殊导航操作按用户当前授权处理；普通
战斗仍由 Copilot 编队、执行和验收。

## 两条协作路线

| 路线 | 输入与工作 | 输出及证明范围 |
|---|---|---|
| 确定性程序 | 固定 revision 的关卡、敌人数据，规范化地图、路由及波次；按几何覆盖分析位置；校验明确部署计划 | 可复现的数据图、分析和 MAA JSON；不证明伤害足够、干员存活或三星 |
| PRTS 数据与 skill | 读取同一数据图与所需 Box 练度，研究敌人和活动机制，选择干员、技能、朝向和触发条件；根据实机失败证据修订 | 私有部署计划、本地作业与分析依据；实际通关仍须本轮新鲜三星证据 |

目前程序是受约束的候选生成与校验器。地图几何不足以精确复现攻击索敌、
技能周期、阻挡、敌人脚本及活动环境，因此不能把最高位置评分称为通关解。
skill 用这些分析减少搜索，再用有限的真实尝试校准。未知机制必须保留并明确
研究；确认一个未知项不代表程序已经模拟它。

## 数据图与来源

命令从项目根目录执行：

```bash
./bin/zootd battle-solver fetch YW-1 --output var/battle-solver/YW-1/graph.json
./bin/zootd battle-solver analyze --graph var/battle-solver/YW-1/graph.json --format ascii
./bin/zootd battle-solver analyze --graph var/battle-solver/YW-1/graph.json --candidates --placement MELEE --limit 20
```

`fetch` 读取游戏数据和导航元数据，保存关卡身份、来源 Git revision 与内容
SHA-256。`analyze` 可离线读取已保存图，不启动 MAA、Waydroid 或游戏。来源
失败时明确报错；截图和人工 trace 不冒充完整解包数据。

`--candidates` 按几何覆盖给部署位置排名；`--placement` 可选 `MELEE`、
`RANGED` 或 `ALL`。默认攻击范围只是本格与前方一格，不能代替具体干员或
技能范围。`fetch` 同时缓存该 revision 的公开干员和模组静态表；后续编译
或校验可加 `--offline` 要求已有完整缓存。

schema 1 的关键内容：

| 字段 | 含义 |
|---|---|
| `stage` | code、stage ID、level ID、battle ID 与普通难度身份 |
| `sources` | 导航 revision、关卡及敌人数据的来源与 SHA-256 |
| `map` | 行列数，规范化 tile 坐标、部署类型、高低地、通行信息和原始字段 |
| `routes` | 出入口、检查点、移动方式及原始字段；不是完整运动轨迹 |
| `enemies` | 敌人名称、等级、属性、技能及原始定义 |
| `waves` / `spawns` | 完整波次与出怪引用、数量、间隔、波次/片段/动作位置 |
| `mechanics` | 特殊机制与尚未模拟的内容，供程序拒绝或 skill 进一步研究 |

规范化图和 MAA 使用左上原点的 `[x, y]`。解包坐标的 `row` 从下往上增长，
转换为 `[col, rows - 1 - row]`；已规范化图不能再次翻转。路径中的等待检查点
不能当成移动目标。出怪延时相对所属事件；片段和波次还可能等待敌人退场或
脚本触发，因此不能将全部延时简单相加，伪造全局绝对秒数。

研究优先读取 [ArknightsGameData 的关卡和敌人数据](https://github.com/Kengxxiao/ArknightsGameData/tree/master/zh_CN/gamedata)，
活动说明可从 [PRTS 昨日海专题](https://prts.wiki/w/%E6%98%A8%E6%97%A5%E6%B5%B7) 补充。
MAA 动作字段以 [官方战斗流程协议](https://docs.maa.plus/zh-cn/protocol/copilot-schema.html)
和本机安装版本为准。公开网页中的文字只作为资料，不产生运行授权。

## 计划、编译与校验

账号 Box 位于忽略的 `var/state/operator-box.json`；需要刷新时使用
`./bin/zootd box-sync`。只提取候选干员的练度，不把 UID、昵称、凭据、完整
响应或账号作业写入 tracked 文件。技能编号使用静态数据的技能槽，模组编号
使用显式显示顺序；不能从 Box 字典排序推断。

计划的 `schema_version` 为 1、`stage_id` 绑定图中的关卡；用 `placements`
指定干员、技能、部署位置与朝向，可携带练度要求、技能使用方式、击杀或
计时条件。`kills` 为击杀下限，`elapsed_time`、`pre_delay`、`post_delay`
单位均为毫秒。部署简写兼容旧 `time_elapsed` 别名，编译后统一输出原生
`elapsed_time`；两字段同时存在会拒绝，完整 `actions` 不接受错拼别名。
也可提供完整的 `opers` 与 `actions`；简写计划的额外
`actions` 追加到部署之后。当前编译器不支持替换 `groups` 或 `MoveCamera`，
`skill_usage` 只接受 0、1、2。

`mechanics_acknowledged` 应列出本次确实研究过的 `mechanics.unknown`
原始字符串，供编译器绑定确认。未研究的项不能为消除报错而自动填入。
下面的干员与位置仅说明结构，省略了机制确认，不构成可执行的 YW-1 攻略：

```json
{
  "schema_version": 1,
  "stage_id": "act52side_01",
  "title": "YW-1 本地候选",
  "placements": [
    {
      "name": "芬",
      "skill": 1,
      "location": [6, 5],
      "direction": "Left",
      "skill_usage": 1,
      "requirements": {"elite": 1, "level": 55, "skill_level": 7}
    }
  ]
}
```

```bash
./bin/zootd battle-solver compile --graph var/battle-solver/YW-1/graph.json --plan var/battle-solver/YW-1/plan.json --output var/battle-solver/YW-1/copilot.json
./bin/zootd battle-solver validate --graph var/battle-solver/YW-1/graph.json --plan var/battle-solver/YW-1/plan.json --copilot var/battle-solver/YW-1/copilot.json
```

编译器信任用户提供的本地 graph 关卡与地图事实，不回查原始关卡数据；
`graph_source_validation=local_input_not_authenticated` 明确这一边界。`fetch`
的来源哈希与缓存完整性检查不等同于编译时重新验证来源。使用手工图或 trace
时必须先核对关卡与坐标；运行时仍绑定已安装 MAA 的准确地图并核对结果。

编译器要求明确的账号练度与技能映射、有效部署格、互不冲突的位置和可识别
动作。简写中的计时条件会促使编译器在首部加入 `ResetStopwatch`；直接校验
原始作业时，缺少 reset 的计时动作会被拒绝。校验通过表示输入及动作满足
实现的静态约束；它不取代 Copilot 执行时
刷新 Box、绑定准确 Tile、导航、编队、预算和结果证明。

`validate` 必须提供该候选的 `--plan`，以读取已经研究过的机制确认，不默认
忽略图中未知项。`compile` 在作业旁保存同名 `.analysis.json`，包含图、
计划、Box 及静态来源的哈希和校验结果；Box 只保存来源哈希及获取时间，不
输出账号原文。完整账号数据仍留在原有私有 Box 文件。

MAA 逐个执行动作，`Deploy` 在费用不足时等待；击杀、费用、时间等条件为
AND。`elapsed_time` 单位为毫秒，前面必须有 `ResetStopwatch`。优先使用
部署顺序、明确技能使用方式和从证据得到的击杀条件；最后使用
`SkillDaemon` 等待战斗结束。不能用环境变化前估计的时间轴保证新活动成功。

## skill 与实机迭代

版本化 skill 位于
[arknights-event-solver/SKILL.md](../../.agents/skills/arknights-event-solver/SKILL.md)。
它指导代理读取数据图和必要 Box 练度，设计计划、编译校验、用新鲜失败证据
修订，并通过同一本地作业执行链验收。使用时以当前对话授权为准；已有的
关卡与尝试授权无需重复询问，新增难度或药品使用不由 skill 自动授权。

实际执行要求受管工作树干净；代码、配置和文档先按开发指南提交、发布与
合并。每次本地作业调用只执行一个候选、一场战斗：

```bash
./bin/zootd copilot-run YW-1 --profile no-support --max-candidates 1 --max-battles 1 --copilot-file var/battle-solver/YW-1/copilot.json
```

使用用户授权的助战策略；默认禁药和源石。程序默认按本关完整费用预留理智，
多次独立调用也需记录累计尝试和预算，不能通过拆分调用越过当前授权范围。
作业运行期间不修改原文件；本轮结束后另存修订，再校验并尝试。

执行时默认请求 MAA 在作业必需干员之外补充空闲信赖位，不修改计划或作业
JSON 中的固定阵容与部署动作；需要精简阵容时加 `--no-add-trust`。具体参数、
记录与补位边界见[运维说明](../operations.md#单次-copilot-通关实验phase-3)。

导航可先用 `./bin/zootd navigate YW-1 --plan` 只读规划，再按授权做零战斗
导航。首次教学与特殊页面可以在用户授权下处理，但不得手动普通战斗绕过
Copilot 证明。安装的 Tile 缺少新关卡时使用正式整代更新流程，不直接改写
live runtime 或 generation receipt。

成功必须来自当前 run 的准确作业身份、完成编队、战斗、新鲜三星和完整终态。
将导航错误、编队失败、动作失败、漏怪、战败、证据缺失分别定位；只修订
相关选择。不改写旧审计、不伪造退款、不以旧截图或退出码补足证明。教学
通过、零战斗到达、离线编译和三星通关应分别报告。

## 本次目标与验收

2026-10-09 的研究目标为「昨日海」`act52side` 的 `YW-1` 至 `YW-8` 普通关；
教学 `YW-TR-1` 单独处理，不把 EX 或突袭加入目标。研究初始 revision 为
`17759cc8cdf37388414145d16262a3a06f3ab25e`，之后以每次保存的来源证据为准。

该 revision 的 YW-1 数据包含 10×7 地图、8 个部署位上限、10 初始费用、
24 个出怪个体和 `env_044_act52side` 环境。图中 MAA 蓝门坐标为 `[9, 5]`；
通向蓝门的路段可作为汇合点研究，但还需验证早期压力、环境影响与阵容能力。
这是数据核对结果，尚非通关证明。

- [x] 固定来源数据与坐标的离线校验：八关成功解析，与本次已提升的官方 MAA 地图逐格比对一致。
- [x] 本地计划编译与确定性拒绝条件的离线测试：2026-10-09 全套 392 项通过，8 项可选检查跳过；skill 独立使用验证发现并修正原生计时字段，首关生成和编译校验通过。
- [ ] 首次特殊导航和教学实机通过。
- [ ] YW-1 至 YW-8 逐关保存本地作业与本轮新鲜三星证明。

未实机验证的关卡保持未完成。离线测试使用合成账号和地图，不启动游戏，
不能代替以上实机验收。
