"""图形控制面板（Tkinter）：双击 exe 就能开，关窗即退。

设计边界（见 AGENTS.md 红线 5「非必要不常驻」）：
- 只在用户点开时存在，关窗即退出；不做托盘、不自启、不开后台服务、不联网。
- 识别主循环仍然只有一份，跑在 ``main.run()`` 里（工作线程），面板只负责展示
  与下发「开始 / 停止」。所有控件操作都在主线程，工作线程只往队列里塞事件。

视觉约定（改样式先看这里）：
- 只用标准库自绘：Tk 原生控件外观太旧，所以卡片 / 按钮 / 进度条都是 Canvas 画出来的
  圆角矩形 + 文本，配色和间距统一走下面的 ``PALETTES`` 与 ``px()`` 比例尺。
- 间距按 8pt 网格（4 / 8 / 12 / 16 / 24），圆角 14px 卡片、10px 按钮，
  字号用像素（负数）而不是磅，避免高分屏上文字与几何对不齐。
- 深浅两套配色等权对待：切换时整窗重建（比让每个控件自己换色简单，也不会漏）。

tkinter 延迟导入：无图形环境的 CI 上 ``import ui`` 不能炸，所以本模块顶层
不 import tkinter，纯逻辑（格式化 / 台词池 / 会话）单独可测。
"""
import json
import logging
import math
import os
import queue
import threading

log = logging.getLogger("300-shout.ui")

LEVELS = ("idle", "ok", "run", "warn", "bad")

# 状态级别 → 语义色（每套配色自己实现，这里只是兜底默认值）
COLORS = {
    "idle": "#6b7280",
    "ok": "#12924f",
    "run": "#2f6bff",
    "warn": "#b25e09",
    "bad": "#c8281c",
}

# 界面风格（v3.3 起可切换）：每套给一份完整配色 + 两个圆角 token。
# 控件只认 palette 里那 19 个键（键集必须一致，缺一个就会在某个控件上 KeyError），
# 圆角只影响卡片与按钮的方/圆手感，改风格时不用碰任何绘制代码。
STYLES = {
    "simple-light": {
        "label": "简洁 · 浅色",
        "hint": "纯色、圆角、克制",
        "card_radius": 14,
        "btn_radius": 10,
        "palette": {
            "bg": "#eef1f5",
            "surface": "#ffffff",
            "surface_alt": "#f6f8fb",
            "border": "#dde3ec",
            "border_soft": "#e8edf4",
            "text": "#111827",
            "muted": "#6b7280",
            "faint": "#9aa3af",
            "accent": "#2f6bff",
            "accent_hover": "#2457d6",
            "accent_fg": "#ffffff",
            "ok": "#12924f",
            "run": "#2f6bff",
            "warn": "#b25e09",
            "bad": "#c8281c",
            "idle": "#6b7280",
            "track": "#e7ecf4",
            "select": "#e8efff",
            "press": "#eef2fd",
        },
    },
    "simple-dark": {
        "label": "简洁 · 深色",
        "hint": "夜里不刺眼",
        "card_radius": 14,
        "btn_radius": 10,
        "palette": {
            "bg": "#0e1116",
            "surface": "#171b22",
            "surface_alt": "#1d222b",
            "border": "#2a3140",
            "border_soft": "#232935",
            "text": "#e9edf4",
            "muted": "#98a2b3",
            "faint": "#6b7484",
            "accent": "#5b8cff",
            "accent_hover": "#7aa2ff",
            "accent_fg": "#0b1020",
            "ok": "#38c98a",
            "run": "#7aa2ff",
            "warn": "#f0a83c",
            "bad": "#ff6f66",
            "idle": "#98a2b3",
            "track": "#252c38",
            "select": "#22304d",
            "press": "#1b2230",
        },
    },
    "neon": {
        "label": "电竞 · 霓虹",
        "hint": "近黑底 + 青蓝光",
        "card_radius": 6,
        "btn_radius": 6,
        "palette": {
            "bg": "#08080e",
            "surface": "#111119",
            "surface_alt": "#171722",
            "border": "#26263c",
            "border_soft": "#1c1c2a",
            "text": "#eef1ff",
            "muted": "#9ba2c4",
            "faint": "#6b7192",
            "accent": "#2ed3f0",
            "accent_hover": "#5fe2ff",
            "accent_fg": "#04121a",
            "ok": "#2ee6a8",
            "run": "#5cd1ff",
            "warn": "#ffc247",
            "bad": "#ff5f7a",
            "idle": "#8b90b5",
            "track": "#1e1e2d",
            "select": "#14314a",
            "press": "#191a28",
        },
    },
    "paper": {
        "label": "纸感 · 米白",
        "hint": "暖白底 + 墨绿",
        "card_radius": 18,
        "btn_radius": 12,
        "palette": {
            "bg": "#f1eae0",
            "surface": "#fdfaf4",
            "surface_alt": "#f5efe4",
            "border": "#e6dccb",
            "border_soft": "#efe7d9",
            "text": "#2c2721",
            "muted": "#7c7161",
            "faint": "#a89d8b",
            "accent": "#1e5f4d",
            "accent_hover": "#174a3b",
            "accent_fg": "#fdfaf4",
            "ok": "#2b7a52",
            "run": "#1e5f4d",
            "warn": "#a2651a",
            "bad": "#b03b28",
            "idle": "#8a8070",
            "track": "#e9e0d1",
            "select": "#e2ece4",
            "press": "#ece5d7",
        },
    },
}

DEFAULT_STYLE = "simple-light"

# 老代码 / 文档里说的是「配色」，实际就是风格里的 palette，这里留个别名方便引用。
PALETTES = {key: spec["palette"] for key, spec in STYLES.items()}


def resolve_style(name):
    """认不出来就回落到默认风格（配置文件被手改坏了也不该开不出面板）。"""
    return name if isinstance(name, str) and name in STYLES else DEFAULT_STYLE


def style_label(key) -> str:
    return STYLES[resolve_style(key)]["label"]


def style_from_config(cfg) -> str:
    """从配置里取 ui.style；缺省 / 类型不对都回落默认。"""
    ui_cfg = (cfg or {}).get("ui")
    if not isinstance(ui_cfg, dict):
        return DEFAULT_STYLE
    return resolve_style(ui_cfg.get("style"))


