"""画面捕获层：dxcam(DDA) 抓独占全屏，失败回退 mss 抓窗口。"""
import logging
from typing import Optional

import numpy as np

log = logging.getLogger("300-shout.capture")


class ScreenCapture:
    """全屏捕获，优先 dxcam（支持独占全屏），回退 mss（窗口化场景）。

    初始化慢 1-2 秒（DDA 接口建立），期间 grab() 返回 None，调用方需跳过。
    """

    def __init__(self, fps: int = 10, output_idx: int = 0):
        self._cam = None
        self._mss = None
        self._init_dxcam(fps, output_idx)

    def _init_dxcam(self, fps: int, output_idx: int):
        try:
            import dxcam
            self._cam = dxcam.create(output_idx=output_idx, output_color="BGR")
            self._cam.start(target_fps=fps)
            log.info("dxcam 已启动（DDA，全屏可用）")
        except Exception as e:
            log.warning("dxcam 初始化失败(%s)，回退 mss", e)
            from mss import mss
            self._mss = mss()
            self._monitor = self._mss.monitors[1]

    def grab(self) -> Optional[np.ndarray]:
        """返回一帧 BGR 图像；未就绪返回 None。"""
        if self._cam is not None:
            return self._cam.get_latest_frame()
        if self._mss is not None:
            shot = self._mss.grab(self._monitor)
            return np.array(shot)[:, :, :3]
        return None

    def release(self):
        if self._cam is not None:
            self._cam.stop()
        if self._mss is not None:
            self._mss.close()
