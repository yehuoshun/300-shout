"""标定工具：截图 → 框选大招图标 → 采集「可用/CD」两态基准 → 写 config.json。

交互模式（真机）::

    python calibrate.py
    # 1. 游戏进自定义/训练模式
    # 2. 弹出全屏截图，鼠标框选大招图标（技能栏 R 槽），回车确认
    # 3. 按提示在「可用」状态回车采集基准
    # 4. 放大招后在「CD 中」状态回车采集基准
    # → 自动写 refs/ready.png、refs/cd.png 与 config.json 的 roi

离线模式（无 GUI，供 CI / 预先截好图时使用）::

    python calibrate.py --image shot.png --roi 100,200,64,64 \
        --ready ready.png --cd cd.png
"""
import argparse
import json
import logging
import os
import sys

import cv2

log = logging.getLogger("300-shout.calibrate")

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(ROOT, "config.json")
REFS_DIR = os.path.join(ROOT, "refs")


# ---------------------------------------------------------------- 核心
def save_calibration(config_path: str, roi: dict, ready_img, cd_img=None,
                     refs_dir: str = REFS_DIR) -> dict:
    """写入基准图 + 更新 config.json 的 roi。返回更新后的配置。"""
    os.makedirs(refs_dir, exist_ok=True)
    ready_path = os.path.join(refs_dir, "ready.png")
    if not cv2.imwrite(ready_path, ready_img):
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


# ---------------------------------------------------------------- 交互
def _grab_screen():
    from capture import ScreenCapture
    cap = ScreenCapture()
    for _ in range(60):
        frame = cap.grab()
        if frame is not None:
            break
        import time
        time.sleep(0.1)
    return frame


def _select_roi_gui(frame) -> dict:
    r = cv2.selectROI("框选大招图标 (R槽) 后按 Enter", frame, showCrosshair=True)
    cv2.destroyAllWindows()
    x, y, w, h = r
    if w == 0 or h == 0:
        raise SystemExit("未选择区域，已取消。")
    return {"left": int(x), "top": int(y), "right": int(x + w), "bottom": int(y + h)}


def interactive(config_path: str) -> int:
    frame = _grab_screen()
    if frame is None:
        print("抓屏失败：请确认游戏已运行且捕获后端可用。")
        return 1
    roi = _select_roi_gui(frame)
    print(f"ROI = {roi}")

    input("请让大招处于【可用】状态，然后按 Enter 采集『可用态』基准...")
    ready_img = _crop(_grab_screen(), roi)

    cd_img = None
    if input("需要同时采集『CD 态』基准吗？(y/N): ").strip().lower() == "y":
        input("请放大招进入 CD，然后按 Enter 采集『CD 态』基准...")
        cd_img = _crop(_grab_screen(), roi)

    save_calibration(config_path, roi, ready_img, cd_img)
    print("标定完成，可以直接: python main.py")
    return 0


# ---------------------------------------------------------------- 入口
def _parse_roi(s: str) -> dict:
    x, y, w, h = (int(v) for v in s.split(","))
    return {"left": x, "top": y, "right": x + w, "bottom": y + h}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="300 Shout 标定工具")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--image", help="用现成全屏截图代替实时抓屏（离线模式）")
    ap.add_argument("--roi", help="ROI 像素，格式 x,y,w,h（跳过 GUI 框选）")
    ap.add_argument("--ready", help="现成『可用态』基准图路径（离线模式）")
    ap.add_argument("--cd", help="现成『CD 态』基准图路径（可选）")
    ap.add_argument("--refs-dir", default=REFS_DIR, help="基准图输出目录")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

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