# ---------------------------------------------------------------- 纯逻辑（可测）
def fmt_score(value) -> str:
    """相似度分数显示：None/NaN → em dash，其余保留两位。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(v) or math.isinf(v):
        return "—"
    return f"{v:.2f}"


def score_line(ready=None, cd=None, threshold=None) -> str:
    return f"ready {fmt_score(ready)}   cd {fmt_score(cd)}   阈值 {fmt_score(threshold)}"


def status_view(*, calibrated: bool, running: bool, ready=None, threshold=None,
                locked: bool = False) -> tuple:
    """把内部状态翻成人话，返回 (文字, 级别)。级别取 LEVELS 之一，用于配色。"""
    if not calibrated:
        return ("未标定 · 先点「标定 R 槽」框选大招图标", "warn")
    if not running:
        return ("已停止 · 点「开始监控」开始识别", "idle")
    if ready is None:
        return ("监控中 · 等待画面……", "run")
    if locked:
        return ("刚触发过 · 冷却中（防连发），等它过去", "ok")
    th = 0.8 if threshold is None else float(threshold)
    r = float(ready)
    if r >= th:
        return (f"待命中 · 大招已就绪（{r:.2f} ≥ {th:.2f}），开大就会喊", "ok")
    return (f"监控中 · 大招未就绪（{r:.2f} < {th:.2f}）", "run")


GUIDE = {
    "未标定": "先点「标定 R 槽」，框住游戏里的大招图标",
    "已停止": "点「开始监控」，识别跑起来后再进游戏开大",
    "启动中": "正在抓第一帧画面……",
    "冷却中": "刚喊过一句，冷却期内不会重复触发",
    "待命中": "大招已就绪，按下 R 就会喊",
    "监控中": "还没看到大招图标，继续等待",
}


def status_brief(*, calibrated: bool, running: bool, ready=None, threshold=None,
                 locked: bool = False) -> tuple:
    """给面板用的 (短标签, 引导语, 级别)：标签进胶囊，引导语单独一行，避免一句话太长。"""
    if not calibrated:
        label, level = "未标定", "warn"
    elif not running:
        label, level = "已停止", "idle"
    elif ready is None:
        label, level = "启动中", "run"
    elif locked:
        label, level = "冷却中", "ok"
    else:
        th = 0.8 if threshold is None else float(threshold)
        label, level = ("待命中", "ok") if float(ready) >= th else ("监控中", "run")
    return (label, GUIDE[label], level)


class PhraseBook:
    """台词池：去重、长度与条数上限都在这里，界面只负责显示。"""

    MAX = 30
    MAX_LEN = 60

    def __init__(self, phrases=()):
        self._items = []
        for p in phrases or ():
            text = str(p).strip()
            if text and text not in self._items:
                self._items.append(text)

    def __len__(self) -> int:
        return len(self._items)

    def add(self, text):
        """返回 (是否成功, 失败原因 或 新增的那条)。"""
        text = (text or "").strip()
        if not text:
            return (False, "空格子，先说一句想喊的话。")
        if len(text) > self.MAX_LEN:
            return (False, f"太长了（{len(text)} 字），{self.MAX_LEN} 字以内。")
        if text in self._items:
            return (False, "这句已经有了。")
        if len(self._items) >= self.MAX:
            return (False, f"最多 {self.MAX} 条，先删一条。")
        self._items.append(text)
        return (True, text)

    def remove(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def replace(self, index, text):
        text = (text or "").strip()
        if not text or len(text) > self.MAX_LEN or not 0 <= index < len(self._items):
            return False
        self._items[index] = text
        return True

    def as_list(self):
        return list(self._items)


def _patch_config(config_path: str, patch) -> bool:
    """读 → 交给 ``patch`` 改 → 写回。只动 patch 碰的键，其余原样保留。

    先写 ``.tmp`` 再 ``os.replace``：中途断电也不会留下半个 config.json。
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("读不了配置 %s：%s", config_path, exc)
        return False
    if not isinstance(data, dict):
        log.warning("配置不是 JSON 对象，不改：%s", config_path)
        return False
    patch(data)
    tmp = config_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path)
    except OSError as exc:
        log.warning("写不了配置 %s：%s", config_path, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


def save_phrases(config_path: str, phrases) -> bool:
    """只改 config.json 里的 phrases 键。"""
    def patch(data):
        data["phrases"] = [str(p) for p in phrases]

    return _patch_config(config_path, patch)


def save_ui_style(config_path: str, style) -> bool:
    """只改 config.json 里的 ui.style，其它（含 ui 段里将来的键）原样保留。"""
    key = resolve_style(style)

    def patch(data):
        ui_cfg = data.get("ui")
        if not isinstance(ui_cfg, dict):
            ui_cfg = {}
            data["ui"] = ui_cfg
        ui_cfg["style"] = key

    return _patch_config(config_path, patch)


class Session:
    """把 ``main.run()`` 包成可随时叫停的线程。界面只看 ``running``。"""

    def __init__(self, runner):
        self._runner = runner
        self._thread = None
        self._stop = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, on_event=None) -> bool:
        if self.running:
            return False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._target, args=(self._stop, on_event), daemon=True, name="300-shout-run")
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> bool:
        if not self.running:
            return False
        self._stop.set()
        self._thread.join(timeout)
        return True

    def _target(self, stop_event, on_event):
        try:
            self._runner(stop_event, on_event)
        except SystemExit:
            pass
        except Exception as exc:  # noqa: BLE001 界面上要能看到崩因，不能静默
            log.exception("监控线程异常：%s", exc)
            if on_event:
                try:
                    on_event("error", {"text": f"监控线程异常：{exc}"})
                except Exception:  # noqa: BLE001
                    pass


class QueueHandler(logging.Handler):
    """把日志记录塞进队列，由界面线程抽走——避免跨线程直接动 Tk 控件。"""

    def __init__(self, sink: queue.Queue):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        try:
            self.sink.put_nowait(("log", self.format(record)))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 自绘控件
_KIT = None


