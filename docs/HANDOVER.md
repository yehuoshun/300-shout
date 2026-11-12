# 300 Shout 交接文档

> 用途：后续接手者快速上手。背景、决策、待办、维护要点一次说清。

## 1. 项目背景

- 需求：300英雄（类 LOL 端游）开大招（R 技能）时，自动在公屏发送预设台词
- 动机：旧有闭源插件不可用，自建开源替代
- 状态：**v3 完成「安全试跑 + 健壮性」改造，CI 可验证启动；真机（游戏内实发）仍待人工实测**（见 §4）

## 2. 方案选型决策记录（勿轻易推翻）

| 方案 | 原理 | 结论 |
|------|------|------|
| A 按键监听 | 监听大招键+窗口校验 | 备选。分不清"打字按 R"与"按 R 放大招" |
| B 内存读取 | ReadProcessMemory 读技能冷却 | 已否决。跨进程读内存有反作弊风险 |
| **C 图像识别（采用）** | 截屏 + 模板匹配检测技能图标 CD 遮罩 | **采用**。零进程接触，反作弊视角等同真人操作，精准度接近 B |

参考案例：腾讯 GameAI SDK（游戏图像自动化官方开源）验证了"图像识别游戏技能状态"路线的成熟性；ALAS/FGO-py 提供了工程架构参考。

### 为什么不用闭源插件那套

旧插件大概率用注入/内存方案，本项目功能用图像方案即可实现，无需脏手段。

## 3. 技术要点

### 3.1 全屏捕获

- 300英雄为老引擎，**无无边框全屏选项**，老板选择保持独占全屏
- 因此画面层用 **dxcam（Desktop Duplication API）**，OBS 同款底层，独占全屏可抓
- 初始化慢 1-2s（`grab()` 返回 None 需跳过），抓帧快（~1-3ms/帧）
- `grab()` 非阻塞、返回末帧缓存；主循环自己按 `capture.fps` 补间隔（`_pace`），
  静止画面也稳定在设定帧率，空闲 CPU 占用低（符合「不长时间占物理机」的约束）
- 若未来切窗口化，`capture.mode` 改 mss 即可，代码已内置回退
- 两种后端都不可用时 `backend == "none"`，入口直接报错退出（不会静默空转）

### 3.2 大招检测

- 技能栏 QWER 布局，大招固定 R 槽（绝大多数英雄）
- 检测逻辑：对「可用态」基准图做 `matchTemplate`，相似度骤降 = CD 遮罩出现 = 施放瞬间
- 双基准（可用/CD）对比提升抗误判
- 上升沿 + 3s 防抖锁，防连点重复触发
- **匹配打分取响应矩阵最大值**（`res.max()`），兼容 ROI 与基准图尺寸不一致的情况
- **启动首帧不触发**（`_stable_ready` 初值 False），避免刚进图误喊一次
- **多帧确认**（v3，`confirm_frames` 默认 2）：连续 N 帧同向才切换状态。图标脉冲高亮、
  粒子特效、敌方印记都会让单帧掉下阈值；单帧判据几乎必然误触发，多帧只花 ~100ms
- **零方差防护**（v3）：纯色基准图（像素方差≈0）会让 `TM_CCOEFF_NORMED` 恒返回 1.0
  （某些 OpenCV 版本返回 NaN），结果是 `ready` 永远为真、**永远检测不到下降沿**——
  一种不报错、不崩、就是不出活的「静默全灭」。构造期检测方差并告警，检测层把 NaN 归零
- ROI 尺寸小于基准图 → 判为不可靠并告警（提示重新标定），不静默失准
- `status()` 暴露最近一次打分（ready/cd/是否稳定可用/是否在锁内），供 `--stats` 与标定体检使用

### 3.3 聊天发送

- `pydirectinput.write` 走 KEYEVENTF_UNICODE，中文无需输入法
- 频道切换序列 `chat.channel_keys` 配置化
- 若游戏靠**文字前缀**切公屏（LOL 系的 `/all `），用 `chat.text_prefix` 拼在台词前，
  和 `channel_keys` 可同时用、也可只用其一
- **组合键**写作 `"shift+enter"`，内部 `keyDown/keyUp` 包夹；`["shift","enter"]` 是两个独立按键，**不是**组合键
- **剪贴板输入法**（v3，`input_method="clipboard"`）：写 `CF_UNICODETEXT` 再按 Ctrl+V，
  兜底老引擎（DirectInput）吞掉 Unicode 注入字符的场景；纯 ctypes，不引第三方依赖
