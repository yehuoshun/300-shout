"""画面捕获层：dxcam(DDA) 抓独占全屏，失败回退 mss 抓窗口。

dxcam 走 Desktop Duplication API，独占全屏也能抓；初始化慢 1~2 秒，
期间 ``grab()`` 返回 None，调用方需跳过。窗口化场景或 dxcam 不可用时
回退 mss。
"""
import logging
from typing import Optional

import numpy as np

log = logging.getLogger("300-shout.capture")


class ScreenCapture:
    def __init__(self, fps: int = 10, output_idx: int = 0, prefer: str = "dxcam"):
        self._cam = None
        self._mss = None
        self._monitor = None
        if prefer == "dxcam":
            self._init_dxcam(fps, output_idx)
        if self._cam is None:
            self._init_mss()

    def _init_dxcam(self, fps: int, output_idx: int):
        try:
            import dxcam
            self._cam = dxcam.create(output_idx=output_idx, output_color="BGR")
            self._cam.start(target_fps=int(fps))
            log.info("dxcam 已启动（DDA，独占全屏可用）")
        except Exception as e:  # noqa: BLE001 - 任何失败都回退 mss
            self._cam = None
            log.warning("dxcam 不可用(%s)，回退 mss", e)

    def _init_mss(self):
        try:
            from mss import mss
            self._mss = mss()
            self._monitor = self._mss.monitors[1]
            log.info("mss 已启动（窗口化回退，monitor=%s）", self._monitor)
        except Exception as e:  # noqa: BLE001
            self._mss = None
            log.error("mss 也不可用(%s)，无可用捕获后端", e)

    @property
    def backend(self) -> str:
        if self._cam is not None:
            return "dxcam"
        if self._mss is not None:
            return "mss"
        return "none"

    def grab(self) -> Optional[np.ndarray]:
        """返回一帧 BGR 图像；未就绪或失败返回 None。"""
        if self._cam is not None:
            return self._cam.get_latest_frame()  # 未就绪时为 None
        if self._mss is not None:
            try:
                shot = self._mss.grab(self._monitor)
                return np.asarray(shot)[:, :, :3]
            except Exception as e:  # noqa: BLE001
                log.warning("mss 抓帧失败: %s", e)
                return None
        return None

    def release(self):
        if self._cam is not None:
            try:
                self._cam.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._mss is not None:
            try:
                self._mss.close()
            except Exception:  # noqa: BLE001
                pass
