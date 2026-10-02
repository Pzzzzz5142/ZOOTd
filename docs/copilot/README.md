# Copilot 实验性通关路线图与进度

[文档索引](../README.md) · [项目首页](../../README.md)

## 产品边界

用户单独执行实验命令后，系统读取 Box、查找作业、确定性匹配并执行有限尝试。**不由 daily、planner、timer 或自动恢复触发。** 原路线图的 Phase 6 自动接入 daily 已改为独立命令的运行保障。共同模型、设计原则和风险见[共同设计](design.md)。

## 追踪规则

任务使用稳定 ID 和 Markdown checkbox。状态为 todo / in_progress / blocked / done；每完成一项就勾选并标 done，同时记录实现、验证和日期。部分完成不等于阶段 done，缺少真实验收必须保留未勾选项。每次提交同步此表和对应阶段文档。

- [x] PLAN-01 — done：拆分共同设计和 8 个阶段，建立独立任务及验收清单（2026-09-23）。
- [x] PLAN-02 — done：统一为用户显式触发 experimental 命令，移除 daily 自动接入目标（2026-09-23）。
- [x] RAID-01 — done：显式突袭参数、模式身份/作业过滤、MAA 模式确认与预算/账本隔离已实现并通过离线验证（2026-09-30，见 [Phase 3](phase-3-technical-mvp.md)）。
- [x] RAID-02 — done：用户授权自行选关后，1-12 突袭自有阵容单场成功，限 9 理智，模式确认与突袭通关图标均有新鲜回调（2026-09-30，见 [验收记录](../history.md#2026-09-30copilot-突袭实机验收)）；不覆盖其他入口、助战或代理能力。
- [x] RAID-03 — done：MN-EX-7 未解锁时误打普通暴露的保护缺陷已修复；独立开战前模式检查、原生切换超限后强制确认、禁止自动代打普通及换候选均经离线 worker/整轮回归和真实 Core 资源加载验证（2026-09-30，见 [Phase 3](phase-3-technical-mvp.md)）。
- [x] MED-01 — done：显式 `--use-sanity-potion` 补药授权、默认禁药及源石 Stop overlay 已接入；CLI、整轮授权与执行参数通过离线验证（2026-10-02，见[运维手册](../operations.md#单次-copilot-通关实验phase-3)）。
- [x] EX-ID-01 — done：游戏数据配对的突袭 ID 作业内容可映射到同一关卡，保留 CLI 难度授权、候选 difficulty 筛选和精确执行地图；离线身份拒绝测试通过（2026-10-02）。
- [x] EX-ZONE-01 — done：旧活动底部暗色英文 EX 分区后缀采用选择栏内原始 OCR 与大小写归一化，保留有限点击和精确关卡详情确认；离线导航图验证及 DV-EX-1 普通模式零理智实机导航通过（2026-10-02）。
- [x] PLOT-PROOF-01 — done：战中剧情跳过完成后的尾部错误要求同链两次模板点击证据，仍严格校验新鲜三星和完整终态；离线缺失、错序、异身份与失败拒绝测试通过（2026-10-02）。
- [x] FORM-AUX-01 — done：编队技能列表原生默认 ID 辅助滑动采用具名任务、动作、设备和编队阶段校验，不替代真实任务 ID 通关证据；离线错误/身份/时序拒绝测试通过（2026-10-02）。
- [ ] RAID-04 — todo：新开战前保护的实机验收，覆盖未解锁入口停止及已解锁突袭成功；须另行明确授权，旧 1-12 成功不代替此次验证。

| Phase | 文档 | 状态 | 依赖 |
|---|---|---|---|
| 0 | [skland-box](phase-0-skland-box.md) | done（7/7，2026-09-23） | 无 |
| 1 | [prts-client](phase-1-prts-client.md) | done（4/4，2026-09-23） | Phase 0 |
| 2 | [matcher](phase-2-matcher.md) | done（5/5，2026-09-24） | Phase 1 |
| 3 | [technical-mvp](phase-3-technical-mvp.md) | done | Phase 2 |
| 4 | [retry](phase-4-retry.md) | done（5/5，2026-09-28） | Phase 3 |
| 5 | [proof](phase-5-proof.md) | in_progress（实现/离线验证完成，待实机证明验收） | Phase 4 |
| 6 | [experimental-operations](phase-6-experimental-operations.md) | todo | Phase 5 |
| 7 | [reliability](phase-7-reliability.md) | todo | Phase 6 |

## 里程碑与下一步

Technical MVP：Phase 3 完成后评估一次。Experimental Operational MVP：Phase 6 完成；仍不启用日常自动触发。Phase 7 仅作为后续增强。

Phase 0：Skland Box 与交互式登录已完成，真实同步与人工核对通过。Phase 1：PRTS 轻量查询、指定 ID 完整获取、关卡身份复核和真实只读验收已完成。Phase 2：离线 matcher、全局位置分配、四档分类与稳定排序已完成。Phase 3 technical MVP 已完成，NL-8 单次实机执行终态成功；Phase 4 已完成：2026-09-28 NL-8 实机验收中，A 受控编队失败且未开战，自动释放预算、返回首页并换 B 执行成功，整轮限 18 理智；允许助战，本轮实际使用自有阵容。预算已按国服现行全额战败退款规则修正，明确失败会释放理智预留，仍限制执行次数。2026-09-26 按用户要求先开始 Phase 5，已接入新鲜三星观察与拒绝测试；2026-09-28 已补齐账号提示、活动/代理证明与现有账本登记及离线测试，新实机证明验收仍待完成。Phase 0–2 不启动游戏；Phase 3 起真实战斗另需用户明确指定关卡和授权。
