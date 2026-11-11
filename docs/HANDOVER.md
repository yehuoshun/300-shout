# 300 Shout 交接文档

> 用途：后续接手者快速上手。背景、决策、待办、维护要点一次说清。

## 1. 项目背景

- 需求：300英雄（类 LOL 端游）开大招（R 技能）时，自动在公屏发送预设台词
- 动机：旧有闭源插件不可用，自建开源替代
- 状态：**v2 补齐入口与标定工具，CI 已可验证启动；真机（游戏内实发）待人工实测**

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
- 若未来切窗口化，`capture.mode` 改 mss 即可，代码已内置回退
- 两种后端都不可用时 `backend == "none"`，入口直接报错退出（不会静默空转）

### 3.2 大招检测

- 技能栏 QWER 布局，大招固定 R 槽（绝大多数英雄）
- 检测逻辑：对「可用态」基准图做 `matchTemplate`，相似度骤降 = CD 遮罩出现 = 施放瞬间
- 双基准（可用/CD）对比提升抗误判
- 上升沿 + 3s 防抖锁，防连点重复触发
- **匹配打分取响应矩阵最大值**（`res.max()`），兼容 ROI 与基准图尺寸不一致的情况
- **启动首帧不触发**（`_prev_ready` 初值 False），避免刚进图误喊一次
- ROI 尺寸小于基准图 → 判为不可靠并告警（提示重新标定），不静默失准

### 3.3 聊天发送

- `pydirectinput.write` 走 KEYEVENTF_UNICODE，中文无需输入法
- 频道切换序列 `chat.channel_keys` 配置化
- **组合键**写作 `"shift+enter"`，内部 `keyDown/keyUp` 包夹；`["shift","enter"]` 是两个独立按键，**不是**组合键

### 3.4 日志

- `logsetup.setup_logging()`：控制台 + 文件（`logs/300-shout.log`，2MB × 3 轮转）
- 各模块用 `logging.getLogger("300-shout.<模块>")`，统一挂父 logger

## 4. 待办（接手者第一件事）

- [ ] **实测 300英雄聊天频道切换方式**（Enter 打开聊天后切"全体"：Tab / Shift+Enter / /all？），填入 `config.json` 的 `chat.channel_keys`
- [ ] 实测大招图标在不同英雄间的位置一致性（若个别英雄 R 槽位置不同，考虑多 ROI 支持）
- [ ] 标定流程在真机跑通一遍（`calibrate.py` GUI 部分需真机确认）
- [ ] 真机端到端：标定 → main.py 运行 → 确认游戏内真的喊出来

## 5. 维护要点

| 场景 | 处理 |
|------|------|
| 改分辨率/UI 缩放 | 重新标定（`python calibrate.py`） |
| 游戏版本更新 | 技能栏布局一般不变；若变，重新标定即可，无需改代码 |
| 系统禁言/举报 | 降低触发频率、扩大台词池随机范围 |
| dxcam 驱动异常 | 检查显卡驱动 DDA 支持；`capture.mode` 可临时改 mss（窗口化时） |
| 换机器 | 分辨率/窗口位置变了就重新标定 |

## 6. 交付模块清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `capture.py` | ✅ | dxcam 优先，mss 回退，无后端时报错退出 |
| `detect.py` | ✅ | 双基准模板匹配 + 上升沿 + 防抖；取 max 打分 |
| `sender.py` | ✅ | 聊天链路，频道/组合键配置化，键盘可注入 |
| `config.json` | ✅ | 参数全外置 |
| `calibrate.py` | ✅ | 标定工具（GUI + 离线双模式） |
| `main.py` | ✅ | 主循环入口 + 日志 + `--self-test` |
| `logsetup.py` | ✅ | 日志系统 |
| `requirements*.txt` | ✅ | 跨平台依赖（Windows 标记） |
| `tests/test_smoke.py` | ✅ | 12 个离线冒烟测试 |
| `.github/workflows/ci.yml` | ✅ | CI：Linux 启动自检 + Windows 依赖链 |
| `README.md` | ✅ | 用户文档 |

## 7. 环境依赖

- Python 3.8+（CI 用 3.11），Windows 10+（dxcam 需 Windows 8+ DDA）
- `pip install -r requirements.txt`（Windows 自动装 dxcam/pydirectinput）
- 需管理员权限运行（部分环境 OpenProcess/输入模拟需要）

## 8. 联系方式/背景

- 项目仓库：GitHub `yehuoshun/300-shout`（**public**）
- 技术决策人：老板（yehuoshun）
- 首次交付：2026-10-13
- v2 修订：2026-11-11（补 main/calibrate/logsetup + 修 3 处缺陷 + 测试与 CI）

## 9. 变更记录

### v2（2026-11-11）
1. **补齐** `main.py`（入口 + 日志 + 主循环 + `--self-test`）、`calibrate.py`（标定，GUI/离线双模式）、`logsetup.py`
2. **修** detect 取 `[0][0]` 改为 `res.max()`（ROI 尺寸与基准不一致时不再静默失准）
3. **修** detect `_prev_ready` 初值 True → False（启动首帧不再误触发）
4. **修** sender 组合键：新增 `"shift+enter"` 写法（keyDown/keyUp 包夹）
5. **增强** capture 无后端时报错退出、mss 抓帧异常不再崩
6. **新增** requirements、tests（12 项）、GitHub Actions CI
