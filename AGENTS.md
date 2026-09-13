# 仓库协作约定

## 项目与入口

ZOOTd 是由 MAA 和 Waydroid 驱动、仅支持明日方舟国服官服的罗德岛自主运营守护进程。先读 [README.md](README.md) 和[文档索引](docs/README.md)，再按任务阅读相关专题。

- `scripts/run-daily.sh`：完整运行编排；`bin/zootd`：宿主操作入口。
- `maa_planner/`：确定性规划、日志证明、缓存、审计和恢复控制。
- `config/`：受管任务与策略；`systemd/`：用户级定时单元。
- `var/`、`.local/`、`.venv/`：忽略的本地运行状态和依赖，不提交日志、账号状态或凭据。

## 修改与验证

每次开发都必须走完整流程：从已同步的 `main` 创建独立分支，完成修改和验证后 commit，push 分支并创建 PR；PR 只允许 squash merge；合并后切回 `main`，执行 `git pull --rebase`，确认本地与远端一致且工作区干净。代码、配置、文档修改均适用，不能把未提交改动作为完成状态交付。具体命令与异常处理见[开发指南](docs/development.md#分支与提交约定)。

先检查 Git 状态并保留用户已有改动，不把无关工作混入本次提交；不要丢弃改动或强制推送。PR 使用 squash merge，本地同步使用 pull rebase；除此之外不要擅自改写已有提交。除用户明确指定例外外，不直接在 `main` 开发或推送。正常开发按上述流程完成提交、发布和合并；无人值守恢复仍受下文恢复契约约束，不能自动合并 PR。

代码或任务配置修改需运行相关测试；完整本地测试命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

这些测试不启动游戏。纯文档修改检查相对链接、命令与代码是否一致即可；修改恢复 scope 时还需运行相关恢复/契约测试。真实设备验证按[开发指南](docs/development.md#测试策略)执行；不要把启动完整托管、消耗理智或调用真实模型当作普通文档验证。受管运行要求干净的 Git 工作树。

保持确定性授权和证据校验：普通药和源石禁用；preflight 不消耗理智；daily 可重入；普通奖励位于最后独立阶段；失败不可凭退出码或旧日志伪装成成功。不要直接改写 live runtime、generation receipt 或追加式审计。

## 文档维护

- `README.md` 只放核心项目简介、Quick Start 和简短文档入口，避免添加实现细节、长配置示例或事故流水账。
- 部署、操作命令、定时器、更新与排障写入 `docs/operations.md`。
- 库存、基建、剿灭和代理策略写入 `docs/configuration.md`；数据职责、阶段契约与 runtime 机制写入 `docs/architecture.md`。
- 代码结构、测试和 Git 约定写入 `docs/development.md`；LLM 行为与 SDK 维护写入 `docs/recovery.md`。
- 历史事故和旧验收结果写入 `docs/history.md`，明确标注当时行为，不作为当前操作说明。
- 新增或移动文档时更新 `docs/README.md` 及引用链接。命令默认从项目根目录执行，Markdown 链接则相对所在文件解析。
- 行为变更时同步更新对应专题，避免在多处重复维护相同规则；以当前代码、配置和测试核对过时描述。

## 无人值守恢复契约

`docs/llm-recovery-scope.md` 是恢复代理实际读取、按 SHA-256 绑定事故的版本化契约，不是普通历史说明。修改其中的行为约定时更新版本并核对 controller 和测试；不要移动文件或用概述取代它。正在执行恢复事故时，遵循该文件与事故输入的边界；普通开发任务不因此自动获得恢复代理的 push、PR 或宿主操作授权。
