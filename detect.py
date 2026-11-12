"""检测层：大招图标 CD 检测（模板匹配 + 多帧确认 + 上升沿 + 防抖锁）。

原理：对「可用态」基准图做模板匹配，相似度骤降 = CD 遮罩出现 = 刚施放。
``update()`` 返回 True 表示检测到「可用 → CD」的下降沿，已内置防抖锁。

两处保护，都是为了真机不被特效骗：

* **多帧确认**（``confirm_frames``，默认 2）：技能图标上有脉冲高亮、粒子特效、
  敌人技能印记等干扰，单帧掉下阈值是常事。要求连续 N 帧同向才切换状态，
  10fps 下代价是 100ms 延迟，换来的是不再被一帧噪声触发出一次喊话。
* **NaN/Inf 防护**：``TM_CCOEFF_NORMED`` 在纯色区域（基准图零方差）无定义，
  会返回 NaN。NaN 参与比较永远是 False，旧实现会让检测「静默全灭」。
  这里统一把非有限值归零并告警一次，提示重新框选一块有纹理的区域。
"""
import logging
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("300-shout.detect")


class CooldownDetector:
    """对「可用态」基准做模板匹配；相似度骤降 = CD 开始 = 施放瞬间。"""

    def __init__(self, ready_ref: np.ndarray, cd_ref: Optional[np.ndarray] = None,
                 threshold: float = 0.8, lock_seconds: float = 3.0,
                 confirm_frames: int = 2):
        if ready_ref is None or np.asarray(ready_ref).size == 0:
            raise ValueError("ready_ref 不能为空")
        if not 0.0 < float(threshold) <= 1.0:
            raise ValueError(f"threshold 必须在 (0, 1] 之间，收到 {threshold!r}")
        self._ready_ref = ready_ref
        self._cd_ref = cd_ref
        self._threshold = float(threshold)
        self._lock_seconds = float(lock_seconds)
        self._confirm = max(1, int(confirm_frames))
        self._lock_until = 0.0
        # 初值 False：启动首帧不会产生「下降沿」，避免刚进图就误喊一次。
        self._stable_ready = False
        self._ready_cnt = 0
        self._cd_cnt = 0
        self._locked = False
        self._warned_size = False
        self._warned_nan = False
        self._last_ready_score = None
        self._last_cd_score = None
        # 纯色基准图是「静默全灭」的典型来源：零方差模板与任何画面都算出 1.0，
        # ready 恒为 True，永远等不到下降沿。这里在构造期就喊一声。
        if float(np.std(np.asarray(ready_ref, dtype=np.float32))) < 1.0:
            log.warning("可用态基准图几乎没有纹理（像素方差≈0），模板匹配会恒等于 1.0，"
                        "检测将永远不触发；请重新标定一块带图标细节的区域")

    # ---- 内部工具 ----------------------------------------------------
    @staticmethod
    def _score(icon: np.ndarray, ref: np.ndarray) -> float:
        """模板匹配打分。

        取整张响应矩阵的**最大值**（而非左上角 ``[0][0]``）。
        ROI 裁剪尺寸与基准图尺寸一致时矩阵为 1x1，二者等价；一旦尺寸因
        分辨率/缩放弃改变，取最大值才能拿到真实的最高相似度，避免静默失准。
        """
        res = cv2.matchTemplate(icon, ref, cv2.TM_CCOEFF_NORMED)
        if not np.size(res):
            return 0.0
        score = float(np.max(res))
        if not np.isfinite(score):  # 纯色/零方差 → 归一化相关无定义
            return float("nan")
        return score

    def _score_checked(self, icon: np.ndarray, ref: np.ndarray) -> float:
        score = self._score(icon, ref)
        if not np.isfinite(score):
            if not self._warned_nan:
                log.warning("模板匹配返回 NaN（基准图区域很可能是纯色/无纹理），"
                            "该结果按 0 处理；请重新标定一块有纹理的图标区域")
                self._warned_nan = True
            return 0.0
        return score

    # ---- 主接口 ------------------------------------------------------
    def update(self, icon: Optional[np.ndarray], now: float) -> bool:
        """喂入一帧 ROI 图，返回是否检测到「可用→CD」下降沿。"""
        if icon is None or np.asarray(icon).size == 0:
            return False  # 未就绪帧，跳过
        self._locked = now < self._lock_until
        if self._locked:
            return False  # 防抖锁内：不评分也不改状态
        if icon.shape[0] < self._ready_ref.shape[0] or icon.shape[1] < self._ready_ref.shape[1]:
            if not self._warned_size:
                log.warning("ROI %s 小于基准图 %s，检测不可靠，请重新标定",
                            icon.shape, self._ready_ref.shape)
                self._warned_size = True
            return False

        ready_score = self._score_checked(icon, self._ready_ref)
        ready = ready_score > self._threshold
        cd_score = None
        if self._cd_ref is not None:
            cd_score = self._score_checked(icon, self._cd_ref)
            ready = ready and ready_score > cd_score
        self._last_ready_score = ready_score
        self._last_cd_score = cd_score

        if ready:
            self._ready_cnt += 1
            self._cd_cnt = 0
        else:
            self._cd_cnt += 1
            self._ready_cnt = 0

        triggered = False
        if not self._stable_ready and self._ready_cnt >= self._confirm:
            self._stable_ready = True
        elif self._stable_ready and self._cd_cnt >= self._confirm:
            self._stable_ready = False
            triggered = True

        if triggered:
            self._lock_until = now + self._lock_seconds
        return triggered

    # ---- 可观测性 ----------------------------------------------------
    def status(self) -> dict:
        """最近一次打分的快照，供日志/统计/标定体检使用。"""
        return {
            "ready_score": self._last_ready_score,
            "cd_score": self._last_cd_score,
            "is_ready": self._stable_ready,
            "locked": self._locked,
            "threshold": self._threshold,
            "confirm_frames": self._confirm,
        }