- **单发测试**（`python main.py --test-send "文本"`）：不起捕获、不需要标定，只走
  「守卫 → 开聊天 → 频道键 → 输入 → 回车」，用来把「检测不到」和「发不出去」两类问题分开；
  守卫不放行时一次都不发（`--no-guard` 才会强发，且先等 3s 提示）
- 发送异常（键盘库缺失/权限问题）在主循环里被捕获计数，不会让整个进程崩掉

### 3.4 安全护栏（v3 新增）

设计原则：**宁可漏喊，不可误发**。

| 护栏 | 实现 | 关键约束 |
|------|------|----------|
| 前台窗口校验 | `guard.foreground_title()` = `GetForegroundWindow` + `GetWindowTextW`；`title_matches()` 子串匹配、大小写不敏感 | **fail-closed**：拿不到标题（`None`/空串）→ 不发送；白名单为空 → 视为不限制 |
| 全局热键 | `HotkeyWatcher` 每帧 `GetAsyncKeyState` 轮询 + 边沿检测 | **不装键盘钩子、不起线程**——与「零常驻」原则一致，进程退出不留后台 |
| 演练模式 | `run(..., dry_run=True)` 只打日志，`send_fn=None` | 真机首验先跑 `--dry-run --stats`，把相似度看清楚再放行发送 |
| 配置校验 | `validate_config`（阻断）+ `config_warnings`（提醒）；`deep_merge` 补默认值 | roi 无效直接退出码 2 并提示先标定；老配置缺新键不会 KeyError |
| 非阻塞抓帧 | `capture.grab()` 用 dxcam 非阻塞 `grab()`，无新帧复用末帧 | 静态画面（加载/主菜单）不再把主循环整个挂住，热键/统计照常 |

非 Windows 平台（CI 的 ubuntu-latest）上 `guard` 全部安全降级：标题返回 `None`、热键永不触发，
所以整个仓库的测试可以在 Linux 上跑完。这也是「守卫类代码不要直接调 Win32」的原因 —— 一律经
`_user32()` 取句柄，失败就走降级分支。

### 3.5 日志

- `logsetup.setup_logging()`：控制台 + 文件（`logs/300-shout.log`，2MB × 3 轮转）
- 各模块用 `logging.getLogger("300-shout.<模块>")`，统一挂父 logger

### 3.6 打包与路径

- **发版**：推 `v*` tag（或手动触发）→ `.github/workflows/release.yml` 在 windows-latest 上跑
  PyInstaller 单文件打包（`--onefile --add-data "config.json;." --collect-submodules dxcam`）→
  用 `exe --self-test` 冒烟 → 发 Release（附 `.sha256` 与 `windows-x64.zip`）。手动触发只上传 artifact。
- **路径规则集中在 `paths.py`**：冻结后 `app_root()` = **exe 所在目录**（`config.json` / `refs/` / `logs/`
  都落这儿），`bundle_dir()` = 解包目录（只读的内置默认配置）；`ensure_config()` 首次运行把内置配置
  释放到 exe 旁边。原因：onefile 下 `__file__` 指向临时解包目录，进程退出即删——照旧写法会让用户
  改过的配置"下次启动就丢"。
- **已发版**：v3.0.0（2026-11-12，Release run 43155913157 全绿）→ https://github.com/yehuoshun/300-shout/releases/tag/v3.0.0 ，资产：`300-Shout.exe`（62MB）、`300-Shout.exe.sha256`、`300-Shout-windows-x64.zip`（exe + 默认配置 + 首验清单）。
- **标定也在 exe 里**：`--calibrate` / `--calibrate-check` 转发到 `calibrate.main()`，
  打包版用户不需要装 Python。`calibrate.py` 自己不再算路径，统一 `from paths import`。
- **已发版**：v3.1.0（2026-11-12，带图形面板）。exe 从 62MB 涨到约 67MB——面板把 Tk 运行时
  （`_tkinter.pyd`、`tcl86t.dll`、`tcl_data/`）一起打了进去。**不需要** `--hidden-import ui`：
  `main._launch_ui()` 里那句函数内 `import ui` 同样会被 PyInstaller 静态分析捕获，本地与 CI 用同一组
  参数即可；验证方式是 `Analysis-00.toc` 里能查到 `ui` / `tkinter` / `_tkinter.pyd`。
  冻结后的面板实测：双击无参数即开窗，配置路径显示为 exe 旁的 `config.json`。

