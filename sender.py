"""发送层：模拟键盘发聊天。频道切换序列由配置注入（实测后填）。

``pydirectinput`` 是 Windows-only，故惰性导入：既能在非 Windows 上导入本模块
做单元测试，也能通过 ``kb=`` 参数注入假键盘实现。
"""
import logging
import time

log = logging.getLogger("300-shout.sender")


def _load_keyboard():
    import pydirectinput
    return pydirectinput


def _press_token(kb, token: str, delay: float) -> None:
    """按下一个按键 token，支持组合键。

    - 单键：``"tab"``       → press("tab")
    - 组合：``"shift+enter"``→ keyDown("shift") → press("enter") → keyUp("shift")

    注意：旧写法把它拆成 ``["shift", "enter"]`` 是**两个独立按键**（shift 按下即
    松开），不构成组合键；真要 Shift+Enter 必须用 ``"shift+enter"`` 这种写法。
    """
    keys = [k.strip().lower() for k in str(token).split("+") if k.strip()]
    if not keys:
        return
    if len(keys) == 1:
        kb.press(keys[0])
    else:
        for k in keys[:-1]:
            kb.keyDown(k)
        kb.press(keys[-1])
        for k in reversed(keys[:-1]):
            kb.keyUp(k)
    time.sleep(delay)


def send_chat(text: str, open_key: str = "enter",
              channel_keys: list = None,
              type_delay: float = 0.08, enter_delay: float = 0.15,
              kb=None) -> None:
    """打开聊天框 → 切频道(可选) → 输入台词 → 发送。

    channel_keys 示例（按实测填写）：
      []                 → 当前频道直发
      ["tab"]            → Tab 切一次频道
      ["shift+enter"]    → Shift+Enter 组合（类 LOL 全部聊天）
    """
    kb = kb or _load_keyboard()
    kb.press(open_key)
    time.sleep(enter_delay)
    for token in (channel_keys or []):
        _press_token(kb, token, enter_delay)
    if channel_keys:
        time.sleep(enter_delay)
    kb.write(text, interval=type_delay)
    time.sleep(0.08)
    kb.press(open_key)
    log.info("已发送: %s", text)
