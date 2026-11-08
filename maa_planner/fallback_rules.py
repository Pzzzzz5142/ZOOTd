"""Declarative detector code; parsed as literals, never imported or executed.

Automatic maintenance may change only RULES and replay fixtures. Host actions,
signal production, recurrence gates and success verification live elsewhere.
"""

RULES = [
    {
        "id": "official-input-anr",
        "version": 1,
        "label": "官服客户端输入 ANR",
        "requires": {"startup_failed": True, "fresh_official_input_anr": True, "game_offline": False},
        "action": "restart-waydroid-session",
    },
]
