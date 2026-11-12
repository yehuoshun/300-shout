"""300 Shout 入口：捕获 → 检测 → 触发 → 发送，主循环 + 日志。

用法::

    python main.py                    # 正常运行（需先 calibrate.py 标定）
    python main.py --dry-run --stats  # 演练：只检测不发送，实时看相似度（真机首验用）
    python main.py --self-test        # 离线自检：不接触显示器/键盘，跑通整条链路
    python main.py --list-windows     # 列出窗口标题，用于配置前台窗口白名单
    python main.py --frames 200       # 处理 200 帧后退出（调试）

运行中热键：F8 暂停/继续，F9 退出（可在 config.json 的 hotkeys 里改）。
"""
import argparse
import json
import logging
import os
import random
import sys
import time

from logsetup import setup_logging

log = logging.getLogger("300-shout.main")

VERSION = "3.0.0"

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(ROOT, "config.json")
REFS_DIR = os.path.join(ROOT, "refs")

# 内置默认值：config.json 里缺的键用这里的补，老配置文件不必手工升级
DEFAULT_CFG = {
    "capture": {"mode": "dxcam", "output_idx": 0, "fps": 10},
    "roi": {"left": 0, "top": 0, "right": 0, "bottom": 0},
    "detect": {"threshold": 0.8, "lock_seconds": 3.0, "confirm_frames": 2},
    "chat": {
        "open_key": "enter",
        "channel_keys": [],
        "type_delay": 0.08,
        "enter_delay": 0.15,
        "input_method": "unicode",
        "require_foreground": True,
        "foreground_titles": ["300英雄"],
    },
    "phrases": [],
    "min_interval": 1.5,
    "max_interval": 3.0,
    "hotkeys": {"pause": "f8", "quit": "f9"},
    "stats_interval": 60,
}


