"""标定工具：截图 → 框选大招图标 → 采集「可用/CD」两态基准 → 写 config.json。

交互模式（真机）::

    python calibrate.py
    # 1. 游戏进自定义/训练模式
    # 2. 弹出全屏截图，鼠标框选大招图标（技能栏 R 槽），回车确认
    # 3. 按提示在「可用」状态回车采集基准
    # 4. 放大招后在「CD 中」状态回车采集基准
    # → 自动写 refs/ready.png、refs/cd.png 与 config.json 的 roi

体检模式（标定完先跑它，离线、不碰游戏）::

    python calibrate.py --check
    # 检查 ROI 与基准图尺寸是否一致、区域是否有纹理、两态是否分得开，
    # 并给出建议阈值。红字（问题）修完再进游戏。

离线模式（无 GUI，供 CI / 预先截好图时使用）::

    python calibrate.py --image shot.png --roi 100,200,64,64 \\
        --ready ready.png --cd cd.png
"""
import argparse
import json
import logging
import os
import sys
import time

import cv2

from paths import default_config, dirs_for

log = logging.getLogger("300-shout.calibrate")


# ---------------------------------------------------------------- 核心
def save_calibration(config_path: str, roi: dict, ready_img, cd_img=None,
                     refs_dir: str = None) -> dict:
    refs_dir = refs_dir or dirs_for(config_path)[1]
    """写入基准图 + 更新 config.json 的 roi。返回更新后的配置。"""
    os.makedirs(refs_dir, exist_ok=True)
    ready_path = os.path.join(refs_dir, "ready.png")
    if ready_img is None or not cv2.imwrite(ready_path, ready_img):
        raise IOError(f"写入基准图失败: {ready_path}")
    if cd_img is not None:
        cd_path = os.path.join(refs_dir, "cd.png")
        if not cv2.imwrite(cd_path, cd_img):
            raise IOError(f"写入基准图失败: {cd_path}")

    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["roi"] = {"left": int(roi["left"]), "top": int(roi["top"]),
                  "right": int(roi["right"]), "bottom": int(roi["bottom"])}
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log.info("标定完成：roi=%s 基准图→%s", cfg["roi"], refs_dir)
    return cfg


def _crop(img, roi):
    return img[roi["top"]:roi["bottom"], roi["left"]:roi["right"]]


def _score(img, ref) -> float:
    """同 detect 的打分口径：取响应矩阵最大值，非有限值归零。"""
    import numpy as np
    if img is None or ref is None:
        return 0.0
    if img.shape[0] < ref.shape[0] or img.shape[1] < ref.shape[1]:
        return 0.0
    res = cv2.matchTemplate(img, ref, cv2.TM_CCOEFF_NORMED)
    if not np.size(res):
        return 0.0
    val = float(np.max(res))
    return val if np.isfinite(val) else float("nan")


