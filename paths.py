"""路径解析：源码运行与 PyInstaller 冻结运行共用同一套规则。

冻结（``--onefile``）后 ``__file__`` 指向临时解包目录，进程退出就删掉。
若把配置、基准图、日志放在那里，用户改过的配置和标定结果下次启动全没了，
所以程序根目录一律取 **exe 所在目录**；内置只读资源（默认 config.json）
才从解包目录 ``sys._MEIPASS`` 取。
"""
import logging
import os
import shutil
import sys

log = logging.getLogger("300-shout.paths")


def app_root() -> str:
    """程序根目录：源码运行是仓库目录，冻结后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def bundle_dir() -> str:
    """内置只读资源所在目录：冻结时是解包目录，源码运行时等于程序根目录。"""
    return getattr(sys, "_MEIPASS", app_root())


ROOT = app_root()
BUNDLE_DIR = bundle_dir()
DEFAULT_CONFIG = os.path.join(ROOT, "config.json")
REFS_DIR = os.path.join(ROOT, "refs")
LOGS_DIR = os.path.join(ROOT, "logs")


def ensure_config(path: str = DEFAULT_CONFIG) -> str:
    """首次运行时把内置的默认 config.json 释放到程序目录旁；已存在则原样保留。

    只负责"有没有"，不负责内容升级——缺的键由 ``main.deep_merge`` 补。
    """
    if os.path.exists(path):
        return path
    bundled = os.path.join(BUNDLE_DIR, "config.json")
    if os.path.abspath(bundled) == os.path.abspath(path) or not os.path.exists(bundled):
        return path
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        shutil.copyfile(bundled, path)
        log.info("首次运行：已生成默认配置 %s", path)
    except OSError as e:
        log.warning("无法生成默认配置 %s：%s", path, e)
    return path
