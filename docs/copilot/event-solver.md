# 活动关求解与本地 Copilot 作业

[文档索引](../README.md) · [Copilot 进度](README.md) · [运维手册](../operations.md#单次-copilot-通关实验phase-3)

本实验以将关卡出怪数据图与账号 Box 转成 MAA 可读的本地作业为目标，并用现有
`--copilot-file` 接口执行。它只用于用户明确要求的活动普通关与突袭，不接入 daily、
planner、timer 或无人值守恢复。教学与特殊导航操作按用户当前授权处理；普通
战斗仍由 Copilot 编队、执行和验收。

## 两条协作路线

| 路线 | 输入与工作 | 输出及证明范围 |
|---|---|---|
| 程序化求解 | 固定 revision 的关卡、敌人数据与 Box；代理研究缺失机制并改进已有求解逻辑、模型或约束，由程序生成战术 | 可复现的数据图、分析、程序生成计划和 MAA JSON；实际通关仍须本轮新鲜证明 |
| PRTS 现成作业适配 | 获取已有 Copilot 作业并保存来源及原件；鼓励直接修改 JSON 适配账号和失败证据 | 有来源及修改依据的本地作业；不证明本项目求解器具备战术生成能力 |

来源边界与迭代要求以[开发指南的战术求解约定](../development.md#战术求解开发约定)为准。
PRTS 关卡、敌人或干员资料不属于现成作业，不能据此手填本项目求解器的私有计划。
当前程序已有数据图、几何位置排名、固定作业评估、编译校验和有界末段防守候选生成，
尚无完整地图战术模拟与生成能力；不能把代理手写计划后调用 `compile` 称为自动求解。

程序另提供 [战斗评估与演习门槛](combat-model.md)，计算受支持技能的逐段伤害、
敌人状态和阻挡情景并保留未建模项。地图几何不足以精确复现攻击索敌、
技能周期、阻挡、敌人脚本及活动环境，因此不能把最高位置评分称为通关解。
skill 用这些分析定位程序缺陷，修正后重新生成，再用有限的真实尝试验证。未知机制必须保留并明确
研究；确认一个未知项不代表程序已经模拟它。

## 数据图与来源

命令从项目根目录执行：

```bash
./bin/zootd battle-solver fetch YW-1 --output var/battle-solver/YW-1/graph.json
# 突袭必须显式选择难度，图与普通关分开保存
./bin/zootd battle-solver fetch YW-EX-1 --raid --output var/battle-solver/YW-EX-1/graph-raid.json
./bin/zootd battle-solver analyze --graph var/battle-solver/YW-1/graph.json --format ascii
./bin/zootd battle-solver analyze --graph var/battle-solver/YW-1/graph.json --candidates --placement MELEE --limit 20
```

`fetch` 读取游戏数据和导航元数据，保存关卡身份、来源 Git revision 与内容
SHA-256。`analyze` 可离线读取已保存图，不启动 MAA、Waydroid 或游戏。来源
失败时明确报错；截图和人工 trace 不冒充完整解包数据。

`--candidates` 按几何覆盖给部署位置排名；`--placement` 可选 `MELEE`、
`RANGED` 或 `ALL`。默认攻击范围只是本格与前方一格，不能代替具体干员或
技能范围。`fetch` 同时缓存该 revision 的公开干员和模组静态表；加 `--combat` 另缓存技能、范围和战斗效果，供离线评估及生成使用。后续编译
或校验可加 `--offline` 要求已有完整缓存。

schema 1 的关键内容：

| 字段 | 含义 |
|---|---|
| `stage` | code、stage ID、level ID、battle ID 与明确的 NORMAL / FOUR_STAR 难度身份 |
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
按 `copilot_static` 中已核对的类型映射 X/Y/A/D/B → 1/2/3/4/5；不能从 Box 字典、模组数组或游戏展示顺序 `charEquipOrder` 推断。未知类型不映射，生成器不能要求它或默默沿用默认模组。

### 有界的普通关候选生成

`generate` 从 Box 中选择已有战斗模型的技能，以末段路线覆盖搜索阻挡、输出、治疗、回费、位置、朝向及部署顺序；不阻挡地面但可造成伤害的近战干员进入输出池而非阻挡池；不接受手填战术选择。评分同时参考实际敌人的有限生命击杀情景及保持相同防御/机制、使用人工高生命目标的持续接触情景。后者提供打数、伤害实例数、总伤、平均 DPS 和技能多次回转，不能解释为移动敌人的实际承伤。

先保存固定 graph 与 Box。研究文件 `schema=1`，`graph_sha256` 使用 `canonical_json(graph)` 的 SHA-256，`mechanics` 是每个原始 unknown 字符串到事实与限制说明的映射，`sources` 为非空来源列表。研究确认不改变图事实，也不授予未知机制正面收益。它不包含阵容、坐标、朝向、动作或时间选择。

```bash
./bin/zootd battle-solver generate --graph var/battle-solver/YW-EX-2/graph.json --box var/battle-solver/box-snapshot.json --research var/battle-solver/YW-EX-2/research.json --offline --output var/battle-solver/YW-EX-2/generated-r1
```

输出目录必须尚不存在，内含 `plan.json`、`copilot.json`、`combat.json` 与 `generation.json`，记录代码 HEAD/dirty、入口参数、来源和输入/输出哈希。开发时的 dirty 输出仅供诊断；完成提交、PR、合并后重新生成供实机执行的候选。仍需单独 `validate` 并走三次演习门槛。

`generate` 默认用 `greedy`，可加 `--strategy backtracking` 回退尝试其他部署/治疗网络选择。
`--max-candidates`（默认 8，1–64）限制每策略构造尝试数，失败构造也计入；
`--branch-width`（默认 3，2–8）限制每步排名候选数，不提供墙钟超时保证。
策略接口与敌人机制扩展设计见[求解器架构](solver-architecture.md)。

用同一输入并行比较策略：

```bash
./bin/zootd battle-solver strategies
./bin/zootd battle-solver compare --graph var/battle-solver/YW-EX-2/graph.json --box var/battle-solver/box-snapshot.json --research var/battle-solver/YW-EX-2/research.json --strategies greedy backtracking --workers 2 --max-candidates 8 --offline --output var/battle-solver/YW-EX-2/comparison-r1
```

`compare` 一次准备共同模型，策略在独立进程搜索；`--workers 1` 串行。
新目录内每个成功策略各有四份标准产物，根目录 `comparison.json` 保存共同指标、
排名、耗时、unknown 和失败原因。相同作业标记重复但保留各自产物。
单策略失败不影响其他候选；全部失败仍保存报告并返回非零。共同输入不合法在搜索前拒绝。
排名只比较末段接触伤害启发分及费用，不代表通关概率，不自动执行候选。
并行策略不会增加敌人模型覆盖，也不能通过换策略绕过未知机制拒绝。

当前生成器只支持普通关及已建模敌人。按准确难度启用 hidden-group，并拆开传送可见路段；未知随机分组、条件分支、普通难度生效的未知 rune、额外禁部署约束或无法满足的覆盖会停止。阻挡不能代替灯的蓝门前常态火力，技能扩范围不能代替常态治疗。永久技能只开一次，其他支持技能交由原有使用方式管理。

有自回的阻挡干员仍需核对其实际拦截路线上的已建模承伤：若满基质净手盆的物理承伤情景超过技能自回，医疗网络必须在常态范围覆盖该阻挡点，并记录两项数值。其它路线的净手盆不转嫁到此干员；未发现这个缺口也不等于已证明存活，控制、其它攻击与治疗分配仍可能改变结果。

两种策略均为有限的末段防守候选搜索，尚未求解力场闭合、道具改路、控制链、队伍共享输出与完整存活。费用影响选取和顺序，实际 Deploy 等待 MAA 的费用检查；尚未提供首波到场/费用的可行性证明。香炉持续伤害、花瓶眩晕和基质回技等 unknown 仍可导致候选失败；缺少必要能力时继续改进程序，不能手改输出弥补。

已审查的 EX 仪仗盾变体占用两格阻挡。若交叉路线的单个敌人即可占满某个阻挡位的技能期阻挡数，
生成与共同验收会要求该阻挡位所守末段有独立输出干员的常态范围覆盖，避免把同一阻挡数同时算给两个方向。
搜索优先补齐这些覆盖，再比较伤害；这仍不证明击杀速度、首开前阻挡或多敌排队可行。
回费位在战术评分相同时偏向朝地图内部部署，减少边缘朝外拖动；该几何偏好不是屏幕手势成功证明。

对上述暴露路线上的塑能基质，生成器还核对常态伤害能否覆盖有限的通过时间：使用来源移速、关卡移动倍率、
常态攻击间隔及每次攻击的已建模伤害，各段连续射程接触分别预留一个完整攻击间隔，不将断开的窗口拼接。
限时部署增伤、主动技能、力场吸收、随机天赋和减速不填补此基线缺口；医疗网络也优先保留能容纳这些输出位的范围。
这是固定路线、持续选中该目标的接触情景，不模拟多目标竞争、精确格内运动或全队输出分配，仍须三次演习。
开局回费干员可以在前线部署后撤退，为最后的输出或医疗腾位；生成与编译分别检查退休声明和动作期间的实际部署数量，
不能把额外编队人数当成额外同时部署许可。

以下是程序输出计划的格式说明，不是手写或直接修订求解器战术的操作流程。
已有私有计划仍可用于离线诊断，但历史人工计划不因此成为程序生成结果。
从 PRTS 获取的现成作业可按开发指南直接适配，经本地来源接口执行；下述
`compile` / `validate` 命令面向配有 graph 和 plan 的编译路线。

计划的 `schema_version` 为 1、`stage_id` 绑定图中的关卡；用 `placements`
指定干员、技能、部署位置与朝向，可携带练度要求、技能使用方式、击杀或
计时条件。`kills` 为击杀下限，`elapsed_time`、`pre_delay`、`post_delay`
单位均为毫秒。部署简写兼容旧 `time_elapsed` 别名，编译后统一输出原生
`elapsed_time`；两字段同时存在会拒绝，完整 `actions` 不接受错拼别名。
也可提供完整的 `opers` 与 `actions`；简写计划的额外
`actions` 追加到部署之后。当前编译器不支持替换 `groups` 或 `MoveCamera`，
`skill_usage` 只接受 0、1、2。

突袭通过游戏目录绑定配对的 `#f#` 身份。计划还必须显式声明
`"difficulty": "FOUR_STAR"`；编译输出只接受准确对应的 `difficulty=2`，
普通计划默认 `NORMAL` 并输出 `difficulty=1`，不接受双难度声明代替绑定。
原始 runes 与 hiddenGroup 出怪条件完整保留，后者列为未模拟机制；出怪总数
可能包括尚未启用的条件分支，不能作为某一模式的精确击杀总数。部署合法性
只检查基础地图；禁部署 rune 等额外条件仍需研究与确认，不自动改写基础格子。
当前 `evaluate` 拒绝突袭，避免把未应用突袭加成的敌人属性作为该模式的估计。

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
它指导代理读取数据图和必要 Box 练度，用新鲜失败证据改进求解程序并重新生成，
或直接适配有来源的 PRTS 现成作业，再通过同一本地作业执行链验收。使用时以当前对话授权为准；已有的
关卡与尝试授权无需重复询问，新增难度或药品使用不由 skill 自动授权。

实际执行要求受管工作树干净；代码、配置和文档先按开发指南提交、发布与
合并。普通本地作业调用只执行一个候选，先连续三次演习三星，再执行
至多一场真实战斗；任意演习失败立即停止：

```bash
./bin/zootd copilot-run YW-1 --profile no-support --max-candidates 1 --max-battles 1 --copilot-file var/battle-solver/YW-1/copilot.json
# 只验证演习，不开始真实作战
./bin/zootd copilot-run YW-1 --profile no-support --practice-only --copilot-file var/battle-solver/YW-1/copilot.json
```

使用用户授权的助战策略；默认禁药和源石。程序默认按本关完整费用预留理智，
多次独立调用也需记录累计尝试和预算，不能通过拆分调用越过当前授权范围。
作业运行期间不修改原文件；本轮结束后，程序路线通过代码改进重新生成，
PRTS 作业路线可直接修改并另存候选，再校验并尝试。

演习门槛固定关闭信赖补位，保证三次演习与真实作战使用同一固定阵容。
当前门槛仅支持普通难度、自有阵容；演习始终禁药和源石。

明确授权的突袭本地作业使用 `copilot-run YW-EX-1 --raid --no-add-trust
--profile no-support --copilot-file <突袭作业>`，复用既有开战前模式检查、
预算和突袭结果契约。突袭没有三次演习门槛，也不生成普通代理能力证明。
EX 普通关仍须通过普通本地作业的三次演习门槛。允许用药时另加
`--use-sanity-potion`；源石始终禁止。

导航可先用 `./bin/zootd navigate YW-1 --plan` 只读规划，再按授权做零战斗
导航。首次教学与特殊页面可以在用户授权下处理，但不得手动普通战斗绕过
Copilot 证明。安装的 Tile 缺少新关卡时使用正式整代更新流程，不直接改写
live runtime 或 generation receipt。

当前 `act52side` 的普通区与 EX 区共用已观察的终端活动卡片；艺术字标题识别失败时，单行识别固定
`SIDESTORY` 标记进入；适配同时绑定活动、分区及完整难度身份，进入后仍须
定位并确认目标关卡。EX 支持普通与突袭各自准确身份，突袭仍须独立确认模式。
EX 分区入口仅在已观察区域内识别完整标题，统一标题首尾被 OCR 读成英文或遗漏的装饰引号。
标题识别后点击同页已观察的圆形按钮中心的小区域，避免随机点击落入文字或装饰区域；进入活动后先等待布局稳定，仍限一次点击并确认准确关卡详情。
它不授予战斗权限；归档、复刻及其它活动不继承
该布局，后续新增版式需另有观察依据。

成功必须来自当前 run 的准确作业身份、完成编队、战斗、新鲜三星和完整终态。
将导航错误、编队失败、动作失败、漏怪、战败、证据缺失分别定位；程序路线
修正对应缺陷并重新生成，有来源的 PRTS 作业才可直接修订战术 JSON。
不改写旧审计、不伪造退款、不以旧截图或退出码补足证明。教学
通过、零战斗到达、离线编译和三星通关应分别报告。

## 初始研究目标与验收快照

以下仅记录 2026-10-09 初始研究时的目标与验收状态，不作为当前开发流程或最新通关清单。
当时研究目标为「昨日海」`act52side` 的 `YW-1` 至 `YW-8` 普通关；
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