## 4. 待办（接手者第一件事）

按顺序做，每步都有可观测的判据（可直接给用户照做的陪跑版：`docs/FIRST_RUN.md`）：

- [ ] **1. 查窗口标题**：真机运行 `python main.py --list-windows`，把游戏标题里的稳定片段填进
      `config.json` 的 `chat.foreground_titles`；游戏切到前台用 `python main.py --check-guard`
      验证（只读标题、不发键，输出「结果: 通过」即 OK）
- [ ] **2. 标定 + 体检**：`python calibrate.py` 框选 R 槽、采「可用/CD」两态 →
      `python calibrate.py --check` 全绿（无「错误」，警告清零）。判据：体检给出「可以进游戏」
- [ ] **3. 演练**：`python main.py --dry-run --stats 30`，在游戏里按 R。判据：日志出现
      `[DRY-RUN] 检测到大招，本应发送：…`，且 `ready` 分数跨过阈值、`cd` 分数明显更低。
      若 ready 恒 ≈1.0 不掉 → 区域没有 CD 变化，重构 ROI；若 ready 在阈值附近抖 → 按打印的
      ready/cd 两值把阈值定在中间
- [ ] **4. 前台守卫**：演练时 Alt-Tab 到别的窗口再按 R。判据：日志出现「前台窗口 … 不在白名单，
      本次不发送」，且有效帧在跑（不是卡住）
- [ ] **5. 频道切换实测**：Enter 打开聊天后切「全体」——Tab？Shift+Enter？`/all`？填进
      `chat.channel_keys`（写法见 §3.3）
- [ ] **6. 端到端实发**：先去 `--dry-run` 跑一次，再单独验发送链路
      `python main.py --test-send "测试一下"`（不抓屏、不用标定，守卫照常生效）。判据：游戏内聊天框
      真的出现这句话；若没出现，按顺序查 前台窗口 → `input_method="clipboard"` → `channel_keys` →
      `type_delay`
- [ ] **7. 全链路**：去掉 `--dry-run` 正常跑一次，判据：开大后游戏内公屏出现台词（不是半句/乱码）
- [ ] 实测大招图标在不同英雄间的位置一致性（若个别英雄 R 槽位置不同，考虑多 ROI 支持）

## 5. 维护要点

| 场景 | 处理 |
|------|------|
| 改分辨率/UI 缩放 | 重新标定（`python calibrate.py`），再跑 `--check` |
| 游戏版本更新 | 技能栏布局一般不变；若变，重新标定即可，无需改代码 |
| 系统禁言/举报 | 降低触发频率、扩大台词池随机范围 |
| dxcam 驱动异常 | 检查显卡驱动 DDA 支持；`capture.mode` 可临时改 mss（窗口化时） |
| 换机器 | 分辨率/窗口位置变了就重新标定；窗口标题变了更新 `chat.foreground_titles` |
| 误触发太多 | 提高 `detect.threshold`、加大 `confirm_frames`/`lock_seconds` |
| 热键与游戏冲突 | 改 `config.json` 的 `hotkeys`，或启动加 `--no-hotkeys` |
| 检测一直不触发 | 先 `calibrate.py --check`，再 `main.py --dry-run --stats 30` 看 ready/cd 分数 |
| 触发但没喊出来 | 看日志是不是「前台窗口 … 不在白名单」；`--list-windows` 查标题 |

## 6. 交付模块清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `capture.py` | ✅ | dxcam 优先，mss 回退，无后端时报错退出；非阻塞 + 末帧缓存 + 限频告警 |
| `detect.py` | ✅ | 双基准模板匹配 + 多帧确认 + 下降沿 + 防抖；取 max 打分；零方差/NaN 防护 |
| `guard.py` | ✅ | 前台窗口标题校验（fail-closed）+ 轮询式全局热键；纯 ctypes，非 Windows 降级 |
| `sender.py` | ✅ | 聊天链路，频道/组合键配置化，键盘可注入；`unicode`/`clipboard` 双输入法 |
| `config.json` | ✅ | 参数全外置；缺键由 `deep_merge` 补默认值 |
| `calibrate.py` | ✅ | 标定工具（GUI + 离线双模式）+ `--check` 体检与建议阈值 |
| `main.py` | ✅ | 主循环入口 + 日志 + 统计 + 热键 + `--self-test`/`--dry-run`/`--list-windows` |
| `logsetup.py` | ✅ | 日志系统 |
| `paths.py` | ✅ | 路径规则：源码/exe 两套根目录，`ensure_config` 首次释放默认配置 |
| `requirements*.txt` | ✅ | 跨平台依赖（Windows 标记） |
| `tests/test_smoke.py` | ✅ | 65 个离线冒烟测试 |
| `.github/workflows/ci.yml` | ✅ | CI：Linux 启动自检 + Windows 依赖链 |
| `.github/workflows/release.yml` | ✅ | 推 `v*` tag 打单文件 exe 并发布到 Releases |
| `README.md` | ✅ | 用户文档（含安全护栏/参数表/排错表） |
| `AGENTS.md` | ✅ | 开发约束：红线、分层职责、必跑检查、文档同步表、已知坑 |
| `docs/FIRST_RUN.md` | ✅ | 真机首验陪跑清单（8 步，每步带判据与失败对照表） |

