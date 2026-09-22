# Copilot 实验性通关路线图与进度

[文档索引](../README.md) · [项目首页](../../README.md)

## 产品边界

用户单独执行实验命令后，系统读取 Box、查找作业、确定性匹配并执行有限尝试。**不由 daily、planner、timer 或自动恢复触发。** 原路线图的 Phase 6 自动接入 daily 已改为独立命令的运行保障。共同模型、设计原则和风险见[共同设计](design.md)。

## 追踪规则

任务使用稳定 ID 和 Markdown checkbox。状态为 todo / in_progress / blocked / done；每完成一项就勾选并标 done，同时记录实现、验证和日期。部分完成不等于阶段 done，缺少真实验收必须保留未勾选项。每次提交同步此表和对应阶段文档。

- [x] PLAN-01 — done：拆分共同设计和 8 个阶段，建立独立任务及验收清单（2026-09-23）。
- [x] PLAN-02 — done：统一为用户显式触发 experimental 命令，移除 daily 自动接入目标（2026-09-23）。

| Phase | 文档 | 状态 | 依赖 |
|---|---|---|---|
| 0 | [skland-box](phase-0-skland-box.md) | done（7/7，2026-09-23） | 无 |
| 1 | [prts-client](phase-1-prts-client.md) | todo | Phase 0 |
| 2 | [matcher](phase-2-matcher.md) | todo | Phase 1 |
| 3 | [technical-mvp](phase-3-technical-mvp.md) | todo | Phase 2 |
| 4 | [retry](phase-4-retry.md) | todo | Phase 3 |
| 5 | [proof](phase-5-proof.md) | todo | Phase 4 |
| 6 | [experimental-operations](phase-6-experimental-operations.md) | todo | Phase 5 |
| 7 | [reliability](phase-7-reliability.md) | todo | Phase 6 |

## 里程碑与下一步

Technical MVP：Phase 3 完成后评估一次。Experimental Operational MVP：Phase 6 完成；仍不启用日常自动触发。Phase 7 仅作为后续增强。

Phase 0：Skland Box 与交互式登录已完成，真实同步与人工核对通过。下一步为 Phase 1 的 PRTS 只读候选查询，尚未开始。Phase 0–2 不启动游戏；Phase 3 起真实战斗另需用户明确指定关卡和授权。
