# 项目文档

[返回项目首页](../README.md)

| 文档 | 内容 |
|---|---|
| [运维手册](operations.md) | 初始化、手动运行、systemd、更新回滚、Web 监控面板、排障与审计状态 |
| [策略与配置](configuration.md) | 库存目标、材料等价换算、基建、周剿灭与代理隔离 |
| [架构与执行契约](architecture.md) | 数据职责、完整链路、拒绝条件与 runtime 一致性 |
| [开发指南](development.md) | 代码目录、测试、Git 约定与运行证据 |
| [LLM 诊断与整轮恢复](recovery.md) | adapter 职责、恢复验证、连通性探针与 SDK 更新 |
| [恢复 scope 与 FAQ](llm-recovery-scope.md) | 恢复代理实际读取的版本化操作契约 |
| [历史事故与验收记录](history.md) | 旧实现的事故、修复背景与当时验收结果 |
| [AGENTS.md](../AGENTS.md) | 代理工作约定与文档维护规则 |

文档中的命令除非另有说明，均从项目根目录执行。历史记录仅解释演进背景；当前行为以代码、配置及对应测试为依据。
