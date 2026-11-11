"""日志系统：控制台 + 文件（``logs/``，按大小轮转）。

各模块用 ``logging.getLogger("300-shout.xxx")`` 拿 logger，统一挂到父 logger
``300-shout`` 下，由 ``setup_logging()`` 一次性配置 handler。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "300-shout"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: str = "logs", level: int = logging.INFO,
                  filename: str = "300-shout.log") -> logging.Logger:
    """配置根 logger。幂等：重复调用不会重复加 handler。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if logger.handlers:
        return logger

    fmt = logging.Formatter(_FORMAT, _DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(log_dir, filename), maxBytes=2 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.debug("日志文件: %s", os.path.join(log_dir, filename))
    except OSError as e:  # 无写权限时退化为仅控制台
        logger.warning("文件日志不可用(%s)，仅输出到控制台", e)

    return logger
