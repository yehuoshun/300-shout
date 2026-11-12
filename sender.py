"""发送层：模拟键盘发聊天。频道切换序列由配置注入（实测后填）。

``pydirectinput`` 是 Windows-only，故惰性导入：既能在非 Windows 上导入本模块
做单元测试，也能通过 ``kb=`` 参数注入假键盘实现。

两种输入方式（``input_method``）：

* ``unicode``（默认）：``pydirectinput.write`` 走 KEYEVENTF_UNICODE，免输入法，
  对绝大多数窗口有效。
* ``clipboard``：先把台词写进剪贴板，再 Ctrl+V 粘贴。部分老引擎（DirectInput）
  会吞掉 Unicode 注入的字符，剪贴板粘贴是那种情况下的兜底；代价是临时占用
  剪贴板。
"""
import logging
import sys
import time

log = logging.getLogger("300-shout.sender")

IS_WINDOWS = sys.platform == "win32"

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _load_keyboard():
    import pydirectinput
    return pydirectinput


def set_clipboard_text(text: str) -> None:
    """把文本写进 Windows 剪贴板（CF_UNICODETEXT），无第三方依赖。"""
    if not IS_WINDOWS:
        raise OSError("剪贴板输入法仅支持 Windows")
    import ctypes

    user32 = ctypes.windll.user32   # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    data = (text + "\x00").encode("utf-16-le")  # 双字节结尾的 UTF-16 串
    if not user32.OpenClipboard(None):
        raise OSError("打开剪贴板失败（可能被其他程序占用）")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("GlobalAlloc 失败")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            raise OSError("GlobalLock 失败")
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError("SetClipboardData 失败")
        # 成功后内存归系统所有，不能再 GlobalFree
    finally:
        user32.CloseClipboard()


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


def _paste(kb, text: str, delay: float, setter=None) -> None:
    """剪贴板输入法：写入剪贴板 → Ctrl+V。"""
    setter = setter or set_clipboard_text
    setter(text)
    time.sleep(delay)          # 给剪贴板一点落定时间
    kb.keyDown("ctrl")
    kb.press("v")
    kb.keyUp("ctrl")
    time.sleep(delay)


def send_chat(text: str, open_key: str = "enter",
              channel_keys: list = None,
              type_delay: float = 0.08, enter_delay: float = 0.15,
              kb=None, input_method: str = "unicode", clipboard_setter=None) -> None:
    """打开聊天框 → 切频道(可选) → 输入台词 → 发送。

    channel_keys 示例（按实测填写）：
      []                 → 当前频道直发
      ["tab"]            → Tab 切一次频道
      ["shift+enter"]    → Shift+Enter 组合（类 LOL 全部聊天）
    """
    kb = kb or _load_keyboard()
    method = str(input_method or "unicode").strip().lower()
    kb.press(open_key)
    time.sleep(enter_delay)
    for token in (channel_keys or []):
        _press_token(kb, token, enter_delay)
    if channel_keys:
        time.sleep(enter_delay)
    if method == "clipboard":
        _paste(kb, text, type_delay, setter=clipboard_setter)
    else:
        if method != "unicode":
            log.warning("未知 input_method=%r，按 unicode 处理", input_method)
        kb.write(text, interval=type_delay)
    time.sleep(0.08)
    kb.press(open_key)
    log.info("已发送: %s", text)
