"""300 Shout 入口：捕获 → 检测 → 触发 → 发送，主循环 + 日志。

用法::

    python main.py                # 正常运行（需先 calibrate.py 标定）
    python main.py --self-test    # 离线自检：不接触显示器/键盘，跑通整条链路
    python main.py --frames 200   # 处理 200 帧后退出（调试）
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

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(ROOT, "config.json")
REFS_DIR = os.path.join(ROOT, "refs")


# ---------------------------------------------------------------- 配置
def load_config(path: str = DEFAULT_CONFIG) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def validate_config(cfg: dict) -> list:
    """返回问题列表（空 = 通过）。"""
    problems = []
    roi = cfg.get("roi") or {}
    if not (roi.get("right") and roi.get("bottom")):
        problems.append("roi 未标定（right/bottom 为 0），请先运行: python calibrate.py")
    if not cfg.get("phrases"):
        problems.append("phrases 台词池为空")
    return problems


def crop_roi(frame, roi: dict):
    return frame[roi["top"]:roi["bottom"], roi["left"]:roi["right"]]


def load_refs(refs_dir: str = REFS_DIR):
    """读取标定生成的基准图。返回 (ready_img, cd_img|None)。"""
    import cv2
    ready_path = os.path.join(refs_dir, "ready.png")
    cd_path = os.path.join(refs_dir, "cd.png")
    if not os.path.exists(ready_path):
        raise FileNotFoundError(
            f"缺少基准图 {ready_path}，请先运行: python calibrate.py")
    ready_img = cv2.imread(ready_path, cv2.IMREAD_COLOR)
    cd_img = cv2.imread(cd_path, cv2.IMREAD_COLOR) if os.path.exists(cd_path) else None
    return ready_img, cd_img


# ---------------------------------------------------------------- 主循环
def run(cfg: dict, capture, detector, send_fn, *, clock=time.monotonic,
        sleeper=time.sleep, max_frames=None, max_empty_frames=0, rng=None) -> int:
    """主循环：抓帧 → 裁 ROI → 检测 → 触发喊话。返回喊话次数。

    ``max_frames``：处理多少张有效帧后退出（None = 常驻）。
    ``max_empty_frames``：连续抓不到帧多少张后判为异常并退出（0 = 不限）。
    抓不到帧通常是 dxcam 初始化（1~2s）或游戏/显示异常，故默认只限制上限、
    不轻易退出；真实运行由 main() 给一个宽松阈值兜底。
    """
    rng = rng or random.Random()
    min_i = float(cfg.get("min_interval", 1.5))
    max_i = float(cfg.get("max_interval", 3.0))
    phrases = cfg.get("phrases") or ["（无台词）"]
    roi = cfg["roi"]
    count = 0
    frames = 0
    empty = 0
    try:
        while max_frames is None or frames < max_frames:
            frame = capture.grab()
            if frame is None:
                empty += 1
                if max_empty_frames and empty >= max_empty_frames:
                    log.error("连续 %d 帧抓不到画面，判定捕获异常，退出。", empty)
                    raise SystemExit(3)
                sleeper(0.05)  # 未就绪帧，跳过
                continue
            empty = 0
            frames += 1
            icon = crop_roi(frame, roi)
            if detector.update(icon, clock()):
                text = rng.choice(phrases)
                send_fn(text)
                count += 1
                sleeper(rng.uniform(min_i, max_i))  # 随机间隔，防机械节奏
    except KeyboardInterrupt:
        log.info("收到中断，退出。")
    finally:
        rel = getattr(capture, "release", None)
        if callable(rel):
            rel()
    log.info("本轮结束：处理 %d 帧，喊话 %d 次", frames, count)
    return count


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
        )
    return _send


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


def self_test() -> int:
    """离线自检：不接触显示器/键盘，串起 配置→ROI→检测→发送→日志 整条链路。"""
    import numpy as np
    from detect import CooldownDetector

    setup_logging(log_dir=os.path.join(ROOT, "logs"), level=logging.INFO)
    log.info("SELF-TEST 开始")

    cfg = load_config(DEFAULT_CONFIG)
    cfg["roi"] = {"left": 0, "top": 0, "right": 48, "bottom": 48}
    cfg["detect"]["lock_seconds"] = 0.0

    def tex(seed, h=48, w=48):
        r = np.random.default_rng(seed)
        return r.integers(0, 255, (h, w, 3), dtype=np.uint8)

    ready_ref, cd_ref = tex(1), tex(2)
    frames = [ready_ref, ready_ref, cd_ref, cd_ref, ready_ref, cd_ref]

    det = CooldownDetector(ready_ref, cd_ref,
                           threshold=cfg["detect"]["threshold"],
                           lock_seconds=cfg["detect"]["lock_seconds"])
    sent = []
    tick = [0.0]

    def clock():
        tick[0] += 0.2
        return tick[0]

    n = run(cfg, _FakeCapture(frames), det, sent.append,
            clock=clock, sleeper=lambda *_: None, rng=random.Random(0),
            max_frames=len(frames))

    if n < 1:
        log.error("SELF-TEST 失败：未检测到任何触发（期望 >=1）")
        return 1
    log.info("SELF-TEST OK：触发 %d 次，已发送 %r", n, sent)
    print(f"SELF-TEST OK: 触发 {n} 次, 台词={sent}")
    return 0


# ---------------------------------------------------------------- 入口
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="300英雄 开大自动公屏喊话（图像识别）")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--log-level", default="INFO", help="日志级别")
    ap.add_argument("--self-test", action="store_true", help="离线自检（不接触显示器/键盘）")
    ap.add_argument("--frames", type=int, default=None, help="最多处理 N 帧后退出")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    setup_logging(log_dir=os.path.join(ROOT, "logs"),
                  level=getattr(logging, args.log_level.upper(), logging.INFO))
    log.info("300 Shout 启动")

    cfg = load_config(args.config)
    problems = validate_config(cfg)
    for p in problems:
        log.warning("配置问题: %s", p)
    if problems:
        log.error("配置不完整，请先标定（python calibrate.py）后再运行。")
        return 2

    from capture import ScreenCapture
    from detect import CooldownDetector

    ready_ref, cd_ref = load_refs()
    cap_cfg = cfg.get("capture") or {}
    cap = ScreenCapture(fps=cap_cfg.get("fps", 10),
                        output_idx=cap_cfg.get("output_idx", 0),
                        prefer=cap_cfg.get("mode", "dxcam"))
    if cap.backend == "none":
        log.error("没有可用的捕获后端（dxcam 需 Windows；mss 需窗口化/有显示环境）。")
        return 3

    det = CooldownDetector(ready_ref, cd_ref,
                           threshold=cfg["detect"]["threshold"],
                           lock_seconds=cfg["detect"]["lock_seconds"])
    log.info("就绪：捕获后端=%s，台词池 %d 条", cap.backend, len(cfg.get("phrases") or []))
    run(cfg, cap, det, build_sender(cfg), max_frames=args.frames,
        max_empty_frames=600)  # ~60s @10fps 抓不到画面即判异常
    return 0


if __name__ == "__main__":
    sys.exit(main())
