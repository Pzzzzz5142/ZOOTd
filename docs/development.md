# 开发指南

[文档索引](README.md) · [项目首页](../README.md)

面向修改代码、配置与文档的开发者。代理协作约定见根目录 [AGENTS.md](../AGENTS.md)。

## 目录

- `bin/zootd-maa`：使用项目隔离目录的 maa-cli 入口。
- `bin/zootd-planner`：来源同步、库存解析、规划和能力账本 CLI。
- `bin/zootd`：统一宿主入口；`run` 转入完整启动器。
- `bin/zootd-codex-advisor` / `bin/zootd-codex-supervisor`：分别承载规划 NOOP 诊断和非完整模式异常分类；两者均为只读结构化 adapter。
- `bin/zootd-codex-recovery`：完整 run 失败后的无 sandbox 操作型恢复 adapter。
- `requirements.txt` / `.venv/`：官方 Python Codex SDK 的项目本地运行环境；不固定 SDK 版本，随最新稳定版升级，SDK 自动安装配套 Codex runtime。
- `scripts/run-daily.sh`：Waydroid、自动刷图和 daily 的一键编排。
- `maa_planner/dashboard.py` / `web/`：默认监听所有 IPv4 接口的标准库 HTTP 服务与无构建步骤的中文 Web 面板；`tests/test_dashboard.py` 验证历史证据、分页及只读 HTTP 边界。
- `maa_planner/operator_box.py` / `skland.py` / `box_cli.py`：独立 experimental Box 模型、森空岛协议与本机交互登录；`tests/test_skland.py` 使用合成 fixture 离线验证，不发送短信或调用真实账号。进度见 [Copilot 路线图](copilot/README.md)。
- `maa_planner/prts.py` / `prts_cli.py`：独立 experimental PRTS adapter、关卡映射和查询/获取入口；`tests/test_prts.py` 使用合成 fixture 验证，不访问真实 API。
- `maa_planner/copilot_matcher.py`：独立 experimental 离线 matcher、静态身份映射与候选排序；`tests/test_copilot_matcher.py` 覆盖练度、未知数据、助战和全局分配，不启动游戏。
- `maa_planner/`：来源适配、确定性策略、库存、能力证明、缓存、阶段账本及 LLM 权限边界。
- `config/farming.toml`：活动、freshness、选关和库存目标策略。
- `config/material-recipes.toml`：严格校验的经典 T1→T2→T3 合成链，只用于蓝材料等价库存计算，不执行合成。
- `config/fight-decision.jq`：启动器对规划器 `FIGHT` JSON 的独立执行契约。
- `config/annihilation-decision.jq`：周剿灭排程的独立执行契约。
- `config/tasks/sanity-fight.toml`：原生自动连战 Fight（series=0），由 MaaCore 持续清理体力与两天临期药；普通药和源石禁用，关卡非交互注入。
- `config/tasks/verify-fight.toml`：保留的显式单场任务；自动 preflight 不调用它，不额外打一场来检测代理。
- `config/tasks/annihilation.toml`：每次只执行一笔、随后核对客户端周进度的剿灭事务。
- `config/tasks/depot.toml`：把原生 StartUp 与当次仓库扫描合在同一个 MaaCore 任务链中。
- `config/tasks/proxy-preflight.toml`：同一 Core 中用三个 Custom 完成零体力导航与代理开关检查；关卡非交互注入，开始战斗/用药入口由检查专用 overlay 禁止。
- `config/tasks/daily.toml`：可重入的基建、公招和信用商店维护；无人机目标由启动器非交互注入；基建拆成普通设施换班和受保护宿舍恢复两个原生 Infrast 阶段；公招先以 09:00 自动确认普通 3–5 星并保护 `支援机械`，随后用独立的原生 Recruit 阶段以 03:50 自动确认小车；若同槽有保证 4/5 星则仍优先高星，只有 6 星留给人工确认；不会在中途关闭启动器持有的 Waydroid 会话。
- `config/infrast/protected-dorm.json`：四间宿舍的官方自定义排班；没有任何具名干员，只允许从游戏“未进驻”筛选结果自动补位。
- `config/tasks/award-only.toml`：固定放在整轮最后的普通任务奖励领取；同时供轻量 E2E 复用，不进入基建、公招、商店、邮件或战斗。
- `config/profiles/waydroid.toml`：Waydroid 连接配置。
- `config/host.env`：Waydroid profile、`MAA_FARM_MODE` 和运行策略；统一入口固定执行 `daily`。
- `scripts/update-maa-runtime.sh`：隔离安装 stable Core 和全部资源，验证整套候选后原子提升为 live；旧资源脚本仅保留为兼容入口。
- `systemd/`：用户级 service/timer；05:30 安全更新完整 MAA runtime，06:00/18:00 完整链路各预留 9 小时 50 分钟运行与 4 分钟清理。
- `var/`：MaaCore、资源、来源缓存、决策、日志和运行状态，不提交 Git。
- `scripts/update-codex-sdk.sh`：更新项目 SDK 与配套 runtime，使用独立锁避免与 adapter 并发升级。
- `docs/`：按运维、配置、架构、开发、恢复及历史分工的专题文档。

