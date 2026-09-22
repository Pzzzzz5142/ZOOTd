# Skland 合成 fixture

这些 UID、干员 ID、名字和练度全部为测试合成值；共享技能 `skcom_atk_up[1]` 是公开游戏标识，用于方括号格式回归。fixture 不包含真实玩家响应，不包含真实凭据。字段布局依据 [Skland 协议调研](../../../docs/copilot/phase-0-skland-box.md)。

`player.json` 有意包含应丢弃的昵称、基建和皮肤字段，以及未知练度、已解锁/未解锁模组，用来验证最小化存储和 unknown 语义。它不代表真实账号验收证据。