# ---------------------------------------------------------------- 配置
def deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base，返回新 dict（不改动入参）。"""
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path: str = DEFAULT_CONFIG) -> dict:
    with open(path, encoding="utf-8") as f:
        user = json.load(f)
    return deep_merge(DEFAULT_CFG, user)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def validate_config(cfg: dict) -> list:
    """返回**阻断性**问题列表（空 = 通过）。非阻断提醒见 config_warnings。"""
    problems = []
    roi = cfg.get("roi") or {}
    left, top = _as_int(roi.get("left"), 0), _as_int(roi.get("top"), 0)
    right, bottom = _as_int(roi.get("right"), 0), _as_int(roi.get("bottom"), 0)
    if right <= left or bottom <= top:
        problems.append("roi 未标定或无效（right/bottom 必须大于 left/top），"
                        "请先运行: python calibrate.py")
    elif left < 0 or top < 0:
        problems.append(f"roi 不能为负: {roi!r}")

    det = cfg.get("detect") or {}
    threshold = _as_float(det.get("threshold"), -1.0)
    if not 0.0 < threshold <= 1.0:
        problems.append(f"detect.threshold 必须在 (0, 1] 之间，当前 {det.get('threshold')!r}")
    if _as_float(det.get("lock_seconds"), -1.0) < 0:
        problems.append("detect.lock_seconds 不能为负")
    if _as_int(det.get("confirm_frames"), 2) < 1:
        problems.append("detect.confirm_frames 必须 >= 1")

    if _as_float(cfg.get("min_interval"), 1.5) < 0 or _as_float(cfg.get("max_interval"), 3.0) < 0:
        problems.append("min_interval / max_interval 不能为负")

    cap = cfg.get("capture") or {}
    if _as_int(cap.get("fps"), 0) <= 0:
        problems.append("capture.fps 必须 > 0")

    phrases = cfg.get("phrases") or []
    if not isinstance(phrases, list):
        problems.append("phrases 必须是字符串数组")
    elif not [p for p in phrases if str(p).strip()]:
        problems.append("phrases 台词池为空")

    chat = cfg.get("chat") or {}
    if not isinstance(chat.get("channel_keys") or [], list):
        problems.append("chat.channel_keys 必须是字符串数组，如 [] 或 [\"tab\"]")
    return problems


def config_warnings(cfg: dict) -> list:
    """非阻断提醒：跑得起来但值得看一眼的配置。"""
    warns = []
    chat = cfg.get("chat") or {}
    titles = [t for t in (chat.get("foreground_titles") or []) if str(t).strip()]
    if chat.get("require_foreground", True) and not titles:
        warns.append("chat.require_foreground 开着但 foreground_titles 为空，等同不校验；"
                     "用 python main.py --list-windows 查游戏窗口标题")
    method = str(chat.get("input_method", "unicode")).lower()
    if method not in ("unicode", "clipboard"):
        warns.append(f"chat.input_method={method!r} 未识别，将按 unicode 处理")
    if _as_float(cfg.get("min_interval"), 1.5) > _as_float(cfg.get("max_interval"), 3.0):
        warns.append("min_interval 大于 max_interval，内部会自动交换")
    if len([p for p in (cfg.get("phrases") or []) if str(p).strip()]) == 1:
        warns.append("台词池只有 1 条，连续喊话会显得很机械")
    return warns


def crop_roi(frame, roi: dict):
    """按 ROI 裁剪；越界自动夹到画面范围内（改分辨率后不会裁出空图）。"""
    h, w = frame.shape[:2]
    left = min(max(_as_int(roi.get("left"), 0), 0), w)
    right = min(max(_as_int(roi.get("right"), 0), 0), w)
    top = min(max(_as_int(roi.get("top"), 0), 0), h)
    bottom = min(max(_as_int(roi.get("bottom"), 0), 0), h)
    return frame[top:bottom, left:right]


def load_refs(refs_dir: str = REFS_DIR):
    """读取标定生成的基准图。返回 (ready_img, cd_img|None)。"""
    import cv2
    ready_path = os.path.join(refs_dir, "ready.png")
    cd_path = os.path.join(refs_dir, "cd.png")
    if not os.path.exists(ready_path):
        raise FileNotFoundError(
            f"缺少基准图 {ready_path}，请先运行: python calibrate.py")
    ready_img = cv2.imread(ready_path, cv2.IMREAD_COLOR)
    if ready_img is None:
        raise ValueError(f"基准图读不出来（文件损坏？）: {ready_path}")
    cd_img = cv2.imread(cd_path, cv2.IMREAD_COLOR) if os.path.exists(cd_path) else None
    return ready_img, cd_img


def pick_phrase(rng, phrases, last=None) -> str:
    """随机取一条台词，尽量不与上一条重复（台词池 1 条时只能重复）。"""
    pool = [str(p) for p in phrases if str(p).strip()] or ["（无台词）"]
    if len(pool) == 1:
        return pool[0]
    candidates = [p for p in pool if p != last] or pool
    return rng.choice(candidates)


# ---------------------------------------------------------------- 主循环
def _ask_guard(guard):
    """调用前台守卫，返回 (allowed, title)。"""
    if guard is None:
        return True, None
    result = guard()
    if isinstance(result, tuple):
        allowed = bool(result[0])
        title = result[1] if len(result) > 1 else None
        return allowed, title
    return bool(result), None


def _score_suffix(detector) -> str:
    """把检测器的最近打分拼进日志，方便真机调阈值。"""
    status_fn = getattr(detector, "status", None)
    if not callable(status_fn):
        return ""
    try:
        st = status_fn()
    except Exception:  # noqa: BLE001 - 打日志不能反过来搞崩主循环
        return ""
    parts = []
    if st.get("ready_score") is not None:
        parts.append("ready=%.3f" % st["ready_score"])
    if st.get("cd_score") is not None:
        parts.append("cd=%.3f" % st["cd_score"])
    if "is_ready" in st:
        parts.append("状态=%s" % ("可用" if st["is_ready"] else "CD"))
    return "（" + "，".join(parts) + "）" if parts else ""


def run(cfg: dict, capture, detector, send_fn, *, clock=time.monotonic,
        sleeper=time.sleep, max_frames=None, max_empty_frames=0, rng=None,
        guard=None, hotkeys=None, dry_run=False, stats_seconds: float = 0.0) -> int:
    """主循环：抓帧 → 裁 ROI → 检测 → 触发喊话。返回「触发并放行」的次数。

    ``max_frames``：处理多少张有效帧后退出（None = 常驻）。
    ``max_empty_frames``：连续抓不到帧多少张后判为异常并退出（0 = 不限）。
    抓不到帧通常是 dxcam 初始化（1~2s）或游戏/显示异常，故默认只限制上限、
    不轻易退出；真实运行由 main() 给一个宽松阈值兜底。

    ``guard``：可选，返回 (是否允许发送, 前台窗口标题)。游戏不在前台时不喊。
    ``hotkeys``：可选，带 ``poll()`` 的热键观察者（F8 暂停 / F9 退出）。
    ``dry_run``：只记录「本应发送」，绝不碰键盘。
    """
    rng = rng or random.Random()
    min_i = float(cfg.get("min_interval", 1.5))
    max_i = float(cfg.get("max_interval", 3.0))
    if max_i < min_i:
        min_i, max_i = max_i, min_i
    phrases = [str(p) for p in (cfg.get("phrases") or []) if str(p).strip()] or ["（无台词）"]
    roi = cfg["roi"]
    hk = cfg.get("hotkeys") or {}
    pause_key = str(hk.get("pause", "f8")).strip().lower()
    quit_key = str(hk.get("quit", "f9")).strip().lower()

    frames = empty = triggers = sent = blocked = 0
    fired = 0
    paused = False
    stop = False
    last_phrase = None
    next_stats = clock() + stats_seconds if stats_seconds else None
    fps = max(1, _as_int((cfg.get("capture") or {}).get("fps"), 10))
    frame_interval = 1.0 / fps  # 抓帧是非阻塞的，必须自己补足节奏，否则空转吃满 CPU

    def _pace(started: float) -> None:
        wait = frame_interval - (clock() - started)
        if wait > 0:
            sleeper(wait)

    try:
        while not stop and (max_frames is None or frames < max_frames):
            tick = clock()
            if hotkeys is not None:
                for key in hotkeys.poll():
                    if key == quit_key:
                        log.info("热键 %s：退出。", key.upper())
                        stop = True
                    elif key == pause_key:
                        paused = not paused
                        log.info("热键 %s：%s。", key.upper(), "已暂停" if paused else "已继续")
                if stop:
                    break
            if paused:
                sleeper(0.1)
                continue

            frame = capture.grab()
            if frame is None:
                empty += 1
                if max_empty_frames and empty >= max_empty_frames:
                    log.error("连续 %d 帧抓不到画面，判定捕获异常，退出。", empty)
                    raise SystemExit(3)
                _pace(tick)  # 没有新帧也按节奏走，别空转
                continue
            empty = 0
            frames += 1
            icon = crop_roi(frame, roi)
            if detector.update(icon, clock()):
                triggers += 1
                allowed, title = _ask_guard(guard)
                if not allowed:
                    blocked += 1
                    log.warning("检测到大招，但前台窗口 %r 不在白名单 %s，本次不发送"
                                "（游戏需在前台）%s。", title,
                                (cfg.get("chat") or {}).get("foreground_titles"),
                                "；DRY-RUN 下本应发送" if dry_run else "")
                else:
                    fired += 1
                    text = pick_phrase(rng, phrases, last_phrase)
                    last_phrase = text
                    if dry_run or send_fn is None:
                        log.info("[DRY-RUN] 检测到大招，本应发送：%s", text)
                    else:
                        send_fn(text)
                        sent += 1
                    sleeper(rng.uniform(min_i, max_i))  # 随机间隔，防机械节奏

            if next_stats is not None and clock() >= next_stats:
                next_stats = clock() + stats_seconds
                log.info("统计：帧 %d，触发 %d，发送 %d，拦下 %d，空帧 %d%s",
                         frames, triggers, sent, blocked, empty, _score_suffix(detector))

            _pace(tick)  # grab 是非阻塞的，这里补足帧间隔，避免空转吃满 CPU
    except KeyboardInterrupt:
        log.info("收到中断，退出。")
    finally:
        rel = getattr(capture, "release", None)
        if callable(rel):
            rel()
    log.info("本轮结束：处理 %d 帧，触发 %d 次，发送 %d 次，前台拦下 %d 次%s",
             frames, triggers, sent, blocked, _score_suffix(detector))
    return fired


# ---------------------------------------------------------------- 组装
def build_sender(cfg: dict):
    """按配置组装发送回调。"""
    import sender as sender_mod
    c = cfg.get("chat") or {}

    def _send(text: str) -> None:
        sender_mod.send_chat(
            text,
            open_key=c.get("open_key", "enter"),
            channel_keys=c.get("channel_keys") or [],
            type_delay=c.get("type_delay", 0.08),
            enter_delay=c.get("enter_delay", 0.15),
            input_method=c.get("input_method", "unicode"),
        )
    return _send


def build_guard(cfg: dict):
    """前台窗口守卫：游戏不在前台就不发。配置为空时返回 None（不校验）。"""
    chat = cfg.get("chat") or {}
    if not chat.get("require_foreground", True):
        return None
    titles = [str(t).strip() for t in (chat.get("foreground_titles") or []) if str(t).strip()]
    if not titles:
        return None
    import guard as guard_mod

    def _guard():
        title = guard_mod.foreground_title()
        return guard_mod.title_matches(title, titles), title
    return _guard


def build_hotkeys(cfg: dict):
    """F8/F9 全局热键观察者；非 Windows 上 poll() 永远返回空。"""
    import guard as guard_mod
    hk = cfg.get("hotkeys") or {}
    keys = [str(k).strip().lower() for k in (hk.get("pause"), hk.get("quit")) if str(k).strip()]
    if not keys:
        return None
    return guard_mod.HotkeyWatcher(keys)


# ---------------------------------------------------------------- 自检
class _FakeCapture:
    """按序吐出预置帧，末尾返回 None。"""
    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0

    def grab(self):
        if self._i >= len(self._frames):
            return None
        f = self._frames[self._i]
        self._i += 1
        return f

    def release(self):  # noqa: D401
        return None


class _FakeHotkeys:
    """固定顺序回放热键按下事件（用于离线自检）。"""

    def __init__(self, script=None):
        self._script = list(script or [])

    def poll(self):
        return [self._script.pop(0)] if self._script else []


def self_test() -> int:
    """离线自检：不接触显示器/键盘，串起 配置→ROI→检测→发送→日志 整条链路。"""
    import numpy as np
    from detect import CooldownDetector

    setup_logging(log_dir=os.path.join(ROOT, "logs"), level=logging.INFO)
    log.info("SELF-TEST 开始")

    cfg = load_config(DEFAULT_CONFIG)
    cfg["roi"] = {"left": 0, "top": 0, "right": 48, "bottom": 48}
    cfg["detect"]["lock_seconds"] = 0.0
    cfg["detect"]["confirm_frames"] = 2

    def tex(seed, h=48, w=48):
        r = np.random.default_rng(seed)
        return r.integers(0, 255, (h, w, 3), dtype=np.uint8)

    ready_ref, cd_ref = tex(1), tex(2)
    # 可用×2 → CD×2（构成一次下降沿）→ 可用×2 → CD（只 1 帧，应被多帧确认挡掉）
    frames = [ready_ref, ready_ref, cd_ref, cd_ref, ready_ref, ready_ref, cd_ref]
    det = CooldownDetector(ready_ref, cd_ref,
                           threshold=cfg["detect"]["threshold"],
                           lock_seconds=cfg["detect"]["lock_seconds"],
                           confirm_frames=cfg["detect"]["confirm_frames"])
    sent = []
    tick = [0.0]

    def clock():
        tick[0] += 0.2
        return tick[0]

    n = run(cfg, _FakeCapture(frames), det, sent.append,
            clock=clock, sleeper=lambda *_: None, rng=random.Random(0),
            max_frames=len(frames))
    if n < 1 or not sent:
        log.error("SELF-TEST 失败：未检测到任何触发（期望 >=1），实际 %d", n)
        return 1

    # 分支二：前台窗口不匹配 → 检测到也不许发（guard 必须真的被问到）
    det2 = CooldownDetector(ready_ref, cd_ref, threshold=cfg["detect"]["threshold"],
                           lock_seconds=0.0, confirm_frames=2)
    guarded = []
    asked = {"n": 0}

    def deny():
        asked["n"] += 1
        return False, "记事本"

    fired_n = run(cfg, _FakeCapture(frames), det2, guarded.append,
                  clock=clock, sleeper=lambda *_: None, rng=random.Random(0),
                  max_frames=len(frames), guard=deny)
    if guarded or fired_n != 0 or asked["n"] < 1:
        log.error("SELF-TEST 失败：前台守卫未拦住发送（守卫被问 %d 次，放行 %d 次，sent=%r）",
                  asked["n"], fired_n, guarded)
        return 1

    # 分支三：F9 应立即退出（若热键失效，会跑完帧数并触发，fired>=1 即暴露）
    det3 = CooldownDetector(ready_ref, cd_ref, threshold=cfg["detect"]["threshold"],
                           lock_seconds=0.0, confirm_frames=2)
    quit_n = run(cfg, _FakeCapture(frames * 3), det3, [].append,
                 clock=clock, sleeper=lambda *_: None, rng=random.Random(0),
                 max_frames=len(frames) * 3, hotkeys=_FakeHotkeys(["f9"]), dry_run=True)
    if quit_n != 0:
        log.error("SELF-TEST 失败：F9 热键没有立刻退出（fired=%d）", quit_n)
        return 1

    log.info("SELF-TEST OK：触发 %d 次，已发送 %r", n, sent)
    print(f"SELF-TEST OK: 触发 {n} 次, 台词={sent}")
    return 0


# ---------------------------------------------------------------- 入口
def _force_utf8_stdio() -> None:
    """Windows 控制台默认用 ANSI 代码页，中文/emoji 会 UnicodeEncodeError。统一为 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _print_windows() -> int:
    import guard as guard_mod
    titles = guard_mod.list_windows()
    if not titles:
        print("（拿不到窗口列表：非 Windows，或当前桌面不可用）")
        return 1
    for t in sorted(titles):
        print(t)
    print(f"\n共 {len(titles)} 个窗口。把游戏窗口标题里的稳定片段填进 "
          f"config.json 的 chat.foreground_titles（如 \"300英雄\"）。")
    return 0


