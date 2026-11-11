"""检测层：大招图标 CD 检测（模板匹配 + 上升沿 + 防抖锁）。

原理：对「可用态」基准图做模板匹配，相似度骤降 = CD 遮罩出现 = 刚施放。
``update()`` 返回 True 表示检测到上升沿（可用 → CD），已内置防抖锁。
"""
import logging
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("300-shout.detect")


class CooldownDetector:
    """对「可用态」基准做模板匹配；相似度骤降 = CD 开始 = 施放瞬间。"""

    def __init__(self, ready_ref: np.ndarray, cd_ref: Optional[np.ndarray] = None,
                 threshold: float = 0.8, lock_seconds: float = 3.0):
        if ready_ref is None or np.asarray(ready_ref).size == 0:
            raise ValueError("ready_ref 不能为空")
        self._ready_ref = ready_ref
        self._cd_ref = cd_ref
        self._threshold = float(threshold)
        self._lock_seconds = float(lock_seconds)
        self._lock_until = 0.0
        # 初值 False：启动首帧不会产生「上升沿」，避免刚进图就误喊一次。
        self._prev_ready = False
        self._warned_size = False

    # ---- 内部工具 ----------------------------------------------------
    @staticmethod
    def _score(icon: np.ndarray, ref: np.ndarray) -> float:
        """模板匹配打分。

        取整张响应矩阵的**最大值**（而非左上角 ``[0][0]``）。
        ROI 裁剪尺寸与基准图尺寸一致时矩阵为 1x1，二者等价；一旦尺寸因
        分辨率/缩放弃改变，取最大值才能拿到真实的最高相似度，避免静默失准。
        """
        res = cv2.matchTemplate(icon, ref, cv2.TM_CCOEFF_NORMED)
        return float(res.max()) if res.size else 0.0

    # ---- 主接口 ------------------------------------------------------
    def update(self, icon: Optional[np.ndarray], now: float) -> bool:
        """喂入一帧 ROI 图，返回是否检测到「可用→CD」上升沿。"""
        if icon is None or np.asarray(icon).size == 0:
            return False  # 未就绪帧，跳过
        if now < self._lock_until:
            return False  # 防抖锁内
        if icon.shape[0] < self._ready_ref.shape[0] or icon.shape[1] < self._ready_ref.shape[1]:
            if not self._warned_size:
                log.warning("ROI %s 小于基准图 %s，检测不可靠，请重新标定",
                            icon.shape, self._ready_ref.shape)
                self._warned_size = True
            return False
        ready_score = self._score(icon, self._ready_ref)
        ready = ready_score > self._threshold
        if self._cd_ref is not None:
            cd_score = self._score(icon, self._cd_ref)
            ready = ready and ready_score > cd_score
        triggered = self._prev_ready and not ready
        self._prev_ready = ready
        if triggered:
            self._lock_until = now + self._lock_seconds
        return triggered
