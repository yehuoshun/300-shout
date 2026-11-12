"""前台窗口校验 + 全局热键（纯 ctypes 实现，不引第三方依赖）。

为什么需要这两样：

* **前台窗口校验**：本工具靠模拟键盘把台词打进聊天框。只要游戏不在前台
  （Alt-Tab 去看攻略、游戏掉线退回桌面、点了控制台窗口），Enter 与台词就会
  落进别的窗口——轻则白打，重则把半句话发进别人的聊天框。发送前校验前台
  窗口标题，可以彻底堵掉这类事故。
* **全局热键**：主循环只在两次抓帧之间处理事件，误触发时最好的止血方式是
  一键退出/暂停，而不是去点控制台（点控制台本身又会切换前台窗口）。

非 Windows 平台（CI 的 ubuntu-latest）上所有接口安全降级：拿不到标题返回
``None``、热键永不触发，因此本模块可以在任何平台被导入与单元测试。
"""
import ctypes
import logging
import sys

log = logging.getLogger("300-shout.guard")

IS_WINDOWS = sys.platform == "win32"

# 虚拟键码表（只列本工具可能用到的，够用即可）
VK_CODES = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
}


def _user32():
    """返回 user32 句柄；非 Windows 返回 None。"""
    if not IS_WINDOWS:
        return None
    try:
        return ctypes.windll.user32  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 极端环境下 windll 不可用
        return None


# ---------------------------------------------------------------- 窗口标题
def foreground_title():
    """当前前台窗口标题；拿不到（非 Windows / 无窗口）返回 None。"""
    u = _user32()
    if u is None:
        return None
    try:
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        n = int(u.GetWindowTextLengthW(hwnd))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception as e:  # noqa: BLE001
        log.debug("读取前台窗口标题失败: %s", e)
        return None


def title_matches(title, keywords) -> bool:
    """标题是否命中关键词白名单（大小写不敏感的子串匹配）。

    ``keywords`` 为空 → 视为不限制（返回 True），便于「先关校验再调」。
    ``title`` 为 None（拿不到标题）→ 返回 False：宁可不喊，也不要喊错窗口。
    """
    keys = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
    if not keys:
        return True
    if not title:
        return False
    low = str(title).lower()
    return any(k in low for k in keys)


def list_windows():
    """列出可见顶层窗口标题，用于填 ``chat.foreground_titles``。"""
    u = _user32()
    if u is None:
        return []
    titles = []
    try:
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lparam):  # pragma: no cover - 需要真实桌面
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                n = int(u.GetWindowTextLengthW(hwnd))
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd, buf, n + 1)
                    if buf.value.strip():
                        titles.append(buf.value)
            except Exception:  # noqa: BLE001
                pass
            return True

        u.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception as e:  # noqa: BLE001
        log.warning("枚举窗口失败: %s", e)
    return titles


# ---------------------------------------------------------------- 全局热键
def key_down(vk: int) -> bool:
    """该虚拟键当前是否按下（GetAsyncKeyState 高位）。"""
    u = _user32()
    if u is None:
        return False
    try:
        u.GetAsyncKeyState.restype = ctypes.c_short
        return bool(u.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


class HotkeyWatcher:
    """轮询式全局热键（边沿检测：按住只算一次）。

    不装键盘钩子、不起线程，主循环每帧 ``poll()`` 一次即可——与「零常驻」
    的设计一致，进程退出后不留任何后台东西。
    """

    def __init__(self, keys=("f8", "f9"), state_fn=None):
        self._keys = [str(k).strip().lower() for k in keys if str(k).strip()]
        self._state_fn = state_fn or key_down
        self._down = set()
        self.unknown = [k for k in self._keys if k not in VK_CODES]
        for k in self.unknown:
            log.warning("未知热键 %r（可用：%s）", k, ",".join(sorted(VK_CODES)))

    @property
    def keys(self):
        return list(self._keys)

    def poll(self):
        """返回本次新按下的键名列表（已按下的不重复返回）。"""
        pressed = []
        for k in self._keys:
            vk = VK_CODES.get(k)
            if vk is None:
                continue
            try:
                down = bool(self._state_fn(vk))
            except Exception:  # noqa: BLE001
                down = False
            if down and k not in self._down:
                pressed.append(k)
            if down:
                self._down.add(k)
            else:
                self._down.discard(k)
        return pressed
