"""图形控制面板（Tkinter）：双击 exe 就能开，关窗即退。

设计边界（见 AGENTS.md 红线 5「非必要不常驻」）：
- 只在用户点开时存在，关窗即退出；不做托盘、不自启、不开后台服务、不联网。
- 识别主循环仍然只有一份，跑在 ``main.run()`` 里（工作线程），面板只负责展示
  与下发「开始 / 停止」。所有控件操作都在主线程，工作线程只往队列里塞事件。

tkinter 延迟导入：无图形环境的 CI 上 ``import ui`` 不能炸，所以本模块顶层
不 import tkinter，纯逻辑（格式化 / 台词池 / 会话）单独可测。
"""
import json
import logging
import math
import os
import queue
import threading

log = logging.getLogger("300-shout.ui")

LEVELS = ("idle", "ok", "run", "warn", "bad")

# 面板配色（Tk 只认颜色字符串，抽出来便于统一改）
COLORS = {
    "idle": "#5f6368",
    "ok": "#1f8f4e",
    "run": "#1a73e8",
    "warn": "#b06000",
    "bad": "#c5221f",
}


# ---------------------------------------------------------------- 纯逻辑（可测）
def fmt_score(value) -> str:
    """相似度分数显示：None/NaN → em dash，其余保留两位。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(v) or math.isinf(v):
        return "—"
    return f"{v:.2f}"


def score_line(ready=None, cd=None, threshold=None) -> str:
    return f"ready {fmt_score(ready)}   cd {fmt_score(cd)}   阈值 {fmt_score(threshold)}"


def status_view(*, calibrated: bool, running: bool, ready=None, threshold=None,
                locked: bool = False) -> tuple:
    """把内部状态翻成人话，返回 (文字, 级别)。级别取 LEVELS 之一，用于配色。"""
    if not calibrated:
        return ("未标定 · 先点「标定 R 槽」框选大招图标", "warn")
    if not running:
        return ("已停止 · 点「开始监控」开始识别", "idle")
    if ready is None:
        return ("监控中 · 等待画面……", "run")
    if locked:
        return ("刚触发过 · 冷却中（防连发），等它过去", "ok")
    th = 0.8 if threshold is None else float(threshold)
    r = float(ready)
    if r >= th:
        return (f"待命中 · 大招已就绪（{r:.2f} ≥ {th:.2f}），开大就会喊", "ok")
    return (f"监控中 · 大招未就绪（{r:.2f} < {th:.2f}）", "run")


class PhraseBook:
    """台词池：增删查，并负责落盘回 config.json。"""

    MAX = 30
    MAX_LEN = 60

    def __init__(self, phrases=()):
        self.items = [str(p).strip() for p in (phrases or []) if str(p).strip()]

    def __len__(self) -> int:
        return len(self.items)

    def add(self, text):
        """返回 (是否成功, 说明或新增的那条)。"""
        t = (text or "").strip()
        if not t:
            return False, "台词不能为空"
        if len(t) > self.MAX_LEN:
            return False, f"太长了（上限 {self.MAX_LEN} 字）"
        if t in self.items:
            return False, "已经有这条了"
        if len(self.items) >= self.MAX:
            return False, f"最多 {self.MAX} 条"
        self.items.append(t)
        return True, t

    def remove(self, index):
        if 0 <= index < len(self.items):
            return self.items.pop(index)
        return None

    def replace(self, index, text):
        t = (text or "").strip()
        if not t or not 0 <= index < len(self.items) or len(t) > self.MAX_LEN:
            return False
        self.items[index] = t
        return True

    def as_list(self):
        return list(self.items)


def save_phrases(config_path: str, phrases) -> bool:
    """只改 config.json 的 phrases 字段，其余键原样保留（写临时文件再替换）。"""
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("读配置失败，台词未保存：%s", exc)
        return False
    data = dict(data or {})
    data["phrases"] = [str(p) for p in phrases]
    tmp = config_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, config_path)
    except OSError as exc:
        log.warning("写配置失败，台词未保存：%s", exc)
        return False
    return True


class Session:
    """一次「开始监控」= 一个工作线程 + 一个停止信号。"""

    def __init__(self, runner):
        self._runner = runner
        self._thread = None
        self._stop = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, on_event=None) -> bool:
        if self.running:
            return False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._target,
                                        args=(self._stop, on_event),
                                        name="300-shout-run", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> bool:
        if self._stop is None:
            return False
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        return True

    def _target(self, stop_event, on_event):
        try:
            self._runner(stop_event, on_event)
        except SystemExit as exc:  # 捕获层判定异常时会 raise SystemExit(3)
            log.error("主循环异常退出（code=%s）。", exc.code)
            if on_event:
                on_event("error", {"text": f"主循环异常退出（code={exc.code}）"})
        except Exception as exc:  # noqa: BLE001 线程里必须兜住，否则面板无感
            log.exception("主循环崩溃：%s", exc)
            if on_event:
                on_event("error", {"text": f"主循环崩溃：{exc}"})


class QueueHandler(logging.Handler):
    """把日志转进队列，由面板主线程渲染（logging 不能直接碰 Tk）。"""

    def __init__(self, sink: queue.Queue):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        try:
            self.sink.put(("log", self.format(record)))
        except Exception:  # noqa: BLE001
            self.handleError(record)


# ---------------------------------------------------------------- 面板
class Panel:
    """Tk 面板。构造时才需要 tkinter 模块对象，便于无 Tk 环境下测试纯逻辑。"""

    MAX_LOG_LINES = 400

    def __init__(self, tk, config_path: str):
        import main as main_mod

        self.tk = tk
        self.main = main_mod
        self.config_path = config_path
        self.cfg = main_mod.load_config(config_path)
        self.book = PhraseBook(self.cfg.get("phrases") or [])
        self.session = Session(self._worker)
        self.events = queue.Queue()
        self.scores = {"ready": None, "cd": None, "threshold": None, "locked": False}
        self.counters = {"frames": 0, "triggers": 0, "sent": 0, "blocked": 0, "errors": 0}
        self.handler = None
        self._build()
        self._attach_logging()
        self.refresh()
        self.append_log("面板已就绪。第一次用：①「标定 R 槽」框选游戏里的大招图标 "
                        "②切到游戏点「开始监控」③开大验证。F8 暂停/继续，F9 退出。")
        self.root.after(80, self._pump)

    # -- 构建界面 --------------------------------------------------
    def _build(self):
        tk = self.tk
        self.root = tk.Tk()
        self.root.title("300 Shout · 自动公屏喊话")
        self.root.geometry("860x640")
        self.root.minsize(760, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        top = tk.Frame(self.root, padx=14, pady=12)
        top.pack(fill="x")
        self.status_var = tk.StringVar(value="")
        self.score_var = tk.StringVar(value="")
        self.status_label = tk.Label(top, textvariable=self.status_var, anchor="w",
                                     font=("Microsoft YaHei UI", 15, "bold"))
        self.status_label.pack(fill="x")
        tk.Label(top, textvariable=self.score_var, anchor="w", fg="#666",
                 font=("Consolas", 10)).pack(fill="x", pady=(4, 0))

        bar = tk.Frame(self.root, padx=14)
        bar.pack(fill="x")
        self.btn_start = tk.Button(bar, text="开始监控", width=11, command=self.on_start,
                                   bg="#1f8f4e", fg="white",
                                   activebackground="#17703d", activeforeground="white")
        self.btn_stop = tk.Button(bar, text="停止", width=8, command=self.on_stop)
        self.btn_calib = tk.Button(bar, text="标定 R 槽", width=11, command=self.on_calibrate)
        self.btn_check = tk.Button(bar, text="标定体检", width=11, command=self.on_calibrate_check)
        self.btn_test = tk.Button(bar, text="发送测试", width=11, command=self.on_test_send)
        self.btn_cfg = tk.Button(bar, text="打开配置", width=11, command=self.on_open_config)
        for b in (self.btn_start, self.btn_stop, self.btn_calib, self.btn_check,
                  self.btn_test, self.btn_cfg):
            b.pack(side="left", padx=(0, 6))

        body = tk.Frame(self.root, padx=14, pady=10)
        body.pack(fill="both", expand=True)

        # 左右两栏按 2:3 分宽，窗口变大时一起变宽，中间不留空档
        body.columnconfigure(0, weight=2, uniform="col")
        body.columnconfigure(1, weight=3, uniform="col")
        body.rowconfigure(0, weight=1)

        left = tk.LabelFrame(body, text="台词池（随机喊一条，不连喊同一条）", padx=8, pady=8)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(left, height=12, exportselection=False)
        sb = tk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        row = tk.Frame(left)
        row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.phrase_var = tk.StringVar()
        entry = tk.Entry(row, textvariable=self.phrase_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self.on_add_phrase())
        tk.Button(row, text="添加", width=6, command=self.on_add_phrase).pack(side="left", padx=4)
        tk.Button(row, text="删除", width=6, command=self.on_del_phrase).pack(side="left")

        right = tk.LabelFrame(body, text="运行日志", padx=8, pady=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self.log_text = tk.Text(right, height=12, wrap="word", state="disabled",
                                font=("Consolas", 9))
        lsb = tk.Scrollbar(right, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")

        self.foot_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.foot_var, anchor="w", fg="#666",
                 padx=14, pady=6).pack(fill="x")

    def _attach_logging(self):
        self.handler = QueueHandler(self.events)
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(message)s",
                                                   "%H:%M:%S"))
        self.handler.setLevel(logging.INFO)
        logging.getLogger("300-shout").addHandler(self.handler)

    def mainloop(self):
        self.root.mainloop()

    # -- 状态刷新 --------------------------------------------------
    def calibrated(self) -> bool:
        try:
            return not self.main.validate_config(self.cfg)
        except Exception:  # noqa: BLE001
            return False

    def refresh(self):
        text, level = status_view(calibrated=self.calibrated(),
                                  running=self.session.running,
                                  ready=self.scores["ready"],
                                  threshold=self.scores["threshold"],
                                  locked=self.scores["locked"])
        self.status_var.set(text)
        self.status_label.configure(fg=COLORS.get(level, "#000"))
        self.score_var.set(score_line(self.scores["ready"], self.scores["cd"],
                                      self.scores["threshold"]))
        c = self.counters
        self.foot_var.set(f"帧 {c['frames']} · 触发 {c['triggers']} · 已喊 {c['sent']} · "
                          f"拦下 {c['blocked']} · 失败 {c['errors']}    |    "
                          f"配置 {self.config_path}")
        running = self.session.running
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.btn_calib.configure(state="disabled" if running else "normal")
        self.btn_check.configure(state="disabled" if running else "normal")
        self.btn_test.configure(state="disabled" if running else "normal")
        if not self.listbox_matches():
            self.listbox.delete(0, "end")
            for p in self.book.as_list():
                self.listbox.insert("end", p)

    def listbox_matches(self) -> bool:
        return list(self.listbox.get(0, "end")) == self.book.as_list()

    def append_log(self, line: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > self.MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{lines - self.MAX_LOG_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- 事件泵 ----------------------------------------------------
    def _pump(self):
        dirty = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                dirty = True
                if kind == "log":
                    self.append_log(str(payload))
                elif kind == "tick":
                    self.counters.update({k: payload.get(k, self.counters[k])
                                          for k in self.counters})
                    self.scores.update({"ready": payload.get("ready_score"),
                                        "cd": payload.get("cd_score"),
                                        "threshold": payload.get("threshold"),
                                        "locked": bool(payload.get("locked"))})
                elif kind == "trigger":
                    if payload.get("allowed"):
                        verb = "本应喊" if payload.get("dry") else "已喊"
                        self.append_log(f"→ {verb}：{payload.get('text')}")
                    else:
                        self.append_log(f"→ 前台不是游戏（{payload.get('title')!r}），这次不喊")
                elif kind == "error":
                    self.append_log(f"!! {payload.get('text')}")
                elif kind == "calibrated":
                    self.reload_config()
        except queue.Empty:
            pass
        if dirty:
            self.refresh()
        self.root.after(120, self._pump)

    def reload_config(self):
        self.cfg = self.main.load_config(self.config_path)
        self.book = PhraseBook(self.cfg.get("phrases") or [])
        self.refresh()

    # -- 按钮动作 --------------------------------------------------
    def on_start(self):
        if self.session.running:
            return
        if not self.calibrated():
            self.append_log("配置还不完整，先标定大招图标。")
            from tkinter import messagebox
            if messagebox.askyesno("还没标定", "现在打开标定？框选游戏里的大招图标。",
                                   parent=self.root):
                self.on_calibrate()
            return
        self.session.start(self._on_event)
        self.append_log("开始监控（F8 暂停/继续，F9 退出）。记得让游戏在最前面，"
                        "独占全屏抓不到画面就切无边框窗口。")
        self.refresh()

    def on_stop(self):
        if self.session.stop():
            self.append_log("已停止监控。")
        self.refresh()

    def on_calibrate(self):
        self._run_tool(self._calibrate_worker, "--calibrate")

    def on_calibrate_check(self):
        self._run_tool(self._calibrate_worker, "--calibrate-check")

    def _run_tool(self, target, arg):
        if self.session.running:
            self.append_log("先停止监控再标定。")
            return
        threading.Thread(target=target, args=(arg,), daemon=True).start()

    def _calibrate_worker(self, arg):
        try:
            rc = self.main.main([arg, "--config", self.config_path])
        except SystemExit as exc:
            rc = exc.code
        except Exception as exc:  # noqa: BLE001
            log.exception("标定失败：%s", exc)
            rc = 1
        self.events.put(("log", f"标定工具结束（code={rc}）。"))
        self.events.put(("calibrated", {}))

    def on_test_send(self):
        from tkinter import simpledialog
        text = simpledialog.askstring("发送测试", "要发到游戏里的文本：",
                                      initialvalue="测试一下", parent=self.root)
        if not text:
            return
        threading.Thread(target=self._test_send_worker, args=(text,), daemon=True).start()

    def _test_send_worker(self, text):
        try:
            self.main.main(["--test-send", text, "--config", self.config_path])
        except SystemExit:
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("发送测试失败：%s", exc)

    def on_open_config(self):
        try:
            os.startfile(self.config_path)  # noqa: S606 用户点按钮才打开
        except (OSError, AttributeError) as exc:
            self.append_log(f"打开配置失败：{exc}")

    def on_add_phrase(self):
        ok, msg = self.book.add(self.phrase_var.get())
        if ok:
            self.phrase_var.set("")
            save_phrases(self.config_path, self.book.as_list())
        else:
            self.append_log(f"没加上：{msg}")
        self.refresh()

    def on_del_phrase(self):
        sel = list(self.listbox.curselection())
        if not sel:
            self.append_log("先在列表里选一条要删的。")
            return
        gone = self.book.remove(int(sel[0]))
        if gone:
            save_phrases(self.config_path, self.book.as_list())
            self.append_log(f"删掉了：{gone}")
        self.refresh()

    # -- 工作线程事件 ----------------------------------------------
    def _on_event(self, kind, payload):
        try:
            self.events.put_nowait((kind, dict(payload or {})))
        except Exception:  # noqa: BLE001 事件丢了也不能影响主循环
            pass

    def _worker(self, stop_event, on_event):
        cfg = self.main.load_config(self.config_path)
        problems = self.main.validate_config(cfg)
        if problems:
            if on_event:
                on_event("error", {"text": "配置问题：" + "；".join(problems)})
            return

        def relay(kind, payload):
            if on_event is None:
                return
            data = dict(payload or {})
            if kind == "tick" and det is not None:
                data.update(det.status())
            on_event(kind, data)

        cap = det = None
        cap, det, send_fn, guard_fn, hotkeys = self.main.build_runtime(cfg)
        if getattr(cap, "backend", "none") == "none":
            relay("error", {"text": "没有可用的捕获后端（dxcam 需 Windows）。"})
            return
        log.info("面板启动：捕获后端=%s，台词 %d 条，守卫=%s，热键=%s",
                 cap.backend, len(cfg.get("phrases") or []),
                 "开" if guard_fn else "关",
                 ",".join(hotkeys.keys) if hotkeys else "关")
        self.main.run(cfg, cap, det, send_fn, max_empty_frames=600,
                      guard=guard_fn, hotkeys=hotkeys,
                      stop_event=stop_event, on_event=relay,
                      stats_seconds=max(0.0, float(cfg.get("stats_interval") or 60.0)))

    # -- 关闭 ------------------------------------------------------
    def shutdown(self):
        self.session.stop()
        if self.handler is not None:
            logging.getLogger("300-shout").removeHandler(self.handler)
            self.handler = None

    def on_close(self):
        self.shutdown()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def launch(config_path: str) -> int:
    """打开面板并阻塞到窗口关闭。返回码 0=正常关闭，2=没有 tkinter。"""
    try:
        import tkinter  # noqa: F401 只为探测是否可用
    except ImportError as exc:
        log.error("没有 tkinter（%s），无法开面板；用命令行：main.py --help", exc)
        return 2
    panel = Panel(tkinter, config_path)
    try:
        panel.mainloop()
    except KeyboardInterrupt:
        log.info("面板收到中断，退出。")
    finally:
        panel.shutdown()
    return 0
