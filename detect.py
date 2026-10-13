"""检测层：大招图标 CD 检测（模板匹配 + 上升沿 + 防抖锁）。"""
from typing import Optional

import cv2
import numpy as np


class CooldownDetector:
    """对'可用态'基准做模板匹配；相似度骤降 = CD 开始 = 施放瞬间。

    update() 返回 True 表示检测到上升沿（可用→CD），已内置防抖锁。
    """

    def __init__(self, ready_ref: np.ndarray, cd_ref: Optional[np.ndarray] = None,
                 threshold: float = 0.8, lock_seconds: float = 3.0):
        self._ready_ref = ready_ref
        self._cd_ref = cd_ref
        self._threshold = threshold
        self._lock_seconds = lock_seconds
        self._lock_until = 0.0
        self._prev_ready = True

    def update(self, icon: np.ndarray, now: float) -> bool:
        if now < self._lock_until:
            return False
        ready_score = float(cv2.matchTemplate(
            icon, self._ready_ref, cv2.TM_CCOEFF_NORMED)[0][0])
        ready = ready_score > self._threshold
        if self._cd_ref is not None:
            cd_score = float(cv2.matchTemplate(
                icon, self._cd_ref, cv2.TM_CCOEFF_NORMED)[0][0])
            ready = ready and ready_score > cd_score
        triggered = self._prev_ready and not ready
        self._prev_ready = ready
        if triggered:
            self._lock_until = now + self._lock_seconds
        return triggered
