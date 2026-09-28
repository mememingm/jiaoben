"""eSIM Profile 状态查询工具的可视化入口。

界面只负责收集查询条件和展示结果，实际请求与安全白名单由
``nexsim_status_checker`` 统一处理。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import nexsim_status_checker as checker


COLORS = {
    "canvas": "#F2F5F5",
    "surface": "#FFFFFF",
    "ink": "#172326",
    "muted": "#667477",
    "line": "#D8E0DF",
    "teal": "#087E8B",
    "teal_dark": "#075E69",
    "teal_soft": "#E1F1F2",
    "green": "#1D7A55",
    "amber": "#9A6813",
    "red": "#B54848",
}


class StatusGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("eSIM Profile Status")
        self.geometry("1360x840")
        self.minsize(1080, 700)
        self.configure(bg=COLORS["canvas"])
        self.protocol("WM_DELETE_WINDOW", self._close)

        desktop = Path.home() / "Desktop"
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.show_password = tk.BooleanVar(value=False)
        self.base_url = tk.StringVar(value="https://admin.nexsimus.com")
        self.output_dir = tk.StringVar(value=str(desktop / "esim-status-results"))
        self.product_id = tk.StringVar()
        self.scope = tk.StringVar(value="all")
        self.search = tk.StringVar()
        self.profile_filter = tk.StringVar(value="全部 Profile 状态")
        self.status_text = tk.StringVar(value="准备就绪")
        self.progress_text = tk.StringVar(value="等待查询")
        self.count_text = tk.StringVar(value="0 条记录")
        self.json_path = tk.StringVar()
        self.csv_path = tk.StringVar()
        self.total_text = tk.StringVar(value="0")
        self.installed_text = tk.StringVar(value="0")
        self.released_text = tk.StringVar(value="0")
        self.failed_text = tk.StringVar(value="0")

        self.rows: list[dict[str, Any]] = []
        self.filtered_rows: list[dict[str, Any]] = []
        self.filter_values: dict[str, str] = {}
        self.result_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.advanced_visible = False

        self._setup_style()
        self._build_ui()
        self.search.trace_add("write", lambda *_: self._apply_filter())
        self.scope.trace_add("write", lambda *_: self._update_scope_state())
        self.after(100, self._poll_results)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background=COLORS["canvas"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure("Header.TFrame", background=COLORS["ink"])
        style.configure("HeaderTitle.TLabel", background=COLORS["ink"], foreground="#FFFFFF",
                        font=("Segoe UI", 20, "bold"))
        style.configure("HeaderMeta.TLabel", background=COLORS["ink"], foreground="#B9C8C8",
                        font=("Segoe UI", 10))
        style.configure("Section.TLabelframe", background=COLORS["surface"],
                        bordercolor=COLORS["line"], relief="solid", borderwidth=1)
        style.configure("Section.TLabelframe.Label", background=COLORS["surface"],
                        foreground=COLORS["ink"], font=("Segoe UI", 10, "bold"))
        style.configure("Body.TLabel", background=COLORS["surface"], foreground=COLORS["ink"],
                        font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=COLORS["surface"], foreground=COLORS["muted"],
                        font=("Segoe UI", 9))
        style.configure("MetricLabel.TLabel", background=COLORS["surface"], foreground=COLORS["muted"],
                        font=("Segoe UI", 9))
        style.configure("MetricValue.TLabel", background=COLORS["surface"], foreground=COLORS["ink"],
                        font=("Segoe UI", 18, "bold"))
        style.configure("Primary.TButton", background=COLORS["teal"], foreground="#FFFFFF",
                        padding=(16, 9), font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", COLORS["teal_dark"]),
                                                   ("disabled", "#A8B9BA")])
        style.configure("Secondary.TButton", background="#FFFFFF", foreground=COLORS["teal_dark"],
                        padding=(10, 7), font=("Segoe UI", 9, "bold"))
        style.map("Secondary.TButton", background=[("active", COLORS["teal_soft"]),
                                                     ("disabled", "#EEF1F1")])
        style.configure("Danger.TButton", background="#FFFFFF", foreground=COLORS["red"],
                        padding=(10, 7), font=("Segoe UI", 9, "bold"))
        style.map("Danger.TButton", background=[("active", "#FCE8E8"),
                                                  ("disabled", "#EEF1F1")])
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 9),
                        background=COLORS["surface"], fieldbackground=COLORS["surface"],
                        bordercolor=COLORS["line"])
        style.configure("Treeview.Heading", background="#E8EFEE", foreground=COLORS["ink"],
                        font=("Segoe UI", 9, "bold"), padding=(7, 7))
        style.map("Treeview", background=[("selected", COLORS["teal_soft"])],
                  foreground=[("selected", COLORS["ink"])])
        style.configure("TEntry", padding=7)
        style.configure("TCombobox", padding=6)
        style.configure("Horizontal.TProgressbar", background=COLORS["teal"],
                        troughcolor="#DDE7E6", bordercolor="#DDE7E6", lightcolor=COLORS["teal"],
                        darkcolor=COLORS["teal"])

    def _build_ui(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(28, 20))
        header.pack(fill="x")
        ttk.Label(header, text="eSIM Profile Status", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(header, text="只读获取 Profile 状态 · 平台原始值完整保留",
                  style="HeaderMeta.TLabel").pack(anchor="w", pady=(6, 0))

        shell = ttk.Frame(self, style="App.TFrame", padding=18)
        shell.pack(fill="both", expand=True)
        panes = ttk.Panedwindow(shell, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes, style="App.TFrame", width=355, padding=(0, 0, 16, 0))
        right = ttk.Frame(panes, style="App.TFrame")
        panes.add(left, weight=0)
        panes.add(right, weight=1)
        self._build_query_panel(left)
        self._build_results_panel(right)

    def _build_query_panel(self, parent: ttk.Frame) -> None:
        connection = ttk.LabelFrame(parent, text="连接", style="Section.TLabelframe", padding=14)
        connection.pack(fill="x", pady=(0, 12))
        connection.columnconfigure(0, weight=1)
        ttk.Label(connection, text="账号", style="Body.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(connection, textvariable=self.username).grid(row=1, column=0, sticky="ew", pady=(4, 12))
        ttk.Label(connection, text="密码", style="Body.TLabel").grid(row=2, column=0, sticky="w")
        self.password_entry = ttk.Entry(connection, textvariable=self.password, show="•")
        self.password_entry.grid(row=3, column=0, sticky="ew", pady=(4, 5))
        ttk.Checkbutton(connection, text="显示密码", variable=self.show_password,
                        command=self._toggle_password).grid(row=4, column=0, sticky="w")
        ttk.Label(connection, text="组织 ID 会在登录后自动识别", style="Muted.TLabel").grid(
            row=5, column=0, sticky="w", pady=(10, 0))

        scope_box = ttk.LabelFrame(parent, text="查询范围", style="Section.TLabelframe", padding=14)
        scope_box.pack(fill="x", pady=(0, 12))
        ttk.Radiobutton(scope_box, text="全部 eSIM 库存", variable=self.scope,
                        value="all").pack(anchor="w")
        ttk.Radiobutton(scope_box, text="指定 ICCID", variable=self.scope,
                        value="selected").pack(anchor="w", pady=(8, 0))
        self.iccid_text = tk.Text(scope_box, height=5, width=30, wrap="none",
                                  font=("Consolas", 9), relief="solid", borderwidth=1,
                                  highlightthickness=0)
        self.iccid_text.pack(fill="x", pady=(9, 0))
        ttk.Label(scope_box, text="每行一个 ICCID，也支持逗号分隔", style="Muted.TLabel").pack(
            anchor="w", pady=(5, 0))

        self.advanced_button = ttk.Button(parent, text="显示高级设置",
                                          style="Secondary.TButton",
                                          command=self._toggle_advanced)
        self.advanced_button.pack(fill="x", pady=(0, 12))
        self.advanced_panel = ttk.LabelFrame(parent, text="高级设置",
                                             style="Section.TLabelframe", padding=14)
        self.advanced_panel.columnconfigure(0, weight=1)
        ttk.Label(self.advanced_panel, text="后台地址", style="Body.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Entry(self.advanced_panel, textvariable=self.base_url).grid(
            row=1, column=0, sticky="ew", pady=(4, 10))
        ttk.Label(self.advanced_panel, text="商品 ID（可选）", style="Body.TLabel").grid(
            row=2, column=0, sticky="w")
        ttk.Entry(self.advanced_panel, textvariable=self.product_id).grid(
            row=3, column=0, sticky="ew", pady=(4, 10))
        ttk.Label(self.advanced_panel, text="结果目录", style="Body.TLabel").grid(
            row=4, column=0, sticky="w")
        output_row = ttk.Frame(self.advanced_panel, style="Surface.TFrame")
        output_row.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        output_row.columnconfigure(0, weight=1)
        ttk.Entry(output_row, textvariable=self.output_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(output_row, text="浏览", style="Secondary.TButton",
                   command=self._choose_output).grid(row=0, column=1, padx=(6, 0))

        action = ttk.Frame(parent, style="App.TFrame")
        action.pack(fill="x", side="bottom")
        self.query_button = ttk.Button(action, text="获取 eSIM 状态", style="Primary.TButton",
                                       command=self._start_query)
        self.query_button.pack(fill="x")
        self.cancel_button = ttk.Button(action, text="取消当前查询", style="Danger.TButton",
                                        command=self._cancel_query, state="disabled")
        self.cancel_button.pack(fill="x", pady=(8, 0))
        ttk.Label(action, text="登录和后续请求均为只读流程", style="Muted.TLabel").pack(
            anchor="center", pady=(10, 0))
        self._update_scope_state()

    def _build_results_panel(self, parent: ttk.Frame) -> None:
        summary = ttk.Frame(parent, style="Surface.TFrame", padding=(16, 12))
        summary.pack(fill="x", pady=(0, 12))
        for index, (label, variable, color) in enumerate((
            ("全部记录", self.total_text, COLORS["ink"]),
            ("已安装 · INSTALLED", self.installed_text, COLORS["green"]),
            ("已释放待下载 · RELEASED", self.released_text, COLORS["amber"]),
            ("Profile 查询失败", self.failed_text, COLORS["red"]),
        )):
            block = ttk.Frame(summary, style="Surface.TFrame", padding=(0, 0, 28, 0))
            block.grid(row=0, column=index, sticky="w")
            ttk.Label(block, text=label, style="MetricLabel.TLabel").pack(anchor="w")
            value_label = ttk.Label(block, textvariable=variable, style="MetricValue.TLabel")
            value_label.configure(foreground=color)
            value_label.pack(anchor="w", pady=(2, 0))

        toolbar = ttk.Frame(parent, style="App.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(toolbar, text="结果筛选", style="Body.TLabel").pack(side="left")
        ttk.Entry(toolbar, textvariable=self.search, width=28).pack(side="left", padx=(9, 8))
        self.filter_combo = ttk.Combobox(toolbar, textvariable=self.profile_filter,
                                         state="readonly", width=25,
                                         values=("全部 Profile 状态",))
        self.filter_combo.pack(side="left")
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _event: self._apply_filter())
        ttk.Button(toolbar, text="打开结果目录", style="Secondary.TButton",
                   command=self._open_output_dir).pack(side="right")

        table_box = ttk.LabelFrame(parent, text="eSIM Profile 状态", style="Section.TLabelframe", padding=10)
        table_box.pack(fill="both", expand=True)
        columns = ("iccid", "status", "profile", "updated", "query")
        content = ttk.Frame(table_box, style="Surface.TFrame")
        content.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(content, columns=columns, show="headings", selectmode="browse")
        headings = {"iccid": "ICCID", "status": "库存 status", "profile": "Profile 状态",
                    "updated": "Profile 更新时间", "query": "查询结果"}
        widths = {"iccid": 230, "status": 120, "profile": 205, "updated": 190, "query": 100}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w", stretch=column == "iccid")
        self.tree.tag_configure("installed", foreground=COLORS["green"])
        self.tree.tag_configure("released", foreground=COLORS["amber"])
        self.tree.tag_configure("failed", foreground=COLORS["red"])
        scroll = ttk.Scrollbar(content, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)

        detail_box = ttk.LabelFrame(parent, text="原始字段", style="Section.TLabelframe", padding=10)
        detail_box.pack(fill="x", pady=(12, 0))
        self.detail = tk.Text(detail_box, height=8, wrap="none", background="#FBFCFC",
                              foreground=COLORS["ink"], relief="flat", font=("Consolas", 9))
        self.detail.pack(fill="x")
        self.detail.configure(state="disabled")

        footer = ttk.Frame(parent, style="App.TFrame")
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, textvariable=self.status_text, style="Muted.TLabel").pack(side="left")
        ttk.Label(footer, textvariable=self.count_text, style="Muted.TLabel").pack(side="right")
        self.progress = ttk.Progressbar(parent, mode="determinate", maximum=1, value=0)
        self.progress.pack(fill="x", pady=(7, 0))
        ttk.Label(parent, textvariable=self.progress_text, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(parent, textvariable=self.json_path, style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        ttk.Label(parent, textvariable=self.csv_path, style="Muted.TLabel").pack(anchor="w")

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_password.get() else "•")

    def _toggle_advanced(self) -> None:
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self.advanced_panel.pack(fill="x", pady=(0, 12), after=self.advanced_button)
            self.advanced_button.configure(text="隐藏高级设置")
        else:
            self.advanced_panel.pack_forget()
            self.advanced_button.configure(text="显示高级设置")

    def _update_scope_state(self) -> None:
        self.iccid_text.configure(state="normal" if self.scope.get() == "selected" else "disabled")

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="选择结果目录")
        if path:
            self.output_dir.set(path)

    def _start_query(self) -> None:
        username = self.username.get().strip()
        password = self.password.get()
        if not username or not password:
            messagebox.showerror("缺少登录信息", "请输入账号和密码。")
            return
        product_text = self.product_id.get().strip()
        if product_text:
            try:
                product_id: int | None = int(product_text)
            except ValueError:
                messagebox.showerror("商品 ID 错误", "商品 ID 必须是整数，或留空。")
                return
        else:
            product_id = None
        output_dir = Path(self.output_dir.get().strip())
        if not output_dir.is_absolute():
            messagebox.showerror("结果目录错误", "结果目录必须是绝对路径。")
            return
        try:
            targets = None
            if self.scope.get() == "selected":
                targets = checker.parse_iccids(self.iccid_text.get("1.0", "end"))
            config = checker.validate_config({
                "base_url": self.base_url.get().strip(),
                "credential_file": None,
                "org_id": None,
                "product_id": product_id,
                "output_dir": str(output_dir),
                "page_size": 100,
                "query_params": {"simType": "ESIM"},
            }, require_credential_file=False)
        except checker.Stop as exc:
            messagebox.showerror("查询配置错误", str(exc))
            return

        self.rows = []
        self.filtered_rows = []
        self._reset_result_view()
        self.cancel_event.clear()
        self.query_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_text.set("正在登录并读取 eSIM 状态……")
        self.progress_text.set("准备连接后台")
        self.progress.configure(value=0, maximum=100)
        self.worker = threading.Thread(
            target=self._query_worker,
            args=(config, (username, password), targets),
            daemon=True,
        )
        self.worker.start()

    def _query_worker(self, config: dict[str, Any], credentials: tuple[str, str],
                      targets: set[str] | None) -> None:
        def progress(stage: str, current: int, total: int) -> None:
            self.result_queue.put(("progress", (stage, current, total)))
        try:
            result = checker.query_status(config, iccids=targets, credentials=credentials,
                                          progress=progress, cancelled=self.cancel_event.is_set)
            self.result_queue.put(("ok", result))
        except Exception as exc:  # UI thread receives a short, safe message.
            self.result_queue.put(("error", str(exc)))

    def _cancel_query(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_text.set("正在停止当前查询……")

    def _poll_results(self) -> None:
        latest_progress: tuple[str, int, int] | None = None
        terminal: tuple[str, Any] | None = None
        while True:
            try:
                kind, value = self.result_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                latest_progress = value
            else:
                terminal = (kind, value)
        if latest_progress is not None:
            stage, current, total = latest_progress
            ratio = min(current / max(total, 1), 1)
            if stage == "inventory":
                percent = 5 + ratio * 15
                label = f"读取库存分页：{current} / {total}"
            elif stage == "profile":
                percent = 20 + ratio * 75
                label = f"读取 Profile 状态：{current} / {total}"
            else:
                percent = 95 + ratio * 5
                label = "正在保存 JSON 和 CSV……"
            self.progress.configure(maximum=100, value=percent)
            self.progress_text.set(f"{label} · {percent:.0f}%")
        if terminal is not None:
            kind, value = terminal
            self.query_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")
            self.worker = None
            if kind == "error":
                self.status_text.set("查询未完成")
                self.progress_text.set(value)
                if value != "查询已取消。":
                    messagebox.showerror("查询失败", value)
            else:
                rows, json_path, csv_path = value
                self.rows = rows
                self.json_path.set(f"JSON：{json_path}")
                self.csv_path.set(f"CSV：{csv_path}")
                self._update_filter_options(rows)
                self.status_text.set("查询完成，结果已保存")
                self.progress_text.set("全部处理完成")
                self.progress.configure(maximum=100, value=100)
                self._update_summary(rows)
                self._apply_filter()
        self.after(100, self._poll_results)

    def _update_filter_options(self, rows: list[dict[str, Any]]) -> None:
        # 后台当前确认的 Profile 原始值只有 INSTALLED、RELEASED；failed 是本地查询结果。
        options = ["全部 Profile 状态", "已安装（INSTALLED）",
                   "已释放待下载（RELEASED）", "查询失败"]
        self.filter_values = {
            "全部 Profile 状态": "__all__",
            "已安装（INSTALLED）": "INSTALLED",
            "已释放待下载（RELEASED）": "RELEASED",
            "查询失败": "__failed__",
        }
        self.filter_combo.configure(values=options)
        self.profile_filter.set(options[0])

    def _update_summary(self, rows: list[dict[str, Any]]) -> None:
        self.total_text.set(str(len(rows)))
        self.installed_text.set(str(sum(row.get("esimProfileStatus") == "INSTALLED" for row in rows)))
        self.released_text.set(str(sum(row.get("esimProfileStatus") == "RELEASED" for row in rows)))
        self.failed_text.set(str(sum(row.get("esimProfileStatusQueryStatus") == "failed" for row in rows)))

    def _apply_filter(self) -> None:
        needle = self.search.get().strip().lower()
        selected = self.filter_values.get(self.profile_filter.get(), "__all__")
        def matches(row: dict[str, Any]) -> bool:
            if needle and needle not in json.dumps(row, ensure_ascii=False).lower():
                return False
            if selected == "__all__":
                return True
            if selected == "__failed__":
                return row.get("esimProfileStatusQueryStatus") == "failed"
            return row.get("esimProfileStatus") == selected
        self.filtered_rows = [row for row in self.rows if matches(row)]
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, row in enumerate(self.filtered_rows):
            raw_profile = row.get("esimProfileStatus", "")
            profile = {"INSTALLED": "已安装（INSTALLED）",
                       "RELEASED": "已释放待下载（RELEASED）"}.get(
                           str(raw_profile), str(raw_profile))
            query_status = row.get("esimProfileStatusQueryStatus", "")
            if query_status == "failed":
                tag, profile = "failed", "查询失败"
            elif raw_profile == "INSTALLED":
                tag = "installed"
            elif raw_profile == "RELEASED":
                tag = "released"
            else:
                tag = ""
            values = (str(row.get("iccid", "") or ""), str(row.get("status", "") or ""),
                      profile, str(row.get("esimProfileStatusUpdatedAt", "") or ""),
                      "成功" if query_status == "ok" else query_status)
            self.tree.insert("", "end", iid=str(index), values=values, tags=(tag,))
        self.count_text.set(f"{len(self.filtered_rows)} / {len(self.rows)} 条记录")

    def _show_detail(self, _event: tk.Event) -> None:
        selected = self.tree.selection()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if selected:
            row = self.filtered_rows[int(selected[0])]
            self.detail.insert("1.0", json.dumps(row, ensure_ascii=False, indent=2))
        self.detail.configure(state="disabled")

    def _reset_result_view(self) -> None:
        for variable in (self.total_text, self.installed_text, self.released_text, self.failed_text):
            variable.set("0")
        self.json_path.set("")
        self.csv_path.set("")
        self.profile_filter.set("全部 Profile 状态")
        self.filter_values = {"全部 Profile 状态": "__all__"}
        self.filter_combo.configure(values=("全部 Profile 状态",))
        self._apply_filter()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.configure(state="disabled")

    def _open_output_dir(self) -> None:
        path = Path(self.output_dir.get().strip())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def _close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            messagebox.showinfo("查询进行中", "已请求停止当前查询，请等待当前请求结束后再关闭窗口。")
            return
        self.destroy()


def launch() -> None:
    StatusGui().mainloop()


if __name__ == "__main__":
    launch()
