"""冒烟测试：离线验证 检测 / 发送 / 配置 / 前台守卫 / 热键 / 标定 / 启动自检。

不依赖游戏、不接触显示器或键盘；pydirectinput 通过 FakeKB 注入，
热键通过 state_fn 注入，剪贴板通过 setter 注入。
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
import guard as guard_mod      # noqa: E402
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
    d = CooldownDetector(ref, cd, threshold=0.8, lock_seconds=3.0, confirm_frames=1)
    assert d.update(ref, 0.0) is False
    assert d.update(cd, 0.5) is True    # 上升沿
    assert d.update(cd, 0.6) is False   # 防抖锁内
    assert d.update(cd, 4.0) is False   # 锁过期但无新上升沿


def test_no_false_trigger_when_start_in_cd():
    ref, cd = tex(1), tex(2)
    d = CooldownDetector(ref, cd, confirm_frames=1)
    assert d.update(cd, 0.0) is False   # 启动首帧即 CD，不应误触发


def test_multi_frame_confirm_rejects_single_glitch():
    """单帧掉下阈值（技能特效/粒子）不应触发；连续两帧才认。"""
    ref, cd = tex(1), tex(2)
    d = CooldownDetector(ref, cd, threshold=0.8, lock_seconds=0.0, confirm_frames=2)
    assert d.update(ref, 0.0) is False
    assert d.update(ref, 0.1) is False   # 稳定「可用」
    assert d.update(cd, 0.2) is False    # 单帧噪声：不触发
    assert d.update(ref, 0.3) is False   # 回稳：计数清零
    assert d.update(cd, 0.4) is False
    assert d.update(cd, 0.5) is True     # 连续两帧：触发
    assert d.status()["is_ready"] is False


def test_score_uses_max_not_corner():
    """ROI 比基准大时，应取最佳匹配（max），而非左上角。"""
    ref = tex(1, 48, 48)
    canvas = np.random.default_rng(9).integers(0, 255, (128, 128, 3), dtype=np.uint8)
    y, x = 40, 50
    canvas[y:y + 48, x:x + 48] = ref
    d = CooldownDetector(ref, None, threshold=0.9)
    assert d._score(canvas, ref) > 0.99


def test_textureless_ref_never_triggers_but_warns(caplog):
    """纯色基准图（零方差）会让匹配恒为 1.0 → 永远不触发，必须在构造期告警。"""
    solid = np.full((16, 16, 3), 200, dtype=np.uint8)
    with caplog.at_level("WARNING"):
        d = CooldownDetector(solid, None, threshold=0.5, confirm_frames=1)
    assert any("纹理" in r.getMessage() for r in caplog.records)
    for i in range(5):
        assert d.update(solid, float(i)) is False


def test_nan_score_degraded_to_zero(monkeypatch):
    """某些 OpenCV 版本零方差会返回 NaN；NaN 参与比较恒 False，必须按 0 处理。"""
    d = CooldownDetector(tex(1), None, threshold=0.5)
    monkeypatch.setattr(d, "_score", lambda icon, ref: float("nan"))
    assert d._score_checked(tex(1), tex(1)) == 0.0
    assert d.update(tex(1), 0.0) is False


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


def test_clipboard_method_pastes_instead_of_typing():
    kb, pasted = FakeKB(), []
    sender_mod.send_chat("大招", enter_delay=0, type_delay=0, kb=kb,
                         input_method="clipboard", clipboard_setter=pasted.append)
    assert pasted == ["大招"]
    assert ("write", "大招") not in kb.calls        # 不走逐字注入
    assert ("keyDown", "ctrl") in kb.calls
    assert ("press", "v") in kb.calls
    assert ("keyUp", "ctrl") in kb.calls


# ------------------------------------------------------------ 前台守卫
def test_title_matches_case_insensitive_substring():
    assert guard_mod.title_matches("300英雄 - 客户端", ["300英雄"])
    assert guard_mod.title_matches("League of Legends", ["legends"])
    assert not guard_mod.title_matches("记事本", ["300英雄"])


def test_title_matches_fails_closed_on_unknown():
    """拿不到标题时宁可不喊。"""
    assert guard_mod.title_matches(None, ["300英雄"]) is False
    assert guard_mod.title_matches("", ["300英雄"]) is False
    # 白名单为空 = 不限制
    assert guard_mod.title_matches(None, []) is True


def test_hotkey_watcher_edge_trigger():
    state = {"f9": True}
    w = guard_mod.HotkeyWatcher(["f9"], state_fn=lambda vk: state.get("f9", False))
    assert w.poll() == ["f9"]     # 首次按下
    assert w.poll() == []         # 按住不重复触发
    state["f9"] = False
    assert w.poll() == []
    state["f9"] = True
    assert w.poll() == ["f9"]     # 松开再按


def test_hotkey_watcher_ignores_unknown_key():
    w = guard_mod.HotkeyWatcher(["nonsense"], state_fn=lambda vk: True)
    assert w.unknown == ["nonsense"]
    assert w.poll() == []


# ------------------------------------------------------------ main / config
def test_config_load_and_validate():
    cfg = main_mod.load_config()
    assert "roi" in cfg and "phrases" in cfg
    assert isinstance(main_mod.validate_config(cfg), list)


def test_deep_merge_keeps_defaults():
    """老 config.json 缺新键时，靠默认值补齐而不是 KeyError。"""
    merged = main_mod.deep_merge(main_mod.DEFAULT_CFG, {"detect": {"threshold": 0.9}})
    assert merged["detect"]["threshold"] == 0.9
    assert merged["detect"]["confirm_frames"] == main_mod.DEFAULT_CFG["detect"]["confirm_frames"]
    assert merged["chat"]["input_method"] == "unicode"
    # 不污染默认值
    assert main_mod.DEFAULT_CFG["detect"]["threshold"] == 0.8


def test_validate_config_catches_bad_values():
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {})
    cfg["roi"] = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    cfg["detect"]["threshold"] = 1.5
    cfg["phrases"] = []
    problems = " ".join(main_mod.validate_config(cfg))
    assert "roi" in problems and "threshold" in problems and "phrases" in problems


def test_config_warnings_for_single_phrase_and_method():
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {"phrases": ["只剩一条"],
                                                     "chat": {"input_method": "typo"}})
    warns = " ".join(main_mod.config_warnings(cfg))
    assert "1 条" in warns and "input_method" in warns


def test_pick_phrase_avoids_immediate_repeat():
    import random
    rng = random.Random(0)
    picks = [main_mod.pick_phrase(rng, ["A", "B"], last="A") for _ in range(20)]
    assert picks and all(p == "B" for p in picks)   # 只有 B 可选
    assert main_mod.pick_phrase(rng, ["A"], last="A") == "A"  # 单条只能重复


def test_crop_roi():
    frame = np.arange(10 * 10 * 3, dtype=np.uint8).reshape(10, 10, 3)
    sub = main_mod.crop_roi(frame, {"left": 2, "top": 3, "right": 6, "bottom": 8})
    assert sub.shape == (5, 4, 3)


def test_crop_roi_clamps_out_of_bounds():
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    sub = main_mod.crop_roi(frame, {"left": 5, "top": 5, "right": 999, "bottom": 999})
    assert sub.shape == (5, 5, 3)


def test_run_paces_loop_to_target_fps():
    """抓帧是非阻塞的：主循环必须按 capture.fps 自己补间隔，否则空转吃满 CPU。"""
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {
        "roi": {"left": 0, "top": 0, "right": 8, "bottom": 8},
        "phrases": ["台词一"],
        "capture": {"fps": 10},
    })

    class _Det:
        def update(self, icon, now):
            return False

    slept = []
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    fired = main_mod.run(cfg, main_mod._FakeCapture([frame] * 3), _Det(), None,
                         clock=lambda: 0.0, sleeper=slept.append, max_frames=3)
    assert fired == 0
    assert len(slept) == 3                      # 每帧都补了一次间隔
    assert all(abs(s - 0.1) < 1e-6 for s in slept)   # fps=10 → 0.1s


# ------------------------------------------------------------ 标定（离线）
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


def _write_cfg(tmp_path, roi):
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {"roi": roi})
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_check_calibration_passes_on_good_refs(tmp_path):
    roi = {"left": 10, "top": 10, "right": 58, "bottom": 58}
    cfg_path = _write_cfg(tmp_path, roi)
    refs = tmp_path / "refs"
    refs.mkdir()
    cv2.imwrite(str(refs / "ready.png"), tex(1))
    cv2.imwrite(str(refs / "cd.png"), tex(2))
    ok, lines = calib_mod.check_calibration(str(cfg_path), str(refs))
    assert ok, "\n".join(lines)
    assert any("建议阈值" in line for line in lines)


def test_check_calibration_flags_textureless_roi(tmp_path):
    roi = {"left": 10, "top": 10, "right": 58, "bottom": 58}
    cfg_path = _write_cfg(tmp_path, roi)
    refs = tmp_path / "refs"
    refs.mkdir()
    cv2.imwrite(str(refs / "ready.png"), np.full((48, 48, 3), 120, dtype=np.uint8))
    ok, lines = calib_mod.check_calibration(str(cfg_path), str(refs))
    assert not ok
    assert any("纹理" in line for line in lines)


def test_check_calibration_flags_size_mismatch(tmp_path):
    roi = {"left": 10, "top": 10, "right": 58, "bottom": 58}
    cfg_path = _write_cfg(tmp_path, roi)
    refs = tmp_path / "refs"
    refs.mkdir()
    cv2.imwrite(str(refs / "ready.png"), tex(1, 32, 32))
    ok, lines = calib_mod.check_calibration(str(cfg_path), str(refs))
    assert any("尺寸" in line for line in lines)


# ------------------------------------------------------------ 端到端启动自检
def test_self_test_subprocess():
    r = subprocess.run([sys.executable, "main.py", "--self-test"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELF-TEST OK" in r.stdout


# ------------------------------------------------------------ 标定 GUI 缩放
def test_display_scale_keeps_small_frames_and_shrinks_big_ones():
    small = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert calib_mod._display_scale(small) == 1.0       # 放得下就不缩
    big = np.zeros((1440, 2560, 3), dtype=np.uint8)     # 2K 全屏：直接弹窗会超出屏幕
    scale = calib_mod._display_scale(big)
    assert 0.5 < scale < 1.0


def test_scale_roi_back_maps_preview_coords_to_full_resolution():
    shape = (1440, 2560, 3)
    scale = 0.625                                        # 1600/2560
    roi = calib_mod._scale_roi_back((100, 50, 64, 64), scale, shape)
    assert (roi["right"] - roi["left"]) == 102           # 64 / 0.625 ≈ 102
    assert (roi["bottom"] - roi["top"]) == 102
    assert roi["left"] == 160 and roi["top"] == 80


def test_scale_roi_back_clamps_to_frame():
    roi = calib_mod._scale_roi_back((0, 0, 5000, 5000), 1.0, (100, 100, 3))
    assert roi == {"left": 0, "top": 0, "right": 100, "bottom": 100}


def test_select_roi_gui_scales_preview_and_maps_back(monkeypatch):
    frame = np.zeros((1440, 2560, 3), dtype=np.uint8)
    seen = {}

    def _fake_select(name, img, showCrosshair=True):
        seen["shape"] = img.shape
        return (100, 50, 64, 64)

    monkeypatch.setattr(cv2, "selectROI", _fake_select)
    monkeypatch.setattr(cv2, "destroyAllWindows", lambda: None)
    roi = calib_mod._select_roi_gui(frame)
    h, w = seen["shape"][:2]
    assert w <= 1600 and h <= 900                     # 预览不会超出 1600×900 的屏幕
    assert 90 <= roi["right"] - roi["left"] <= 120    # 64 / 0.625 ≈ 102


# ------------------------------------------------------------ 真机发送测试
def test_test_send_refuses_when_foreground_not_allowed(monkeypatch):
    """守卫不放行时，--test-send 必须一次都不发。"""
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {"chat": {"foreground_titles": ["300英雄"]}})
    monkeypatch.setattr(main_mod, "build_guard", lambda c: (lambda: (False, "记事本")))
    sent = []
    monkeypatch.setattr(main_mod, "build_sender", lambda c: sent.append)
    assert main_mod._test_send(cfg, "你好") == 1
    assert sent == []


def test_test_send_sends_plain_text_when_allowed(monkeypatch):
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {"chat": {"foreground_titles": ["300英雄"]}})
    monkeypatch.setattr(main_mod, "build_guard", lambda c: (lambda: (True, "300英雄")))
    sent = []
    monkeypatch.setattr(main_mod, "build_sender", lambda c: sent.append)
    assert main_mod._test_send(cfg, "测试台词") == 0
    assert sent == ["测试台词"]


def test_test_send_reports_send_failure(monkeypatch):
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {"chat": {"foreground_titles": ["300英雄"]}})
    monkeypatch.setattr(main_mod, "build_guard", lambda c: (lambda: (True, "300英雄")))

    def _boom(text):
        raise RuntimeError("pydirectinput 不可用")

    monkeypatch.setattr(main_mod, "build_sender", lambda c: _boom)
    assert main_mod._test_send(cfg, "测试台词") == 1


def test_run_survives_send_error():
    """发送抛异常（键盘库缺失/权限问题）不能让主循环整个崩掉。"""
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {
        "roi": {"left": 0, "top": 0, "right": 8, "bottom": 8},
        "phrases": ["台词一"], "min_interval": 0, "max_interval": 0,
    })

    class _Det:
        def __init__(self):
            self.n = 0

        def update(self, icon, now):
            self.n += 1
            return self.n == 1

    def _boom(text):
        raise RuntimeError("模拟键盘库报错")

    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    fired = main_mod.run(cfg, main_mod._FakeCapture([frame] * 2), _Det(), _boom,
                         clock=lambda: 0.0, sleeper=lambda *_: None, max_frames=2)
    assert fired == 1        # 触发并放行了，只是发送失败被吃掉


def test_text_prefix_is_prepended(monkeypatch):
    """有些游戏靠文字前缀切公屏（如 "/all "），前缀应无条件拼在台词前面。"""
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {"chat": {"text_prefix": "/all "}})
    seen = []
    monkeypatch.setattr(sender_mod, "send_chat", lambda text, **kw: seen.append(text))
    main_mod.build_sender(cfg)("大招好了")
    assert seen == ["/all 大招好了"]


def test_text_prefix_default_is_empty(monkeypatch):
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {})
    seen = []
    monkeypatch.setattr(sender_mod, "send_chat", lambda text, **kw: seen.append(text))
    main_mod.build_sender(cfg)("大招好了")
    assert seen == ["大招好了"]


def test_validate_config_rejects_non_string_prefix():
    cfg = main_mod.deep_merge(main_mod.DEFAULT_CFG, {"chat": {"text_prefix": 123}})
    assert any("text_prefix" in p for p in main_mod.validate_config(cfg))


def test_grab_screen_uses_configured_monitor(monkeypatch):
    """多显示器时标定必须抓 config.json 指定的那块屏，否则 ROI 全错。"""
    import capture as capture_mod
    seen = {}

    class _FakeCap:
        backend = "fake"

        def __init__(self, **kw):
            seen.update(kw)

        def grab(self):
            return np.zeros((4, 4, 3), dtype=np.uint8)

        def release(self):
            return None

    monkeypatch.setattr(capture_mod, "ScreenCapture", _FakeCap)
    monkeypatch.setattr(main_mod, "load_config",
                        lambda *a, **k: {"capture": {"output_idx": 1, "mode": "mss", "fps": 5}})
    img = calib_mod._grab_screen("dummy.json")
    assert img is not None
    assert seen["output_idx"] == 1 and seen["prefer"] == "mss" and seen["fps"] == 5


def test_grab_screen_tolerates_broken_config(monkeypatch):
    import capture as capture_mod

    class _FakeCap:
        backend = "fake"

        def __init__(self, **kw):
            self.kw = kw

        def grab(self):
            return np.zeros((4, 4, 3), dtype=np.uint8)

        def release(self):
            return None

    def _boom(*a, **k):
        raise ValueError("配置坏了")

    monkeypatch.setattr(capture_mod, "ScreenCapture", _FakeCap)
    monkeypatch.setattr(main_mod, "load_config", _boom)
    assert calib_mod._grab_screen("nope.json") is not None   # 配置坏了也要能标定


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