def check_calibration(config_path: str = None, refs_dir: str = None):
    config_path = config_path or default_config()
    refs_dir = refs_dir or dirs_for(config_path)[1]
    """标定体检。返回 ``(ok, lines)``；lines 每条形如 ``"OK/警告/错误: ..."``。"""
    import main as main_mod

    lines = []
    problems = 0

    def ok(msg):
        lines.append(f"OK   : {msg}")

    def warn(msg):
        nonlocal problems
        problems += 1
        lines.append(f"警告 : {msg}")

    def bad(msg):
        nonlocal problems
        problems += 2
        lines.append(f"错误 : {msg}")

    cfg = main_mod.load_config(config_path)
    roi = cfg.get("roi") or {}
    roi_w = int(roi.get("right", 0)) - int(roi.get("left", 0))
    roi_h = int(roi.get("bottom", 0)) - int(roi.get("top", 0))
    threshold = float((cfg.get("detect") or {}).get("threshold", 0.8))
    if roi_w <= 0 or roi_h <= 0:
        bad(f"config.json 的 roi 还没标定（{roi!r}），先跑 --calibrate（源码版：python calibrate.py）")
        return False, lines
    ok(f"ROI = {roi_w}×{roi_h} px，阈值 {threshold}")

    ready_path = os.path.join(refs_dir, "ready.png")
    cd_path = os.path.join(refs_dir, "cd.png")
    if not os.path.exists(ready_path):
        bad(f"缺少可用态基准图: {ready_path}")
        return False, lines
    ready = cv2.imread(ready_path, cv2.IMREAD_COLOR)
    if ready is None:
        bad(f"基准图读不出来（文件损坏？）: {ready_path}")
        return False, lines

    if (ready.shape[1], ready.shape[0]) != (roi_w, roi_h):
        warn(f"基准图尺寸 {ready.shape[1]}×{ready.shape[0]} 与 ROI {roi_w}×{roi_h} 不一致；"
             f"ROI 改过就要重新标定")
    else:
        ok("基准图尺寸与 ROI 一致")

    self_score = _score(ready, ready)
    import numpy as np
    if float(np.std(ready.astype(np.float32))) < 1.0:
        warn("可用态基准图几乎没有纹理（纯色区域）；模板匹配会恒等于 1.0，"
             "检测将永远不触发。请框选带图标细节的区域重新标定")
    elif not (self_score == self_score):  # NaN
        warn("可用态基准图自匹配得分为 NaN（零方差/无纹理区域），"
             "模板匹配无定义；请重新框选")
    elif self_score < 0.99:
        warn(f"基准图自匹配得分只有 {self_score:.3f}，基准图可能已损坏")
    else:
        ok("基准图自匹配 1.000（可用于匹配）")

    if not os.path.exists(cd_path):
        warn("没有 CD 态基准图（可选），检测将只靠阈值判断，抗误判能力弱一些")
        lines.append(f"建议 : 建议阈值区间 0.70~0.90（当前 {threshold}）")
        return problems == 0, lines

    cd = cv2.imread(cd_path, cv2.IMREAD_COLOR)
    if cd is None:
        bad(f"CD 态基准图读不出来: {cd_path}")
        return False, lines
    if cd.shape[:2] != ready.shape[:2]:
        warn(f"CD 态基准图尺寸 {cd.shape[:2]} 与可用态 {ready.shape[:2]} 不一致")

    cross = _score(cd, ready)   # CD 态画面 vs 可用态基准：越小越好分
    if not (cross == cross):
        warn("两态对比得分是 NaN，基准图无纹理，无法评估可分性")
        return problems == 0, lines
    ok(f"可用态基准 vs CD 态画面 = {cross:.3f}（越低越好分）")
    if cross >= threshold:
        bad(f"两态得分 {cross:.3f} ≥ 阈值 {threshold}，CD 遮罩压不掉相似度；"
            f"建议把 detect.threshold 调到 {cross + 0.02:.2f} 以上，或重选图标区域")
    else:
        margin = 1.0 - cross
        suggests = round(min(0.95, max(0.5, cross + margin / 2)), 2)
        lines.append(f"建议 : 两态间距 {margin:.3f}，建议阈值 ≈ {suggests}"
                     f"（当前 {threshold}）")
        if threshold <= cross + 0.02:
            warn("当前阈值太贴近 CD 态得分，容易漏检/误检")
    return problems == 0, lines


# ---------------------------------------------------------------- 交互
def _grab_screen(config_path: str = None):
    config_path = config_path or default_config()
    """抓一帧屏幕。按 config.json 的 capture 段走（多显示器时别抓错屏）。"""
    from capture import ScreenCapture
    import main as main_mod
    try:
        cap_cfg = main_mod.load_config(config_path).get("capture") or {}
    except Exception:  # noqa: BLE001 配置坏了也要能标定
        cap_cfg = {}
    cap = ScreenCapture(fps=cap_cfg.get("fps", 10),
                        output_idx=cap_cfg.get("output_idx", 0),
                        prefer=cap_cfg.get("mode", "dxcam"))
    try:
        for _ in range(60):
            frame = cap.grab()
            if frame is not None:
                return frame
            time.sleep(0.1)
        return None
    finally:
        rel = getattr(cap, "release", None)
        if callable(rel):
            rel()


def _display_scale(frame, max_w: int = 1600, max_h: int = 900) -> float:
    """预览窗口限尺寸：2K/4K 截图直接弹窗会超出屏幕，框选时拖不到边角。"""
    h, w = frame.shape[:2]
    if w <= max_w and h <= max_h:
        return 1.0
    return min(max_w / float(w), max_h / float(h))


def _scale_roi_back(r, scale: float, shape) -> dict:
    """把「缩小后的预览窗口」里框出的矩形换算回原始分辨率坐标。"""
    x, y, w, h = (int(round(v / scale)) for v in r)
    height, width = shape[:2]
    return {"left": max(0, x), "top": max(0, y),
            "right": min(width, x + w), "bottom": min(height, y + h)}