def _mix(color: str, other: str, t: float) -> str:
    """把 color 往 other 混 t（0..1）。用来算 hover / 淡底色，省得手写一堆色值。"""
    def parts(c):
        c = c.lstrip("#")
        return [int(c[i:i + 2], 16) for i in (0, 2, 4)]

    a, b = parts(color), parts(other)
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _round_rect(canvas, x0, y0, x1, y1, r, fill, tag):
    """圆角矩形＝两个矩形 + 四个圆，比 smooth 多边形锐利，也不用图片。"""
    items = [
        canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline="", tags=tag),
        canvas.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline="", tags=tag),
    ]
    for cx, cy in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r), (x1 - 2 * r, y1 - 2 * r)):
        items.append(canvas.create_oval(cx, cy, cx + 2 * r, cy + 2 * r,
                                        fill=fill, outline="", tags=tag))
    return items


def _make_kit():
    """在真正有 tkinter 时才定义控件类（顶层 import 会拖垮无图形环境的 CI）。"""
    import tkinter as tk
    import tkinter.font as tkfont

    UI = "Microsoft YaHei UI"
    MONO = "Consolas"

    class Card(tk.Canvas):
        """白底圆角卡片：自己画背景，内容放进 ``body`` 这个 Frame。"""

        def __init__(self, parent, palette, radius=14, pad=16):
            super().__init__(parent, bg=palette["bg"], highlightthickness=0, bd=0)
            self.palette = palette
            self.radius = radius
            self.pad = pad
            self.body = tk.Frame(self, bg=palette["surface"])
            self._win = self.create_window(pad, pad, window=self.body, anchor="nw")
            self.bind("<Configure>", self._on_resize)

        def _on_resize(self, event):
            w, h = max(event.width, 2), max(event.height, 2)
            self.delete("card")
            _round_rect(self, 0, 0, w - 1, h - 1, self.radius,
                        self.palette["border"], ("card", "edge"))
            _round_rect(self, 1, 1, w - 1.5, h - 1.5, self.radius - 0.5,
                        self.palette["surface"], ("card", "fill"))
            self.tag_lower("card")
            self.itemconfigure(self._win, width=max(w - 2 * self.pad, 40),
                               height=max(h - 2 * self.pad, 40))

    class Well(tk.Canvas):
        """卡片里的一块浅色圆角容器，内容放进 ``body``。列表与日志都要它，
        否则方角浅色块嵌在圆角卡片里，边角会"破相"。"""

        def __init__(self, parent, palette, radius=10, pad=6):
            super().__init__(parent, bg=palette["surface"], highlightthickness=0, bd=0)
            self.palette = palette
            self.radius = radius
            self.pad = pad
            self.body = tk.Frame(self, bg=palette["surface_alt"])
            self._win = self.create_window(pad, pad, window=self.body, anchor="nw")
            self.bind("<Configure>", self._on_resize)

        def _on_resize(self, event):
            w, h = max(event.width, 2), max(event.height, 2)
            self.delete("well")
            _round_rect(self, 0, 0, w - 1, h - 1, self.radius,
                        self.palette["surface_alt"], "well")
            self.tag_lower("well")
            self.itemconfigure(self._win, width=max(w - 2 * self.pad, 20),
                               height=max(h - 2 * self.pad, 20))

    class Button(tk.Canvas):
        """圆角按钮：primary / secondary / quiet 三种。悬停、按下、禁用都有反馈。"""

        PAD_X = 14
        GAP = 6

        def __init__(self, parent, palette, text, command=None, kind="secondary",
                     icon="", font_size=13, height=36, min_width=0, radius=10,
                     surface=False):
            self.palette = palette
            self.kind = kind
            self.text = text
            self.icon = icon
            self.font_size = font_size
            self.height = height
            self.radius = radius
            self.min_width = min_width
            self.command = command
            self._enabled = True
            self._hover = False
            self._press = False
            self._font = tkfont.Font(family=UI, size=-font_size, weight="normal")
            self.base = palette["surface"] if surface else palette["bg"]
            width = self._measure() + 2 * self.PAD_X
            super().__init__(parent, width=max(width, min_width), height=height,
                             bg=self.base, highlightthickness=0, bd=0, cursor="hand2")
            self.bind("<Enter>", self._on_enter)
            self.bind("<Leave>", self._on_leave)
            self.bind("<ButtonPress-1>", self._on_press)
            self.bind("<ButtonRelease-1>", self._on_release)
            self._paint()

        # -- 尺寸与配色 --
        def _measure(self) -> int:
            label = (self.icon + "  " if self.icon else "") + self.text
            return self._font.measure(label)

        def _colors(self):
            p = self.palette
            if not self._enabled:
                base = p["surface_alt"] if self.kind != "quiet" else self.base
                return (base, p["faint"], None)
            if self.kind == "primary":
                fill = p["accent_hover"] if (self._hover or self._press) else p["accent"]
                return (fill, p["accent_fg"], None)
            if self.kind == "secondary":
                fill = p["surface_alt"] if (self._hover or self._press) else p["surface"]
                return (fill, p["text"], p["border"])
            fill = p["press"] if (self._hover or self._press) else self.base
            return (fill, p["muted"], None)

        def _paint(self):
            fill, fg, border = self._colors()
            w = max(self.winfo_reqwidth(), self.winfo_width())
            h = self.winfo_reqheight()
            self.delete("all")
            if border:
                _round_rect(self, 0, 0, w - 1, h - 1, self.radius, border, "btn")
            inset = 1 if border else 0
            _round_rect(self, inset, inset, w - 1 - inset, h - 1 - inset,
                        self.radius - inset, fill, "btn")
            label = (self.icon + "  " if self.icon else "") + self.text
            self.create_text(w / 2, h / 2, text=label, fill=fg,
                             font=(UI, -self.font_size,
                                   "bold" if self.kind == "primary" else "normal"),
                             tags="btn")

        # -- 交互 --
        def _on_enter(self, _e):
            self._hover = True
            self._paint()

        def _on_leave(self, _e):
            self._hover = self._press = False
            self._paint()

        def _on_press(self, _e):
            if not self._enabled:
                return
            self._press = True
            self._paint()

        def _on_release(self, _e):
            was = self._press
            self._press = False
            self._paint()
            if was and self._enabled and self.command:
                self.command()

        def set_enabled(self, enabled: bool):
            enabled = bool(enabled)
            if enabled != self._enabled:
                self._enabled = enabled
                self.configure(cursor="hand2" if enabled else "arrow")
                self._paint()

        def set_text(self, text: str):
            if text != self.text:
                self.text = text
                self.configure(width=max(self._measure() + 2 * self.PAD_X, self.min_width))
                self._paint()

    class Pill(tk.Canvas):
        """状态胶囊：一个圆点 + 一句话，底色是语义色的淡版。"""

        def __init__(self, parent, palette, text="", level="idle", font_size=13, height=34,
                     autosize=True):
            self._font = tkfont.Font(family=UI, size=-font_size, weight="bold")
            width = self._font.measure(text) + 2 * 12 + 30 if autosize else 10
            super().__init__(parent, height=height, width=width, bg=palette["surface"],
                             highlightthickness=0, bd=0)
            self.palette = palette
            self.font_size = font_size
            self.level = level
            self.text = text
            self.bind("<Configure>", lambda _e: self._paint())

        def set(self, text: str, level: str = "idle"):
            if (text, level) == (self.text, self.level):
                return
            self.text, self.level = text, level
            self.configure(width=self._font.measure(text) + 2 * 12 + 30)
            self._paint()

        def _paint(self):
            p = self.palette
            color = p.get(self.level, p["idle"])
            h = int(self.cget("height"))
            w = max(self.winfo_width(), 10)
            self.delete("all")
            if w < 20:  # 还没布局完，等 <Configure> 再来
                return
            bg = _mix(p["surface"], color, 0.12)
            _round_rect(self, 0, 0, w - 1, h - 1, (h - 1) / 2, bg, "pill")
            r = 3.5
            cy = h / 2
            self.create_oval(12, cy - r, 12 + 2 * r, cy + r, fill=color, outline="", tags="pill")
            self.create_text(12 + 2 * r + 9, cy, text=self.text, anchor="w",
                             fill=color if self.level != "idle" else p["muted"],
                             font=(UI, -self.font_size, "bold"), tags="pill")

    class Bar(tk.Canvas):
        """细进度条，带阈值刻度线。"""

        def __init__(self, parent, palette, height=8):
            super().__init__(parent, height=height, bg=palette["surface"],
                             highlightthickness=0, bd=0)
            self.palette = palette
            self.value = None
            self.threshold = None
            self.bind("<Configure>", lambda _e: self._paint())

        def set(self, value, threshold=None):
            self.value, self.threshold = value, threshold
            self._paint()

        def _paint(self):
            p = self.palette
            w = self.winfo_width()
            h = int(self.cget("height"))
            self.delete("all")
            if w < 20:
                return
            r = h / 2
            _round_rect(self, 0, 0, w - 1, h - 1, r, p["track"], "bar")
            v = 0.0
            try:
                if self.value is not None and not math.isnan(float(self.value)):
                    v = max(0.0, min(1.0, float(self.value)))
            except (TypeError, ValueError):
                v = 0.0
            filled = max(h, (w - 1) * v) if v > 0 else 0
            if filled:
                color = p["ok"] if v >= (self.threshold or 0.8) else p["accent"]
                _round_rect(self, 0, 0, filled, h - 1, r, color, "bar")
            if self.threshold is not None:
                try:
                    tx = max(1.0, min(w - 2.0, (w - 1) * float(self.threshold)))
                except (TypeError, ValueError):
                    tx = None
                if tx:
                    self.create_line(tx, -1, tx, h, fill=p["surface"], width=2, tags="bar")

    class Metric(tk.Frame):
        """一行指标：左边小标题、右边大数字，下面是进度条。"""

        def __init__(self, parent, palette, title, unit="", font_size=26):
            super().__init__(parent, bg=palette["surface"])
            self.palette = palette
            self.font_size = font_size
            head = tk.Frame(self, bg=palette["surface"])
            head.pack(fill="x")
            tk.Label(head, text=title, bg=palette["surface"], fg=palette["text"],
                     font=(UI, -13)).pack(side="left")
            self.unit = tk.Label(head, text=unit, bg=palette["surface"], fg=palette["faint"],
                                 font=(UI, -11))
            self.unit.pack(side="left", padx=(6, 0))
            self.value = tk.Label(head, text="—", bg=palette["surface"], fg=palette["text"],
                                  font=(MONO, -font_size, "bold"))
            self.value.pack(side="right")
            self.bar = Bar(self, palette, height=8)
            self.bar.pack(fill="x", pady=(8, 0))

        def set(self, value, threshold=None):
            self.value.configure(text=fmt_score(value))
            self.bar.set(value, threshold)

    return {"tk": tk, "UI": UI, "MONO": MONO, "Card": Card, "Well": Well,
            "Button": Button, "Pill": Pill, "Bar": Bar, "Metric": Metric}


