# AGENTS.md — 300-shout 开发约束

本文件是给 AI 编码代理和未来维护者的硬约束。动手前通读一遍；与既有代码冲突时以本文件为准。
确需突破某一条时，先征得维护者同意，并在提交信息里写明理由与影响面。

## 1. 项目定位

在 Windows 游戏「300英雄」里，玩家按下 R 开大招的那一刻，自动在公屏喊一句台词。

- 唯一输入：屏幕像素。
- 唯一输出：模拟键盘输入（聊天栏打字 + 回车）。
- 零进程接触：不碰游戏进程、不读它的内存、不动它的文件。

技术栈刻意保持最薄：`numpy` + `opencv` + `mss`/`dxcam` + `pydirectinput` + 标准库。
没有模型、没有服务端、没有前端。一个 `pip install -r requirements.txt` 就能跑。

## 2. 红线（违反即回滚，没有"临时"例外）

1. **纯图像识别路线**。禁止读游戏内存、注入 DLL/线程、API hook、加载驱动、抓包改包、附加调试器、读取游戏进程句柄。
2. **不主动提权**。程序不写 `requireAdministrator` 清单、不自动 UAC 提权、不用计划任务/服务绕过 UAC。
   若游戏本身以管理员运行、导致 `SendInput` 被 UIPI 拦下，由用户自行决定是否手动"以管理员身份运行"；
   代码里不做任何提权动作。不使用内核级输入模拟（Interception、驱动注入等），只用 `SendInput` 这一层
   （pydirectinput 或 ctypes）。
3. **前台校验 fail-closed**。`require_foreground` 默认 true；`title_matches()` 在标题为 None/空串时返回 False（宁可不发，不可错发）。不允许新增"默认跳过守卫"的分支；`--no-guard` 仅限演练与单发测试，使用时必须留显式告警日志。
4. **未标定不得正式发送**。`validate_config()` 里的阻断项不许为了"先跑起来"被删掉或降级成 warning。
5. **非必要不常驻**。不做守护进程、系统服务、开机自启、文件监听、后台任务队列。程序形态固定为「用户显式启动 → 主循环 → F9 退出（或关窗）的一次性进程；要定时就交给外部调度器。
   图形面板（`ui.py`）也是这个形态：点开才有、关窗即退，不做托盘/自启/后台服务。
6. **抓屏只用公开 API**（Desktop Duplication / `mss`）。不 hook 显卡驱动、不读游戏私有呈现表面。

## 3. 环境（本机已配好，照抄）

- 解释器只有一个：`.venv/Scripts/python.exe`（仓库内虚拟环境）。
  **裸 `python` 是 Microsoft Store 的桩，退出码 9009**——用它跑脚本会静默失败，文件看起来"没改"。
- 测试：`./.venv/Scripts/python.exe -m pytest -q`（当前 72 项，要求 < 2 秒；其中面板菜单那 1 项要真窗口，CI 的 ubuntu 会自动 skip）
- 语法：`./.venv/Scripts/python.exe -m compileall -q .`
- 自检：`./.venv/Scripts/python.exe main.py --self-test`
- 依赖：`requirements.txt`（跨平台分段：Windows 装 dxcam/pydirectinput，其它平台自动只装可测部分）+ `requirements-dev.txt`。
  新增依赖必须能在 CI 的 ubuntu 与 windows 两个 job 上都装上；不要在运行路径里引入 PyQt/torch/onnx 这类重家伙。

## 4. 分层与代码约定

职责边界（越界就是 bug）：

| 文件 | 只负责 |
|---|---|
| `capture.py` | 出帧：非阻塞 grab、末帧缓存、限频告警 |
| `detect.py` | 判定：模板匹配、多帧确认、上升沿、防抖锁 |
| `guard.py` | 前台窗口标题匹配 + 全局热键（纯 ctypes） |
| `sender.py` | 聊天按键序列（unicode / clipboard 两种输入法） |
| `calibrate.py` | 标定 + 体检 |
| `main.py` | 编排：配置、主循环、统计、自检、CLI、组装（`build_runtime`，CLI 与面板共用） |
| `ui.py` | 展示与下发「开始/停止」：Tkinter 单窗口，控件自绘（圆角卡片/按钮/进度条）+ 四套可切换外观（`STYLES`，选完写回 `ui.style`）；顶层不 import tkinter，纯逻辑可离线测 |