## 7. 环境依赖

- Python 3.8+（CI 用 3.11；本机已验证 3.12），Windows 10+（dxcam 需 Windows 8+ DDA）
- `pip install -r requirements.txt`（Windows 自动装 dxcam/pydirectinput）
- 建议用仓库内虚拟环境（`.venv/` 已 gitignore）：
  `python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt`
- 需管理员权限运行（部分环境输入模拟需要）
- 打包（可选，发版才用）：`pip install "pyinstaller>=6.6"`，命令与产物规则见 README「自己打包」；
  CI 用 Python 3.11 打 windows-x64 单文件，产物约 61MB（opencv + numpy 占大头）

## 8. 联系方式/背景

- 项目仓库：GitHub `yehuoshun/300-shout`（**public**）
- 技术决策人：老板（yehuoshun）
- 首次交付：2026-10-13
- v2 修订：2026-11-11（补 main/calibrate/logsetup + 修 3 处缺陷 + 测试与 CI）
- v3 修订：2026-11-12（安全护栏 + 健壮性 + 标定体检，测试 12 → 28）

## 9. 变更记录

### v3（2026-11-12）「安全试跑 + 健壮性」

改之前已确认的 v2 缺陷：①主循环会向任意前台窗口打字；②无演练模式、无相似度输出；
③单帧判据易被特效骗、纯色基准图让检测静默全灭；④阻塞抓帧在静态画面卡死主循环；
⑤无热键止血；⑥配置校验/默认值升级缺失；⑦标定没有体检。

1. **新增** `guard.py`：`foreground_title()` / `title_matches()`（fail-closed 子串白名单）、
   `list_windows()`、`HotkeyWatcher`（`GetAsyncKeyState` 轮询 + 边沿检测，不装钩子不起线程）；
   非 Windows 全部安全降级
2. **检测** 加 `confirm_frames`（默认 2，多帧确认）、`status()` 观测接口；构造期纯色/零方差告警；
   NaN 归零（OpenCV 5.0 对零方差返回 1.0 而非 NaN，同样会导致永不触发，故按方差判断）
3. **捕获** 改非阻塞 `dxcam.grab()` + 末帧缓存（静态画面不再挂死主循环）+ 抓帧异常 try/except
   与限频告警（`_WARN_EVERY=100`）
4. **发送** 新增 `input_method="clipboard"`（`CF_UNICODETEXT` + Ctrl+V，纯 ctypes），兜底
   DirectInput 吞字；未知取值回退 unicode 并告警
5. **入口** 新增 `--dry-run` / `--stats [SEC]` / `--no-guard` / `--no-hotkeys` / `--list-windows`
   / `--version`；`DEFAULT_CFG` + `deep_merge`（老配置免手工升级）；`validate_config`（阻断）
   + `config_warnings`（提醒）；`pick_phrase` 避免与上一条重复；`crop_roi` 越界夹取
6. **标定** 新增 `--check` 体检（ROI/基准尺寸一致性、纹理方差、两态可分性、建议阈值），
   GUI 标定结束后自动体检
7. **测试** 12 → 45 项：多帧确认、零方差告警、NaN 归零、剪贴板发送、标题匹配 fail-closed、
   热键边沿、深合并、配置校验/提醒、台词去重、ROI 夹取、标定体检三态、按 fps 补节奏、
   `--test-send` 三态（拦下/发出/发送失败）、发送异常不崩循环、标定预览缩放与坐标换算、
   文字前缀拼接与校验、标定抓屏走配置显示器、`--check-guard` 三态；
   自检增加「守卫拦截」「F9 退出」两个分支
