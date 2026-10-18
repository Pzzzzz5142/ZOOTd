# 项目文档

[返回项目首页](../README.md)

| 文档 | 内容 |
|---|---|
| [运维手册](operations.md) | 初始化、手动运行、systemd、更新回滚、Web 监控面板、Bark 结果推送、森空岛双游戏签到、排障与审计状态 |
| [策略与配置](configuration.md) | 库存目标、材料等价换算、基建、周剿灭与代理隔离 |
| [架构与执行契约](architecture.md) | 数据职责、完整链路、拒绝条件与 runtime 一致性 |
| [开发指南](development.md) | 代码目录、战术求解与 PRTS 作业修改边界、测试、Git 约定与运行证据 |
| [工程设计指南](engineering-guidelines.md) | 每次开发必读：模块边界、数据维护、前向兼容、历史依据与工程投入 |
| [LLM 诊断与整轮恢复](recovery.md) | adapter 职责、恢复验证、连通性探针与 SDK 更新 |
| [恢复 scope 与 FAQ](llm-recovery-scope.md) | 恢复代理实际读取的版本化操作契约 |
| [历史事故与验收记录](history.md) | 旧实现的事故、修复背景与当时验收结果 |
| [Copilot 实验路线图与进度](copilot/README.md) | 显式实验命令的分阶段任务、依赖和验收进度 |
| [活动关求解与本地 Copilot 作业](copilot/event-solver.md) | 出怪数据图、程序化求解能力边界、PRTS 现成作业适配与本地作业验收 |
| [可替换求解策略与机制模型](copilot/solver-architecture.md) | 共享问题、贪心/回溯策略、进程并行比较及敌人机制模型后续设计 |
| [战斗评估与演习门槛](copilot/combat-model.md) | 逐段伤害、侵蚀与控制状态、地图约束及三次演习后真实作战 |
| [Copilot 剧情协议样本](../tests/fixtures/copilot/README.md) | 原生 helper 生命周期脱敏回放、变化依据与合成验证限制 |
| [AGENTS.md](../AGENTS.md) | 代理工作约定与文档维护规则 |

文档中的命令除非另有说明，均从项目根目录执行。历史记录仅解释演进背景；当前行为以代码、配置及对应测试为依据。