def _select_roi_gui(frame) -> dict:
    scale = _display_scale(frame)
    if scale < 1.0:
        view = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        print(f"预览已缩到 {scale * 100:.0f}% 显示（原图 {frame.shape[1]}×{frame.shape[0]}）")
    else:
        view = frame
    r = cv2.selectROI("框选大招图标 (R槽) 后按 Enter", view, showCrosshair=True)
    cv2.destroyAllWindows()
    x, y, w, h = r
    if w == 0 or h == 0:
        raise SystemExit("未选择区域，已取消。")
    roi = _scale_roi_back((x, y, w, h), scale, frame.shape)
    if roi["right"] - roi["left"] < 8 or roi["bottom"] - roi["top"] < 8:
        raise SystemExit("选区太小（宽高需 ≥8px），已取消。")
    return roi


def interactive(config_path: str) -> int:
    frame = _grab_screen(config_path)
    if frame is None:
        print("抓屏失败：请确认游戏已运行且捕获后端可用。")
        return 1
    roi = _select_roi_gui(frame)
    print(f"ROI = {roi}")

    input("请让大招处于【可用】状态，然后按 Enter 采集『可用态』基准...")
    ready_img = _crop(_grab_screen(config_path), roi)

    cd_img = None
    if input("需要同时采集『CD 态』基准吗？(y/N): ").strip().lower() == "y":
        input("请放大招进入 CD，然后按 Enter 采集『CD 态』基准...")
        cd_img = _crop(_grab_screen(config_path), roi)

    save_calibration(config_path, roi, ready_img, cd_img)
    print("标定完成，先体检一次：")
    _ok, lines = check_calibration(config_path)
    for line in lines:
        print("  " + line)
    print("体检没问题就可以: python main.py --dry-run --stats 30（演练，不真发）")
    print("确认检测正常后再: python main.py")
    return 0


# ---------------------------------------------------------------- 入口
def _parse_roi(s: str) -> dict:
    x, y, w, h = (int(v) for v in s.split(","))
    return {"left": x, "top": y, "right": x + w, "bottom": y + h}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="300 Shout 标定工具")
    ap.add_argument("--config", default=default_config(),
                    help="配置文件路径（默认程序目录下的 config/config.json）")
    ap.add_argument("--image", help="用现成全屏截图代替实时抓屏（离线模式）")
    ap.add_argument("--roi", help="ROI 像素，格式 x,y,w,h（跳过 GUI 框选）")
    ap.add_argument("--ready", help="现成『可用态』基准图路径（离线模式）")
    ap.add_argument("--cd", help="现成『CD 态』基准图路径（可选）")
    ap.add_argument("--refs-dir", default=None,
                    help="基准图目录（默认跟 config.json 同目录的 refs/）")
    ap.add_argument("--check", action="store_true",
                    help="只体检已有标定（不抓屏、不框选）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.check:
        ok, lines = check_calibration(args.config, args.refs_dir)
        print(f"标定体检（{args.config} / {args.refs_dir}）")
        for line in lines:
            print("  " + line)
        print("结论：可以进游戏" if ok else "结论：先修上面的问题再进游戏")
        return 0 if ok else 1

    # 离线模式：不依赖 GUI / 显示器
    if args.image and args.roi and args.ready:
        frame = cv2.imread(args.image, cv2.IMREAD_COLOR)
        if frame is None:
            print(f"读不到截图: {args.image}")
            return 1
        roi = _parse_roi(args.roi)
        ready_img = cv2.imread(args.ready, cv2.IMREAD_COLOR)
        cd_img = cv2.imread(args.cd, cv2.IMREAD_COLOR) if args.cd else None
        if ready_img is None:
            print(f"读不到基准图: {args.ready}")
            return 1
        if ready_img.shape[:2] != (roi["bottom"] - roi["top"], roi["right"] - roi["left"]):
            print(f"提示：基准图尺寸 {ready_img.shape[:2]} 与 ROI 尺寸 "
                  f"{(roi['bottom']-roi['top'], roi['right']-roi['left'])} 不一致。")
        save_calibration(args.config, roi, ready_img, cd_img, args.refs_dir)
        print("离线标定完成。")
        return 0

    return interactive(args.config)


if __name__ == "__main__":
    sys.exit(main())