8. **文档** README 重写（安全护栏、参数表、排错表）；HANDOVER 补 §3.4 安全护栏与新待办
9. **修复（提交后补）** 非阻塞抓帧让主循环少了天然节流，静止画面下 ~30fps 空转吃 CPU：
   `run()` 按 `capture.fps` 计算 `frame_interval`，每轮（含空帧路径）用 `_pace(tick)` 补足间隔；
   实测 20 帧 @10fps = 2.02s。`--stats` 不给时改读 `stats_interval`（默认 60s）
10. **新增** `--test-send TEXT` 单发测试（不抓屏、免标定，修复「检测正常但发不出去」最难定位的一段）；
    主循环内发送异常改为捕获计数（`send_errors`），不再让进程崩掉
11. **修复** 标定框选在 2K/4K 下不可用：`cv2.selectROI` 直接弹原图会超出屏幕（边角拖不到）。
    新增 `_display_scale`（限 1600×900 预览）+ `_scale_roi_back`（坐标换算回原分辨率，
    并夹取到画面内、拒绝 <8px 选区）
12. **新增** `chat.text_prefix`（默认空）：拼在台词前的文字前缀，应对「靠 `/all ` 这类前缀切公屏」
    的频道机制，免得再去改代码
13. **修复** 标定抓屏没读 `capture.output_idx`（写死默认显示器）：多显示器下 ROI 会全错。
    `_grab_screen(config_path)` 现在按配置走，配置读不出来也不影响标定
14. **新增** `--check-guard`（只读前台标题、不发键，判断白名单是否命中）与
    `docs/FIRST_RUN.md`（真机首验陪跑清单：8 步，每步带判据与失败对照表）
15. **新增** 根目录 `AGENTS.md`：给 AI 编码代理/后来者的硬约束——六条红线（纯图像识别、不主动提权、
    前台校验 fail-closed、未标定不发送、非必要不常驻、只用公开抓屏 API）、分层职责边界、
    配置三件套、测试离线化要求、"改完必跑"三条命令、文档同步表、七条已知坑。
    顺带修正 README/HANDOVER 里过期的测试数（28 → 45）与裸 `python` 命令
16. **修复** 打包成 exe 后配置/日志会丢：新增 `paths.py`（`app_root()`/`bundle_dir()`/`ensure_config()`），
    `main.load_config()` 首次运行释放内置 `config.json`，`setup_logging` 改用 `LOGS_DIR`。
    冻结后 `__file__` 在临时解包目录（退出即删），原来那套 `ROOT = dirname(__file__)` 必须换掉
17. **新增** `.github/workflows/release.yml`：推 `v*` tag 自动 PyInstaller 打单文件 exe、跑 `exe --self-test`、
    发到 Releases（附 sha256 与 zip）；tag 与 `main.VERSION` 不一致时直接失败
18. **新增** `--calibrate` / `--calibrate-check`：把标定工具编进 exe，打包版用户不必再装 Python
19. **新增** `ui.py` 图形控制面板（v3.1）：双击 exe 无参数即开窗——状态灯与实时 `ready`/`cd`
    分数、开始/停止、一键标定、台词池编辑、日志滚动区、发送测试。要点：
    `main.run()` 加了 `stop_event` 与 `on_event` 回调（界面靠它刷新，不再读主循环内部变量），
    组装逻辑抽成 `main.build_runtime()` 给 CLI 与面板共用，避免两套逻辑漂移；
    `ui` 顶层不 import tkinter（CI 无图形环境也能 `import ui`），面板逻辑拆成
    `fmt_score/status_view/PhraseBook/Session` 这些可测纯函数；界面回调异常被 `_emit` 吞掉并记日志，
    界面炸不影响识别。窗口关闭即 `Session.stop()`，不留后台线程

### v2（2026-11-11）
1. **补齐** `main.py`（入口 + 日志 + 主循环 + `--self-test`）、`calibrate.py`（标定，GUI/离线双模式）、`logsetup.py`
2. **修** detect 取 `[0][0]` 改为 `res.max()`（ROI 尺寸与基准不一致时不再静默失准）
3. **修** detect `_prev_ready` 初值 True → False（启动首帧不再误触发）
4. **修** sender 组合键：新增 `"shift+enter"` 写法（keyDown/keyUp 包夹）
5. **增强** capture 无后端时报错退出、mss 抓帧异常不再崩
6. **新增** requirements、tests（12 项）、GitHub Actions CI
