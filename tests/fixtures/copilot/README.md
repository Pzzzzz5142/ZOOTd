# Copilot 剧情协议样本

[`plot-lifecycle.json`](plot-lifecycle.json) 投影自 2026-10-10 普通活动关
MAA Core v6.18.0 的实机回调片段。保留原生 message、first、pre_task、任务、
动作、算法、模板、score 和执行计数；设备与 task ID 替换为合成值，移除
账号、作业、运行目录、时间、图像及干员信息。原运行与结果保持不变。

`during_battle` 是两次独立 BattleHelper 调用：第一次 Skip + Confirm
正常结束，第二次 Skip 匹配后发出无附加原因的空 ProcessTaskError。
`after_battle` 是 `Copilot@WaitUntilEndOfAction` 中的 Skip + Confirm。
[`plot-zero-match.json`](plot-zero-match.json) 投影自 2026-10-11 同版本的
另一普通活动关，使用相同字段白名单与合成身份。其 `during_battle`
是一次完成的 Skip + Confirm，随后一个没有点击回调的新 helper 发出
空错误；`after_battle` 同样处于结算等待链。原生日志显示前次
ProcessTask 已返回，再次外层 Skip 匹配后创建新 ProcessTask，内部首次
匹配失败后直接返回。不能把这个错误归到已完成的 Confirm 上。
测试把这些片段放入合成、重新编号的完整战斗上下文；这是协议兼容回放，
不是原运行重新验收，也不作为新鲜实机三星证明。

源码依据为 [v5.28.0 BattleHelper](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v5.28.0/src/MaaCore/Task/BattleHelper.cpp)、
[v6.18.0 BattleHelper](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/src/MaaCore/Task/BattleHelper.cpp)、
[v5.28.0 ProcessTask](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v5.28.0/src/MaaCore/Task/ProcessTask.cpp)、
[v6.18.0 ProcessTask](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/src/MaaCore/Task/ProcessTask.cpp)
和 [v6.18.0 tasks](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.18.0/resource/tasks/tasks.json)。
两版本均会在检测到 Skip 时创建独立 ProcessTask 并忽略其返回值；Skip
的 next 可以再次匹配 Skip，普通 Confirm 无 next。外层 Matcher 与新
ProcessTask 使用不同截图；内部首次匹配失败可直接发出
`first=[SkipThePreBattlePlot]`、`pre_task=''`、`details={}` 的错误，没有
Start/Completed。无调用 ID 的回调不能区分这样的多次新探测，因此不
按场景数或错误次数限制；仍要求新鲜战中身份、正确 class、原生前驱、
明确空 details、无附加原因及无未完成点击。战后 scope 则有变化：
v5.28.0 使用 `ClickCornerUntilEndOfAction`，v6.18.0 使用
`WaitUntilEndOfAction`。失败时的前驱更新时机也不同：v5.28.0 在下次
识别前推进 pre_task，v6.18.0 只在成功匹配后推进，故已匹配 Skip 后
失败的回调沿用最后点击的前驱。当前 adapter 保留 v6.18.0 的精确校验；
旧版用于确认变化依据，不表示当前三星证明支持所有旧版 scope、前驱
语义或缺少模板字段的旧版回调。

首次零匹配、连续零匹配、确认或失败后新探测、重复新 helper、同一
helper 连续 Skip、多个战后剧情、附加无关字段是
基于这些任务边的合成 forward tests；不冒充多个活动的实机样本。身份、
scope、点击配对、模板、错误与时序反例使用同一 fixture 的最小扰动。