def kit():
    global _KIT
    if _KIT is None:
        _KIT = _make_kit()
    return _KIT


# ---------------------------------------------------------------- 面板
class Panel:
    """Tk 面板。构造时才需要 tkinter 模块对象，便于无 Tk 环境下测试纯逻辑。"""

    MAX_LOG_LINES = 400
    PAD = 24
    GAP = 16

    def __init__(self, tk, config_path: str):
        import main as main_mod

        self.tk = tk
        self.main = main_mod
        self.config_path = config_path
        self.cfg = main_mod.load_config(config_path)
        self.book = PhraseBook(self.cfg.get("phrases") or [])
        self.session = Session(self._worker)
        self.events = queue.Queue()
        self.scores = {"ready": None, "cd": None, "threshold": None, "locked": False}
        self.counters = {"frames": 0, "triggers": 0, "sent": 0, "blocked": 0, "errors": 0}
        self.log_lines = []
        self.handler = None
        self.style = style_from_config(self.cfg)
        self._tool = kit()
        self._build()
        self._attach_logging()
        self.refresh()
        self.append_log("面板已就绪。第一次用：①「标定 R 槽」框选游戏里的大招图标 "
                        "②切到游戏点「开始监控」③开大验证。F8 暂停/继续，F9 退出。")
        self.root.after(80, self._pump)

    # -- 比例尺 ----------------------------------------------------
    @property
    def palette(self):
        return PALETTES[self.style]

    @property
    def spec(self):
        return STYLES[self.style]

    @property
    def r_card(self) -> int:
        """卡片圆角：风格不同粗细不同（电竞风方、纸感风圆）。"""
        return self.px(self.spec["card_radius"])

    @property
    def r_btn(self) -> int:
        return self.px(self.spec["btn_radius"])

    def px(self, value: float) -> int:
        """按屏幕 DPI 放大逻辑像素，高分屏上不至于糊成一小坨。"""
        return max(1, int(round(value * self._scale)))

    # -- 构建界面 --------------------------------------------------
    def _build(self):
        tk = self.tk
        K = self._tool
        P = self.palette

        if getattr(self, "root", None) is None:
            self.root = tk.Tk()
            self._scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
            self.root.title("300 Shout · 自动公屏喊话")
            self.root.minsize(self.px(920), self.px(700))
            self.root.geometry(f"{self.px(1000)}x{self.px(768)}")
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        else:
            for child in self.root.winfo_children():
                child.destroy()
        self.root.configure(bg=P["bg"])

        root = tk.Frame(self.root, bg=P["bg"])
        root.pack(fill="both", expand=True, padx=self.px(self.PAD), pady=self.px(18))
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ---- 顶栏：标识 + 标题 + 右侧工具 ----
        head = tk.Frame(root, bg=P["bg"])
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)

        mark = tk.Canvas(head, width=self.px(40), height=self.px(40), bg=P["bg"],
                         highlightthickness=0, bd=0)
        mark.grid(row=0, column=0, rowspan=2, sticky="w")
        _round_rect(mark, 0, 0, self.px(40) - 1, self.px(40) - 1, self.px(11),
                    P["accent"], "m")
        mark.create_text(self.px(20), self.px(20), text="300", fill=P["accent_fg"],
                         font=(K["UI"], -self.px(15), "bold"), tags="m")

        tk.Label(head, text="300 Shout", bg=P["bg"], fg=P["text"],
                 font=(K["UI"], -self.px(21), "bold")).grid(
            row=0, column=1, sticky="w", padx=(self.px(12), 0))
        tk.Label(head, text="自动公屏喊话 · 纯图像识别 · 零进程接触", bg=P["bg"], fg=P["muted"],
                 font=(K["UI"], -self.px(12))).grid(
            row=1, column=1, sticky="w", padx=(self.px(12), 0), pady=(self.px(2), 0))

        tools = tk.Frame(head, bg=P["bg"])
        tools.grid(row=0, column=2, rowspan=2, sticky="e")
        self._style_var = tk.StringVar(master=self.root, value=self.style)
        self.btn_style = K["Button"](tools, P, f"外观 · {self.spec['label']}",
                                     command=self._open_style_menu, kind="secondary",
                                     font_size=self.px(12), height=self.px(30),
                                     radius=self.px(max(6, self.spec["btn_radius"] - 1)),
                                     min_width=self.px(124))
        self.btn_style.pack(side="right")
        self._ver_chip(tools, P, K).pack(side="right", padx=(0, self.px(8)))

        # ---- 卡片行：状态 + 台词池 ----
        cards = tk.Frame(root, bg=P["bg"])
        cards.grid(row=1, column=0, sticky="ew", pady=(self.px(18), self.px(16)))
        cards.columnconfigure(0, weight=0, minsize=self.px(340))
        cards.columnconfigure(1, weight=1)
        cards.rowconfigure(0, weight=1)

        status_card = K["Card"](cards, P, radius=self.r_card, pad=self.px(18))
        status_card.configure(height=self.px(318))
        status_card.grid(row=0, column=0, sticky="nsew")
        self._build_status_card(status_card.body, K, P)

        phrase_card = K["Card"](cards, P, radius=self.r_card, pad=self.px(18))
        phrase_card.configure(height=self.px(318))
        phrase_card.grid(row=0, column=1, sticky="nsew", padx=(self.px(self.GAP), 0))
        self._build_phrase_card(phrase_card.body, K, P)

        # ---- 日志卡 ----
        log_card = K["Card"](root, P, radius=self.r_card, pad=self.px(18))
        log_card.grid(row=2, column=0, sticky="nsew")
        self._build_log_card(log_card.body, K, P)

        # ---- 操作条 ----
        actions = tk.Frame(root, bg=P["bg"])
        actions.grid(row=3, column=0, sticky="ew", pady=(self.px(16), 0))
        self.btn_start = K["Button"](actions, P, "开始监控", command=self.on_start,
                                     kind="primary", icon="▶", font_size=self.px(13),
                                     height=self.px(38), radius=self.r_btn,
                                     min_width=self.px(132))
        self.btn_start.pack(side="left")
        self.btn_stop = K["Button"](actions, P, "停止", command=self.on_stop,
                                    kind="secondary", icon="■", font_size=self.px(13),
                                    height=self.px(38), radius=self.r_btn,
                                    min_width=self.px(96))
        self.btn_stop.pack(side="left", padx=(self.px(8), self.px(16)))
        self.btn_calib = K["Button"](actions, P, "标定 R 槽", command=self.on_calibrate,
                                     kind="secondary", font_size=self.px(13),
                                     height=self.px(38), radius=self.r_btn)
        self.btn_check = K["Button"](actions, P, "标定体检", command=self.on_calibrate_check,
                                     kind="secondary", font_size=self.px(13),
                                     height=self.px(38), radius=self.r_btn)
        self.btn_test = K["Button"](actions, P, "发送测试", command=self.on_test_send,
                                    kind="secondary", font_size=self.px(13),
                                    height=self.px(38), radius=self.r_btn)
        self.btn_cfg = K["Button"](actions, P, "打开配置", command=self.on_open_config,
                                   kind="quiet", font_size=self.px(13),
                                   height=self.px(38), radius=self.r_btn)
        for b in (self.btn_calib, self.btn_check, self.btn_test):
            b.pack(side="left", padx=(0, self.px(8)))
        self.btn_cfg.pack(side="left", padx=(self.px(8), 0))

        self.foot_var = tk.StringVar(value="")
        tk.Label(actions, textvariable=self.foot_var, bg=P["bg"], fg=P["muted"],
                 font=(K["MONO"], -self.px(12))).pack(side="right", pady=(self.px(2), 0))

        # ---- 底栏：配置路径 ----
        self.hint_var = tk.StringVar(value="")
        tk.Label(root, textvariable=self.hint_var, bg=P["bg"], fg=P["faint"],
                 font=(K["UI"], -self.px(11)), anchor="w").grid(
            row=4, column=0, sticky="w", pady=(self.px(10), 0))

    def _ver_chip(self, parent, P, K):
        tk = self.tk
        c = tk.Canvas(parent, bg=P["bg"], highlightthickness=0, bd=0,
                      height=self.px(28), width=self.px(56))
        c.create_rectangle(0, 0, self.px(56), self.px(28), fill="", outline="")
        c.configure(bg=P["bg"])

        def paint(_e=None):
            w, h = c.winfo_width(), int(c.cget("height"))
            c.delete("all")
            _round_rect(c, 0, (h - self.px(24)) / 2, w - 1, (h + self.px(24)) / 2,
                        self.px(8), P["surface"], "chip")
            c.create_text(w / 2, h / 2, text=f"v{self.main.VERSION}", fill=P["muted"],
                          font=(K["MONO"], -self.px(11)), tags="chip")

        c.bind("<Configure>", paint)
        return c

    def _build_status_card(self, body, K, P):
        tk = self.tk
        body.columnconfigure(0, weight=1)

        tk.Label(body, text="运行状态", bg=P["surface"], fg=P["muted"],
                 font=(K["UI"], -self.px(12))).grid(row=0, column=0, sticky="w")

        self.guide_var = tk.StringVar(value="")
        row = tk.Frame(body, bg=P["surface"])
        row.grid(row=1, column=0, sticky="ew", pady=(self.px(10), 0))
        self.pill = K["Pill"](row, P, "未标定", "warn", font_size=self.px(13),
                              height=self.px(32))
        self.pill.pack(side="left")
        tk.Label(body, textvariable=self.guide_var, bg=P["surface"], fg=P["muted"],
                 font=(K["UI"], -self.px(12)), justify="left", anchor="w",
                 wraplength=self.px(280)).grid(row=2, column=0, sticky="w",
                                               pady=(self.px(8), self.px(16)))

        self.metric_ready = K["Metric"](body, P, "大招待命", unit="ready",
                                        font_size=self.px(26))
        self.metric_ready.grid(row=3, column=0, sticky="ew")
        self.metric_cd = K["Metric"](body, P, "冷却中", unit="cd", font_size=self.px(26))
        self.metric_cd.grid(row=4, column=0, sticky="ew", pady=(self.px(14), 0))

        self.meta_var = tk.StringVar(value="")
        tk.Label(body, textvariable=self.meta_var, bg=P["surface"], fg=P["faint"],
                 font=(K["UI"], -self.px(11))).grid(
            row=5, column=0, sticky="w", pady=(self.px(14), 0))

    def _build_phrase_card(self, body, K, P):
        tk = self.tk
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        head = tk.Frame(body, bg=P["surface"])
        head.grid(row=0, column=0, sticky="ew", pady=(0, self.px(10)))
        head.columnconfigure(1, weight=1)
        tk.Label(head, text="台词池", bg=P["surface"], fg=P["text"],
                 font=(K["UI"], -self.px(14), "bold")).grid(row=0, column=0, sticky="w")
        self.count_var = tk.StringVar(value="")
        tk.Label(head, textvariable=self.count_var, bg=P["surface"], fg=P["faint"],
                 font=(K["UI"], -self.px(11))).grid(row=0, column=1, sticky="w",
                                                    padx=(self.px(8), 0))
        tk.Label(head, text="随机一条，不连喊同一条", bg=P["surface"], fg=P["faint"],
                 font=(K["UI"], -self.px(11))).grid(row=0, column=2, sticky="e")

        well = K["Well"](body, P, radius=self.px(10), pad=self.px(6))
        well.grid(row=1, column=0, sticky="nsew")
        well.body.rowconfigure(0, weight=1)
        well.body.columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(well.body, exportselection=False, bd=0, height=6, width=20,
                                  highlightthickness=0, activestyle="none", relief="flat",
                                  bg=P["surface_alt"], fg=P["text"],
                                  selectbackground=P["select"], selectforeground=P["text"],
                                  font=(K["UI"], -self.px(13)))
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=self.px(4), pady=self.px(4))
        self.listbox.bind("<Double-Button-1>", lambda _e: self.on_del_phrase())

        row = tk.Frame(body, bg=P["surface"])
        row.grid(row=2, column=0, sticky="ew", pady=(self.px(10), 0))
        row.columnconfigure(0, weight=1)
        self.phrase_var = tk.StringVar()
        self.entry = tk.Entry(row, textvariable=self.phrase_var, bd=0, relief="flat",
                              highlightthickness=1, highlightbackground=P["border"],
                              highlightcolor=P["accent"], bg=P["surface_alt"], fg=P["text"],
                              insertbackground=P["text"], font=(K["UI"], -self.px(13)))
        self.entry.grid(row=0, column=0, sticky="ew", ipady=self.px(7))
        self.entry.bind("<Return>", lambda _e: self.on_add_phrase())
        K["Button"](row, P, "添加", command=self.on_add_phrase, kind="secondary",
                    font_size=self.px(12), height=self.px(34), radius=self.px(max(6, self.spec["btn_radius"] - 1)),
                    surface=True).grid(
            row=0, column=1, padx=(self.px(8), 0))
        K["Button"](row, P, "删除", command=self.on_del_phrase, kind="quiet",
                    font_size=self.px(12), height=self.px(34), radius=self.px(max(6, self.spec["btn_radius"] - 1)),
                    surface=True).grid(
            row=0, column=2, padx=(self.px(6), 0))

    def _build_log_card(self, body, K, P):
        tk = self.tk
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        head = tk.Frame(body, bg=P["surface"])
        head.grid(row=0, column=0, sticky="ew", pady=(0, self.px(10)))
        head.columnconfigure(1, weight=1)
        tk.Label(head, text="运行日志", bg=P["surface"], fg=P["text"],
                 font=(K["UI"], -self.px(14), "bold")).grid(row=0, column=0, sticky="w")
        K["Button"](head, P, "清空", command=self.on_clear_log, kind="quiet",
                    font_size=self.px(11), height=self.px(28), radius=self.px(8),
                    min_width=self.px(58), surface=True).grid(row=0, column=2, sticky="e")

        well = K["Well"](body, P, radius=self.px(10), pad=self.px(0))
        well.grid(row=1, column=0, sticky="nsew")
        well.body.rowconfigure(0, weight=1)
        well.body.columnconfigure(0, weight=1)
        self.log_text = tk.Text(well.body, wrap="word", state="disabled", bd=0,
                                height=6, width=20,
                                highlightthickness=0, relief="flat", padx=self.px(12),
                                pady=self.px(10), bg=P["surface_alt"], fg=P["text"],
                                font=(K["MONO"], -self.px(12)), spacing3=self.px(3))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("dim", foreground=P["faint"])
        self.log_text.tag_configure("lvlinfo", foreground=P["muted"])
        self.log_text.tag_configure("lvlwarning", foreground=P["warn"])
        self.log_text.tag_configure("lvlerror", foreground=P["bad"])
        self.log_text.tag_configure("accent", foreground=P["accent"])
        self.log_text.tag_configure("ok", foreground=P["ok"])
        self.log_text.tag_configure("bad", foreground=P["bad"])
        for line in self.log_lines:
            self._insert_log(line)

    # -- 日志与状态 --------------------------------------------------
    def _attach_logging(self):
        self.handler = QueueHandler(self.events)
        self.handler.setFormatter(logging.Formatter(
            "%(asctime)s|%(levelname)s|%(message)s", "%H:%M:%S"))
        self.handler.setLevel(logging.INFO)
        logging.getLogger("300-shout").addHandler(self.handler)

    def mainloop(self):
        self.root.mainloop()

    def calibrated(self) -> bool:
        try:
            return not self.main.validate_config(self.cfg)
        except Exception:  # noqa: BLE001
            return False

    def refresh(self):
        label, guide, level = status_brief(calibrated=self.calibrated(),
                                           running=self.session.running,
                                           ready=self.scores["ready"],
                                           threshold=self.scores["threshold"],
                                           locked=self.scores["locked"])
        if getattr(self, "pill", None) is not None:
            self.pill.set(label, level)
            self.guide_var.set(guide)

        th = self.scores["threshold"]
        self.metric_ready.set(self.scores["ready"], th)
        self.metric_cd.set(self.scores["cd"], 0.5 if th is None else th)

        detect = self.cfg.get("detect") or {}
        self.meta_var.set(
            "阈值 %s · 确认 %s 帧 · 触发后锁 %ss"
            % (fmt_score(th if th is not None else 0.8),
               detect.get("confirm_frames", 2), detect.get("lock_seconds", 3.0)))

        self.count_var.set(f"{len(self.book)} 条")
        c = self.counters
        self.foot_var.set(f"帧 {c['frames']} · 触发 {c['triggers']} · 已喊 {c['sent']} · "
                          f"拦下 {c['blocked']} · 失败 {c['errors']}")
        self.hint_var.set(f"配置文件：{self.config_path}　·　F8 暂停/继续，F9 退出，"
                          f"关窗即退")

        running = self.session.running
        self.btn_start.set_enabled(not running)
        self.btn_stop.set_enabled(running)
        for b in (self.btn_calib, self.btn_check, self.btn_test):
            b.set_enabled(not running)

        if not self.listbox_matches():
            self.listbox.delete(0, "end")
            for p in self.book.as_list():
                self.listbox.insert("end", p)

    def listbox_matches(self) -> bool:
        return list(self.listbox.get(0, "end")) == self.book.as_list()

    def append_log(self, line: str):
        self.log_lines.append(line)
        if len(self.log_lines) > self.MAX_LOG_LINES:
            self.log_lines = self.log_lines[-self.MAX_LOG_LINES:]
            text = getattr(self, "log_text", None)
            if text is not None:
                text.configure(state="normal")
                text.delete("1.0", "2.0")
                text.configure(state="disabled")
        self._insert_log(line)

    def _insert_log(self, line: str):
        text = getattr(self, "log_text", None)
        if text is None:
            return
        text.configure(state="normal")
        text.mark_set("insert", "end-1c")
        parts = line.split("|", 2)
        if len(parts) == 3 and len(parts[0]) == 8 and parts[0][2] == ":":
            ts, lvl, msg = parts
            text.insert("end", ts + "  ", "dim")
            text.insert("end", f"{lvl}  ", "lvl" + lvl.lower())
            text.insert("end", msg + "\n")
        elif line.startswith("→"):
            text.insert("end", line + "\n", "accent")
        elif line.startswith("!!"):
            text.insert("end", line + "\n", "bad")
        else:
            text.insert("end", line + "\n")
        text.see("end")
        text.configure(state="disabled")

    def on_clear_log(self):
        self.log_lines = []
        if getattr(self, "log_text", None) is not None:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")

    def _build_style_menu(self):
        """外观菜单单独建好返回（便于测试：菜单项顺序必须与 STYLES 一致）。"""
        self._style_var.set(self.style)
        menu = self.tk.Menu(self.root, tearoff=0)
        for key, spec in STYLES.items():
            menu.add_radiobutton(label=f"{spec['label']}   ·   {spec['hint']}", value=key,
                                 variable=self._style_var,
                                 command=lambda k=key: self.set_style(k))
        return menu

    def _open_style_menu(self):
        """点「外观」弹系统菜单：四套风格单选，选完立刻重建窗口并记进配置。"""
        menu = self._build_style_menu()
        x = self.btn_style.winfo_rootx()
        y = self.btn_style.winfo_rooty() + self.btn_style.winfo_height() + self.px(2)
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def set_style(self, key):
        key = resolve_style(key)
        if key == self.style:
            return
        self.style = key
        if save_ui_style(self.config_path, key):
            log.debug("外观已写入配置：%s", key)
        self._build()
        self.refresh()
        self.append_log(f"外观换成了「{style_label(key)}」。")

    # -- 事件泵 ----------------------------------------------------
    def _pump(self):
        dirty = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                dirty = True
                if kind == "log":
                    self.append_log(str(payload))
                elif kind == "tick":
                    self.counters.update({k: payload.get(k, self.counters[k])
                                          for k in self.counters})
                    self.scores.update({"ready": payload.get("ready_score"),
                                        "cd": payload.get("cd_score"),
                                        "threshold": payload.get("threshold"),
                                        "locked": bool(payload.get("locked"))})
                elif kind == "trigger":
                    if payload.get("allowed"):
                        verb = "本应喊" if payload.get("dry") else "已喊"
                        self.append_log(f"→ {verb}：{payload.get('text')}")
                    else:
                        self.append_log(f"→ 前台不是游戏（{payload.get('title')!r}），这次不喊")
                elif kind == "error":
                    self.append_log(f"!! {payload.get('text')}")
                elif kind == "calibrated":
                    self.reload_config()
        except queue.Empty:
            pass
        if dirty:
            self.refresh()
        self.root.after(120, self._pump)

    def reload_config(self):
        self.cfg = self.main.load_config(self.config_path)
        self.book = PhraseBook(self.cfg.get("phrases") or [])
        self.refresh()

    # -- 按钮动作 --------------------------------------------------
    def on_start(self):
        if self.session.running:
            return
        if not self.calibrated():
            self.append_log("配置还不完整，先标定大招图标。")
            from tkinter import messagebox
            if messagebox.askyesno("还没标定", "现在打开标定？框选游戏里的大招图标。",
                                   parent=self.root):
                self.on_calibrate()
            return
        self.session.start(self._on_event)
        self.append_log("开始监控（F8 暂停/继续，F9 退出）。记得让游戏在最前面，"
                        "独占全屏抓不到画面就切无边框窗口。")
        self.refresh()

    def on_stop(self):
        if self.session.stop():
            self.append_log("已停止监控。")
        self.refresh()

    def on_calibrate(self):
        self._run_tool(self._calibrate_worker, "--calibrate")

    def on_calibrate_check(self):
        self._run_tool(self._calibrate_worker, "--calibrate-check")

    def _run_tool(self, target, arg):
        if self.session.running:
            self.append_log("先停止监控再标定。")
            return
        threading.Thread(target=target, args=(arg,), daemon=True).start()

    def _calibrate_worker(self, arg):
        try:
            rc = self.main.main([arg, "--config", self.config_path])
        except SystemExit as exc:
            rc = exc.code
        except Exception as exc:  # noqa: BLE001
            log.exception("标定失败：%s", exc)
            rc = 1
        self.events.put(("log", f"标定工具结束（code={rc}）。"))
        self.events.put(("calibrated", {}))

    def on_test_send(self):
        from tkinter import simpledialog
        text = simpledialog.askstring("发送测试", "要发到游戏里的文本：",
                                      initialvalue="测试一下", parent=self.root)
        if not text:
            return
        threading.Thread(target=self._test_send_worker, args=(text,), daemon=True).start()

    def _test_send_worker(self, text):
        try:
            self.main.main(["--test-send", text, "--config", self.config_path])
        except SystemExit:
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("发送测试失败：%s", exc)

    def on_open_config(self):
        try:
            os.startfile(self.config_path)  # noqa: S606 用户点按钮才打开
        except (OSError, AttributeError) as exc:
            self.append_log(f"打开配置失败：{exc}")

    def on_add_phrase(self):
        ok, msg = self.book.add(self.phrase_var.get())
        if ok:
            self.phrase_var.set("")
            save_phrases(self.config_path, self.book.as_list())
        else:
            self.append_log(f"没加上：{msg}")
        self.refresh()

    def on_del_phrase(self):
        sel = list(self.listbox.curselection())
        if not sel:
            self.append_log("先在列表里选一条要删的。")
            return
        gone = self.book.remove(int(sel[0]))
        if gone:
            save_phrases(self.config_path, self.book.as_list())
            self.append_log(f"删掉了：{gone}")
        self.refresh()

    # -- 工作线程事件 ----------------------------------------------
    def _on_event(self, kind, payload):
        try:
            self.events.put_nowait((kind, dict(payload or {})))
        except Exception:  # noqa: BLE001 事件丢了也不能影响主循环
            pass

    def _worker(self, stop_event, on_event):
        cfg = self.main.load_config(self.config_path)
        problems = self.main.validate_config(cfg)
        if problems:
            if on_event:
                on_event("error", {"text": "配置问题：" + "；".join(problems)})
            return

        def relay(kind, payload):
            if on_event is None:
                return
            data = dict(payload or {})
            if kind == "tick" and det is not None:
                data.update(det.status())
            on_event(kind, data)

        cap = det = None
        cap, det, send_fn, guard_fn, hotkeys = self.main.build_runtime(cfg)
        if getattr(cap, "backend", "none") == "none":
            relay("error", {"text": "没有可用的捕获后端（dxcam 需 Windows）。"})
            return
        log.info("面板启动：捕获后端=%s，台词 %d 条，守卫=%s，热键=%s",
                 cap.backend, len(cfg.get("phrases") or []),
                 "开" if guard_fn else "关",
                 ",".join(hotkeys.keys) if hotkeys else "关")
        self.main.run(cfg, cap, det, send_fn, max_empty_frames=600,
                      guard=guard_fn, hotkeys=hotkeys,
                      stop_event=stop_event, on_event=relay,
                      stats_seconds=max(0.0, float(cfg.get("stats_interval") or 60.0)))

    # -- 关闭 ------------------------------------------------------
    def shutdown(self):
        self.session.stop()
        if self.handler is not None:
            logging.getLogger("300-shout").removeHandler(self.handler)
            self.handler = None

    def on_close(self):
        self.shutdown()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def launch(config_path: str) -> int:
    """打开面板并阻塞到窗口关闭。返回码 0=正常关闭，2=没有 tkinter。"""
    try:
        import tkinter  # noqa: F401 只为探测是否可用
    except ImportError as exc:
        log.error("没有 tkinter（%s），无法开面板；用命令行：main.py --help", exc)
        return 2
    try:
        panel = Panel(tkinter, config_path)
    except Exception as exc:  # noqa: BLE001 例如无显示环境
        log.exception("面板启动失败：%s", exc)
        log.error("可以改用命令行：main.py --no-ui --dry-run --stats 5")
        return 2
    try:
        panel.mainloop()
    finally:
        panel.shutdown()
    return 0
