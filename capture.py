"""画面捕获层：dxcam(DDA) 抓独占全屏，失败回退 mss 抓窗口。

dxcam 走 Desktop Duplication API，独占全屏也能抓；初始化慢 1~2 秒，
期间拿不到帧，调用方需跳过。窗口化场景或 dxcam 不可用时回退 mss。

**为什么 grab() 要缓存末帧**：DDA 只在画面**有变化**时才产生新帧。游戏停在
加载画面/主菜单（画面静止）时，阻塞式的 ``get_latest_frame()`` 会把主循环
整个挂住——热键、暂停、统计全部失灵。所以这里统一用非阻塞的 ``grab()``，
没有新帧就返回上一帧，让主循环照常转。
"""
import logging
from typing import Optional

import numpy as np

log = logging.getLogger("300-shout.capture")

# 抓帧异常时，每多少次失败告警一次（避免刷屏）
_WARN_EVERY = 100


class ScreenCapture:
    def __init__(self, fps: int = 10, output_idx: int = 0, prefer: str = "dxcam"):
        self._cam = None
        self._mss = None
        self._monitor = None
        self._last_frame: Optional[np.ndarray] = None
        self._fail_count = 0
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

    # ---- 抓帧 --------------------------------------------------------
    def _grab_raw(self) -> Optional[np.ndarray]:
        """向当前后端要一帧；没有新帧/失败返回 None。"""
        if self._cam is not None:
            try:
                # 非阻塞：上次 grab 之后没有新帧就返回 None
                return self._cam.grab()
            except Exception as e:  # noqa: BLE001
                self._on_fail("dxcam 抓帧失败: %s", e)
                return None
        if self._mss is not None:
            try:
                shot = self._mss.grab(self._monitor)
                return np.asarray(shot)[:, :, :3]
            except Exception as e:  # noqa: BLE001
                self._on_fail("mss 抓帧失败: %s", e)
                return None
        return None

    def _on_fail(self, msg: str, exc: Exception) -> None:
        self._fail_count += 1
        if self._fail_count == 1 or self._fail_count % _WARN_EVERY == 0:
            log.warning(msg + "（累计 %d 次）", exc, self._fail_count)

    def grab(self) -> Optional[np.ndarray]:
        """返回一帧 BGR 图像；既没有新帧也没有历史帧时返回 None。"""
        frame = self._grab_raw()
        if frame is not None:
            self._last_frame = frame
            self._fail_count = 0
            return frame
        return self._last_frame

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
        self._cam = None
        self._mss = None