## 测试策略

当前测试套件覆盖：无人登录的 service 与任务边界、非交互运行时任务参数、单进程 Depot 和单次来源刷新、MAA 活动窗口与库存共同授权刷图、活动后理智尾数回退、不安全输入 fail closed、剿灭与能力账本、Depot/HTTP 缓存完整性、只读顾问、绑定事故身份的无 sandbox 持久恢复 SDK 请求与同线程运输重试、历史成功防伪装、修复文件白名单、所有阶段可重入、scope blocker、升级前后事故差异、自选修复分支/PR 的独立校验、整轮成功校验、阶段历史哈希链，以及 Core＋资源整代原子更新、generation receipt 与显式回滚。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

前端交互另有可选浏览器测试 `tests/test_dashboard_ui.py`，使用合成 API 响应，覆盖阶段计数、筛选、自动刷新、恢复时间线、缺失/异常记录、文本转义、手机布局及侧栏导航/浏览器前进后退/详情直达。默认环境未安装 Playwright 时跳过这组测试；需要验证前端时，可在项目外的临时环境安装并执行，不增加运行时依赖：

```bash
python3 -m venv /tmp/zootd-ui-check
/tmp/zootd-ui-check/bin/pip install playwright
/tmp/zootd-ui-check/bin/python -m playwright install chromium
PYTHONDONTWRITEBYTECODE=1 /tmp/zootd-ui-check/bin/python -m unittest discover -s tests -p test_dashboard_ui.py -v
```

这组测试完全在本地运行；更新器场景使用临时目录、假 Maa 和本地 Git 仓库，不启动 MaaCore、ADB、Waydroid 或游戏。真实设备 E2E 另用 `award-only.toml`，只在设备空闲时手工执行。

## 修改与运行历史

### 分支与提交约定

本仓库主分支为 `main`。每次开发，包括代码、配置和文档修改，都必须在独立分支完成，并提交、推送、创建 PR。PR 只允许 squash merge；合并后切回 `main` 并用 `git pull --rebase` 同步远端，交付时必须保持工作区干净且本地 `main` 与 `origin/main` 一致。除用户明确指定本次例外外，不直接在 `main` 修改、提交或推送。

标准流程：

1. 执行 `git fetch origin` 和 `git status`，确认没有遗留改动；切回 `main` 后检查 `git log --oneline origin/main..main`，确认没有未推送提交，再执行 `git pull --rebase`。
2. 用 `git switch -c <开发分支>` 创建本次工作的独立分支，再开始修改。
3. 完成相关验证，检查 diff，将本次改动明确加入暂存区并创建普通 commit。每次改动都要提交，不能以 dirty 工作区结束任务，也不能仅为绕过运行检查而提交未经核对的改动。
4. 执行 `git push -u origin <开发分支>`，通过 `gh pr create` 创建面向 `main` 的 PR，说明最终行为和验证结果。
5. 确认 PR 检查和仓库合并条件满足后，执行 `gh pr merge <PR编号> --squash`，将本次 PR 的改动合为一个提交进入 `main`。不使用 merge commit 或 GitHub 的 rebase merge；本地 `git pull --rebase` 用于同步，与 PR 合并方式是不同操作。不擅自 amend 或改写其他已有提交，不 force-push，也不绕过检查或分支保护。
6. 确认 PR 已合并后执行 `git switch main`、`git pull --rebase`，再用 `git status --short` 和 `git rev-list --left-right --count main...origin/main` 确认无未提交文件、提交差异为 `0 0`。

若发现遗留的未提交改动或本地 `main` 上未推送的提交，先用备份分支、stash 或补丁妥善保存，再整理到对应开发分支；不得删除或混入无关工作。不要把仅存在于本地 `main` 的提交带入新 PR 分支后又留在本地 `main`，以免后续远端合并导致主分支分叉。遇到凭据、网络或 PR 检查阻塞时，保留已验证的本地提交和干净工作区，明确报告尚未完成的 push、PR 或合并步骤，不声称流程已完成。

managed run 启动前要求 Git 工作树为 clean；未提交的代码修改会直接阻止无人值守任务，避免执行一个无法从历史恢复的版本。**无人值守恢复不适用自动合并步骤**：恢复 agent 的 tracked 修改必须位于 base 后代的独立分支并先提交，只能发 PR，不能合并；controller 会校验分支 ref、commit ancestry、changed paths、active checkout 和 GitHub PR URL。恢复后的 checkout 状态遵循[恢复 scope](llm-recovery-scope.md)，未声明或 dirty 的变化不能记为恢复成功。

代码历史与运行历史分开保存：Git 记录可执行代码和文档；`var/state/supervisor/runs/` 记录本地游戏运行证据，不提交可能含账号状态的日志。每个 run 起始事件固定当时的 Git HEAD、dirty 标志、status 哈希和 tracked diff 哈希；每个后续事件包含前一事件 SHA-256 且以 exclusive-create 写入，既不能覆盖旧阶段，也不能给同一阶段补写第二个“更好看”的终态。`latest-run.json` 只是可变索引，不是历史真相。
