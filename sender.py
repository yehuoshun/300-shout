"""发送层：模拟键盘发聊天。频道切换序列由配置注入（实测后填）。"""
import logging
import time

import pydirectinput

log = logging.getLogger("300-shout.sender")


def send_chat(text: str, open_key: str = "enter",
              channel_keys: list = None,
              type_delay: float = 0.08, enter_delay: float = 0.15) -> None:
    """打开聊天框 → 切频道(可选) → 输入台词 → 发送。

    channel_keys 示例（按实测填写）：
      []                → 当前频道直发
      ["tab"]           → Tab 切一次频道
      ["shift", "enter"]→ Shift+Enter 组合（类LOL全部聊天）
    """
    pydirectinput.press(open_key)
    time.sleep(enter_delay)
    if channel_keys:
        for key in channel_keys:
            pydirectinput.press(key)
        time.sleep(enter_delay)
    pydirectinput.write(text, interval=type_delay)
    time.sleep(0.08)
    pydirectinput.press(open_key)
    log.info("已发送: %s", text)
