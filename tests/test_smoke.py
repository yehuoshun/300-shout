"""冒烟测试：离线验证 检测逻辑 / 发送按键序列 / 配置 / 标定链路 / 启动自检。

不依赖游戏、不接触显示器或键盘；pydirectinput 通过 FakeKB 注入。
"""
import json
import os
import subprocess
import sys

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import calibrate as calib_mod  # noqa: E402
import main as main_mod        # noqa: E402
import sender as sender_mod    # noqa: E402
from detect import CooldownDetector  # noqa: E402


def tex(seed, h=48, w=48):
    r = np.random.default_rng(seed)
    return r.integers(0, 255, (h, w, 3), dtype=np.uint8)


class FakeKB:
    def __init__(self):
        self.calls = []

    def press(self, k):
        self.calls.append(("press", k))

    def write(self, t, interval=0):
        self.calls.append(("write", t))

    def keyDown(self, k):
        self.calls.append(("keyDown", k))

    def keyUp(self, k):
        self.calls.append(("keyUp", k))


# ------------------------------------------------------------ detect
def test_rising_edge_triggers_once():
    ref, cd = tex(1), tex(2)
    d = CooldownDetector(ref, cd, threshold=0.8, lock_seconds=3.0)
    assert d.update(ref, 0.0) is False
    assert d.update(cd, 0.5) is True    # 上升沿
    assert d.update(cd, 0.6) is False   # 防抖锁内
    assert d.update(cd, 4.0) is False   # 锁过期但无新上升沿


def test_no_false_trigger_when_start_in_cd():
    ref, cd = tex(1), tex(2)
    d = CooldownDetector(ref, cd)
    assert d.update(cd, 0.0) is False   # 启动首帧即 CD，不应误触发（旧 bug）


def test_score_uses_max_not_corner():
    """ROI 比基准大时，应取最佳匹配（max），而非左上角（旧 bug）。"""
    ref = tex(1, 48, 48)
    canvas = np.random.default_rng(9).integers(0, 255, (128, 128, 3), dtype=np.uint8)
    y, x = 40, 50
    canvas[y:y + 48, x:x + 48] = ref
    d = CooldownDetector(ref, None, threshold=0.9)
    assert d._score(canvas, ref) > 0.99


def test_size_mismatch_safe_no_crash():
    ref = tex(1, 48, 48)
    small = tex(3, 20, 20)
    d = CooldownDetector(ref, None)
    assert d.update(small, 0.0) is False  # 尺寸过小 -> 安全跳过，不崩


def test_none_frame_skipped():
    d = CooldownDetector(tex(1), None)
    assert d.update(None, 0.0) is False


# ------------------------------------------------------------ sender
def test_combo_key_uses_keydown_keyup():
    kb = FakeKB()
    sender_mod.send_chat("hi", channel_keys=["shift+enter"], enter_delay=0, kb=kb)
    assert ("keyDown", "shift") in kb.calls
    assert ("keyUp", "shift") in kb.calls
    kd = kb.calls.index(("keyDown", "shift"))
    ku = kb.calls.index(("keyUp", "shift"))
    press_enter = [i for i, c in enumerate(kb.calls) if c == ("press", "enter")]
    assert kd < ku
    assert any(kd < i < ku for i in press_enter)  # Enter 夹在 keyDown/keyUp 之间


def test_single_key_channel():
    kb = FakeKB()
    sender_mod.send_chat("hi", channel_keys=["tab"], enter_delay=0, kb=kb)
    assert ("press", "tab") in kb.calls
    assert ("write", "hi") in kb.calls


def test_plain_send_no_channel():
    kb = FakeKB()
    sender_mod.send_chat("hello", channel_keys=[], enter_delay=0, kb=kb)
    assert kb.calls == [("press", "enter"), ("write", "hello"), ("press", "enter")]


# ------------------------------------------------------------ main / config
def test_config_load_and_validate():
    cfg = main_mod.load_config()
    assert "roi" in cfg and "phrases" in cfg
    assert isinstance(main_mod.validate_config(cfg), list)


def test_crop_roi():
    frame = np.arange(10 * 10 * 3, dtype=np.uint8).reshape(10, 10, 3)
    sub = main_mod.crop_roi(frame, {"left": 2, "top": 3, "right": 6, "bottom": 8})
    assert sub.shape == (5, 4, 3)


# ------------------------------------------------------------ calibrate（离线）
def test_calibrate_offline_roundtrip(tmp_path):
    frame = np.random.default_rng(0).integers(0, 255, (200, 200, 3), dtype=np.uint8)
    img_path = tmp_path / "shot.png"
    cv2.imwrite(str(img_path), frame)
    ready = frame[10:74, 20:84]
    ready_path = tmp_path / "ready.png"
    cv2.imwrite(str(ready_path), ready)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(main_mod.load_config()), encoding="utf-8")
    refs = tmp_path / "refs"

    rc = calib_mod.main(["--config", str(cfg_path), "--image", str(img_path),
                         "--roi", "20,10,64,64", "--ready", str(ready_path),
                         "--refs-dir", str(refs)])
    assert rc == 0
    out = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert out["roi"] == {"left": 20, "top": 10, "right": 84, "bottom": 74}
    assert (refs / "ready.png").exists()


# ------------------------------------------------------------ 端到端启动自检
def test_self_test_subprocess():
    r = subprocess.run([sys.executable, "main.py", "--self-test"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELF-TEST OK" in r.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