def main(argv=None) -> int:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(description="300英雄 开大自动公屏喊话（图像识别）")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--log-level", default="INFO", help="日志级别")
    ap.add_argument("--self-test", action="store_true", help="离线自检（不接触显示器/键盘）")
    ap.add_argument("--frames", type=int, default=None, help="最多处理 N 帧后退出")
    ap.add_argument("--dry-run", action="store_true",
                    help="演练模式：只检测不发送（真机首验用，安全）")
    ap.add_argument("--stats", type=float, nargs="?", const=60.0, default=None,
                    metavar="SEC", help="每 SEC 秒打印统计（含相似度分数），默认 60")
    ap.add_argument("--no-guard", action="store_true", help="关闭前台窗口校验")
    ap.add_argument("--no-hotkeys", action="store_true", help="关闭 F8/F9 全局热键")
    ap.add_argument("--list-windows", action="store_true", help="列出窗口标题（配置白名单用）")
    ap.add_argument("--version", action="version", version=f"300-shout {VERSION}")
    args = ap.parse_args(argv)

    if args.list_windows:
        return _print_windows()
    if args.self_test:
        return self_test()

    setup_logging(log_dir=os.path.join(ROOT, "logs"),
                  level=getattr(logging, args.log_level.upper(), logging.INFO))
    log.info("300 Shout %s 启动", VERSION)

    cfg = load_config(args.config)
    for w in config_warnings(cfg):
        log.warning("配置提醒: %s", w)
    problems = validate_config(cfg)
    for p in problems:
        log.error("配置问题: %s", p)
    if problems:
        log.error("配置检查未通过（%d 项），修好 config.json 再运行；"
                  "若提示 roi 未标定，先跑 python calibrate.py。", len(problems))
        return 2

    from capture import ScreenCapture
    from detect import CooldownDetector

    ready_ref, cd_ref = load_refs()
    det_cfg = cfg.get("detect") or {}
    cap_cfg = cfg.get("capture") or {}
    cap = ScreenCapture(fps=cap_cfg.get("fps", 10),
                        output_idx=cap_cfg.get("output_idx", 0),
                        prefer=cap_cfg.get("mode", "dxcam"))
    if cap.backend == "none":
        log.error("没有可用的捕获后端（dxcam 需 Windows；mss 需窗口化/有显示环境）。")
        return 3

    det = CooldownDetector(ready_ref, cd_ref,
                           threshold=float(det_cfg.get("threshold", 0.8)),
                           lock_seconds=float(det_cfg.get("lock_seconds", 3.0)),
                           confirm_frames=int(det_cfg.get("confirm_frames", 2)))
    guard_fn = None if args.no_guard else build_guard(cfg)
    hotkeys = None if args.no_hotkeys else build_hotkeys(cfg)
    send_fn = None if args.dry_run else build_sender(cfg)

    log.info("就绪：捕获后端=%s，台词池 %d 条，前台守卫=%s，热键=%s%s",
             cap.backend, len(cfg.get("phrases") or []),
             "开" if guard_fn else "关",
             ",".join(hotkeys.keys) if hotkeys else "关",
             "，DRY-RUN(不发送)" if args.dry_run else "")
    if args.stats is not None:
        stats_seconds = float(args.stats)
    else:
        stats_seconds = _as_float(cfg.get("stats_interval"), 60.0)
    run(cfg, cap, det, send_fn, max_frames=args.frames,
        max_empty_frames=600,  # ~60s @10fps 抓不到画面即判异常
        guard=guard_fn, hotkeys=hotkeys, dry_run=args.dry_run,
        stats_seconds=max(0.0, stats_seconds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
