"""路径解析：源码运行与 PyInstaller 冻结运行共用同一套规则。

冻结（``--onefile``）后 ``__file__`` 指向临时解包目录，进程退出就删掉。
若把配置、基准图、日志放在那里，用户改过的配置和标定结果下次启动全没了，
所以程序根目录一律取 **exe 所在目录**；内置只读资源（默认 config.json）
才从解包目录 ``sys._MEIPASS`` 取。

用户数据统一收在一个 ``config/`` 文件夹里，exe 旁边只留「一个 exe + 一个 config」：

    <程序根目录>/
        300-Shout.exe
        config/
            config.json      配置（面板/标定都会改它）
            refs/            标定基准图 ready.png / cd.png
            logs/            运行日志

老版本把 ``config.json`` 直接摆在 exe 旁边的，继续沿用老位置（refs/ logs/ 也在那儿），
升级后不会突然变成「未标定」。新装的用户走新目录。
"""
import logging
import os
import shutil
import sys

log = logging.getLogger("300-shout.paths")

DATA_DIRNAME = "config"


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


def dirs_for(config_path: str):
    """配置在哪，refs/ 与 logs/ 就跟到哪。返回 (配置, 基准图目录, 日志目录)。

    这样 ``--config D:\\别的目录\\my.json`` 不会拿错基准图，也不会把日志写到别处。
    """
    cfg = os.path.abspath(config_path)
    folder = os.path.dirname(cfg)
    return cfg, os.path.join(folder, "refs"), os.path.join(folder, "logs")


def layout(root: str = None):
    """默认布局。返回 (配置, 基准图目录, 日志目录)。

    有老版本残留（根目录直接放着 config.json、且新位置还没有配置）就沿用老位置，
    否则用 ``<root>/config/``。判据只看"哪个配置文件真实存在"，不搬动用户文件。
    """
    root = os.path.abspath(root or ROOT)
    new_cfg = os.path.join(root, DATA_DIRNAME, "config.json")
    legacy_cfg = os.path.join(root, "config.json")
    if os.path.exists(legacy_cfg) and not os.path.exists(new_cfg):
        return legacy_cfg, os.path.join(root, "refs"), os.path.join(root, "logs")
    return dirs_for(new_cfg)


def default_config() -> str:
    """默认配置文件的完整路径（源码运行是仓库里的 config/config.json）。"""
    return layout()[0]


def default_refs_dir() -> str:
    return layout()[1]


def default_logs_dir() -> str:
    return layout()[2]


def ensure_config(path: str = None, bundled: str = None) -> str:
    """首次运行时把内置的默认 config.json 释放到程序目录（新建 config/ 文件夹）；
    已存在则原样保留。只负责"有没有"，不负责内容升级——缺的键由 ``main.deep_merge`` 补。
    """
    path = path or default_config()
    if os.path.exists(path):
        return path
    bundled = bundled or os.path.join(BUNDLE_DIR, DATA_DIRNAME, "config.json")
    if os.path.abspath(bundled) == os.path.abspath(path) or not os.path.exists(bundled):
        return path
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        shutil.copyfile(bundled, path)
        log.info("首次运行：已生成默认配置 %s", path)
    except OSError as e:
        log.warning("无法生成默认配置 %s：%s", path, e)
    return path