- **语言**：注释、docstring、日志、提交信息一律中文；标识符英文 `snake_case`。
- **日志**：`logging.getLogger("300-shout.<模块名>")`，模块内不自己配 handler（统一走 `logsetup`）。
- **可测试性**：时间、随机数、睡眠、抓屏、发键都必须可注入（参数默认值是真实实现）。`run()` 里不要直接依赖无法替代的外部副作用。
- **主循环补节奏**：每帧按 `1 / capture.fps` 补足等待（`_pace`）。禁止忙等——空闲时几乎不占 CPU 是硬要求。
- **配置三件套**：任何新键必须 ① 进 `DEFAULT_CFG` ② 经 `deep_merge` 兼容老配置 ③ 在 `validate_config()` 或 `config_warnings()` 里有明确处理，再加 ④ README 配置表 ⑤ `config.json` 示例值。老用户的配置不允许需要手工升级才能运行。
- **配置文件只增量写**：标定只改 `roi` 与 `refs/`，不许整份覆盖用户手填的 `chat` / `phrases` / `detect`。
- **组合键语义不可混**：`"shift+enter"` 是一个组合（keyDown→press→keyUp 包夹）；`["shift", "enter"]` 是两个独立按键。改动这段前先看 README「频道切换」。
- **检测判据**：单帧不下结论，走 `confirm_frames` 多帧确认；比较分数前必须处理非有限值（NaN/Inf）。

## 5. 测试规矩

- 先写能复现的断言，再改实现。修 bug 的提交里必须带上那条测试。
- 测试必须离线：不碰真实屏幕、不发真实按键、不 `sleep` 真实等待（用注入的 `clock` / `sleeper`）。整套 < 2 秒是硬指标。
- 测试数量变化要同步到 README「测试与 CI」和 `docs/HANDOVER.md` §9。

## 6. 改完必须跑的（顺序固定）

```bash
cd /d/DeepseekWork/300-shout
./.venv/Scripts/python.exe -m pytest -q \
  && ./.venv/Scripts/python.exe -m compileall -q . \
  && ./.venv/Scripts/python.exe main.py --self-test
```

动了 CLI 或配置的话再补：`--version`、`--check-guard`，必要时 `--dry-run --frames 30` 冒烟。

## 6.1 提交与推送（铁律）

- 提交信息中文，格式 `范围: 改了什么(为什么)`。范围用模块或主题词：
  `detect` / `capture` / `guard` / `sender` / `calibrate` / `main` / `ci` / `docs` / `test` / `chore`。
- **一修改一 push**：每完成一处改动就 `git commit` + `git push origin main`，不许攒着。
  同时干了多件事要拆成多个 commit 分别推——一个 commit 只说一件事，回溯时才能精确 revert。
- push 失败（网络 / 凭据 / 远端拒绝）**当场贴出原文错误并停下**，不许静默跳过、不许改成本地攒着。
- 判断 push 成功不看退出码（PowerShell 5.1 会把 `To github.com:...` 当 NativeCommandError 返回 1）。
  以 `git status -sb` 显示 `## main...origin/main`（无 `ahead`）或
  `git log --oneline -1 origin/main` 等于本地 HEAD 为准。
- 动仓库前先看 `git status` + `git log --oneline -3` + `git remote -v`。
  **发现工作区有不是自己改的文件，先问，别 `git add -A` 一键全加**（同一仓库可能有并行会话在改）。
- 只加自己改的具体文件：`git add <path> ...`，不用 `git add -A` 兜底。
- **禁止**替用户执行 `git reset --hard` / `git checkout -- .` / `git clean -fd` / `git push --force`。
  需要回退时只做 `git revert`（留痕），或先问。
- git 操作只在项目目录内做，不跨仓库 `git add`。

## 7. 文档同步表

| 改了什么 | 必须更新 |
|---|---|
| 新 CLI 参数 | README「命令行参数」+ HANDOVER §3 |
| 新配置键 | README「配置说明」+ `config.json` + `DEFAULT_CFG` |
| 行为/判据变化 | HANDOVER 对应 §3.x + §9 变更记录 |
| 依赖或环境 | `requirements*.txt` + HANDOVER §7 + CI |
| 需要真机才能验的东西 | HANDOVER §4 待办（写清判据）+ `docs/FIRST_RUN.md` |

## 8. 关于真机

没有游戏画面、没有用户回传日志时，不允许声称"已跑通""游戏里已经喊出来了"。
可以说到：「离线链路已通、真机待验」。真机结论只有在拿到日志后才能写进 HANDOVER。

## 9. 已知坑（踩过的，别再踩）

- `run()` 返回的是 `fired`（守卫放行的次数），**不是**被拦下的次数。自检里用错过一次。
- 抓帧改成非阻塞后如果不补节奏，静止画面下主循环会空转吃满 CPU。
- 标定框选必须先限尺寸预览（1600×900）再把坐标换算回原分辨率，否则 2K/4K 屏上选区不可用。
- 抓屏要按 `capture.output_idx` 走；多显示器下默认主屏会抓错屏。
- `cv2.matchTemplate(..., TM_CCOEFF_NORMED)` 对零方差模板返回 **1.0**（不是 NaN）。纯色基准图的真实后果是"永远判定可用、永不触发"，所以构造期用 `np.std(ref) < 1.0` 告警。
- 不要对已有目录做"先删后建"式覆盖写。本项目标定只写自己的 `refs/` 与 `config.json` 的 `roi` 字段。
- 文档里的测试数、行号、参数名会过期；发版前 grep 一遍 `28 个`、`42 项` 这类旧数字。
