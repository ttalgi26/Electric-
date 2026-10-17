"""컴퓨터 월별 전기 사용량 계산기 (화면 버전).

실행하면 내 컴퓨터의 부품·모니터와 최근 사용 기록을 자동으로 읽어 월별 전기 사용량과
한전 누진제 기준 요금을 보여준다. "전력 테스트" 탭에서는 직접 부하를 걸거나 원하는
프로그램을 실행하면서 전력을 측정하고, 그 결과로 월 사용량을 다시 계산할 수 있다.
"""

import copy
import csv
import datetime
import json
import multiprocessing
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import hardware
import measure
import monitor
import power_calc as pc
import usage


def make_tree(parent, cols, height, stretch=None):
    tree = ttk.Treeview(parent, columns=[c[0] for c in cols], show="headings", height=height)
    for key, label, width in cols:
        tree.heading(key, text=label)
        anchor = "w" if key in ("name", "note", "method", "desc") else "e"
        tree.column(key, width=width, anchor=anchor, stretch=key == stretch)
    return tree


def fmt(value, unit="", digits=0):
    return "-" if value is None else f"{value:,.{digits}f}{unit}"


class Dialog(tk.Toplevel):
    """확인/취소가 있는 입력 창의 공통 틀."""

    def __init__(self, master, title):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self.body = ttk.Frame(self, padding=10)
        self.body.pack()

    def finish(self, row):
        buttons = ttk.Frame(self.body)
        buttons.grid(row=row, column=0, columnspan=3, pady=(10, 0))
        ttk.Button(buttons, text="확인", command=self.ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="left", padx=4)
        self.bind("<Return>", lambda e: self.ok())
        self.grab_set()

    def row(self, row, label, widget):
        ttk.Label(self.body, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
        widget.grid(row=row, column=1, sticky="w", pady=3)
        return widget


class PartDialog(Dialog):
    """부품 추가/수정 창."""

    def __init__(self, master, part=None):
        super().__init__(master, "부품 수정" if part else "부품 추가")
        part = part or {"name": "", "idle_w": 5, "load_w": 20, "count": 1}
        self.part = part
        self.vars = {k: tk.StringVar(value=str(part.get(k, 1 if k == "count" else "")))
                     for k in ("name", "count", "idle_w", "load_w")}
        labels = (("name", "부품 이름"), ("count", "개수"),
                  ("idle_w", "유휴 소비전력 (W)"), ("load_w", "최대 소비전력 (W)"))
        for i, (key, label) in enumerate(labels):
            self.row(i, label, ttk.Entry(self.body, textvariable=self.vars[key], width=34))
        self.kind = tk.StringVar(value=part.get("kind", "other"))
        kinds = ttk.Combobox(self.body, textvariable=self.kind, state="readonly", width=12,
                             values=("cpu", "gpu", "igpu", "board", "ram", "ssd", "hdd",
                                     "fan", "other"))
        self.row(4, "종류 (전력 테스트용)", kinds)
        self.finish(5)

    def ok(self):
        try:
            count = int(float(self.vars["count"].get()))
            idle = float(self.vars["idle_w"].get())
            load = float(self.vars["load_w"].get())
        except ValueError:
            messagebox.showerror("입력 오류", "개수와 소비전력은 숫자로 입력해주세요.", parent=self)
            return
        if count < 1 or idle < 0 or load < idle:
            messagebox.showerror("입력 오류", "개수는 1 이상, 최대 전력은 유휴 전력 이상이어야 합니다.",
                                 parent=self)
            return
        self.result = {"name": self.vars["name"].get().strip() or "부품", "kind": self.kind.get(),
                       "idle_w": idle, "load_w": load, "count": count, "note": "직접 입력"}
        if self.part.get("external"):
            self.result["external"] = True
        self.destroy()


class MonitorDialog(Dialog):
    """모니터 추가/수정 창. 값을 바꾸면 예상 소비전력이 바로 바뀐다."""

    PANEL_LABELS = list(monitor.PANELS.values())

    def __init__(self, master, spec=None):
        super().__init__(master, "모니터 수정" if spec else "모니터 추가")
        spec = spec or monitor.make_spec()
        self.v = {
            "name": tk.StringVar(value=spec["name"]),
            "size": tk.StringVar(value=f"{spec['size']:g}"),
            "resolution": tk.StringVar(value=spec["resolution"]),
            "refresh": tk.StringVar(value=f"{spec['refresh']:g}"),
            "panel": tk.StringVar(value=monitor.PANELS.get(spec["panel"], self.PANEL_LABELS[0])),
            "brightness": tk.DoubleVar(value=spec["brightness"]),
            "count": tk.StringVar(value=str(spec.get("count", 1))),
        }
        self.hdr = tk.BooleanVar(value=spec.get("hdr", False))
        b = self.body
        self.row(0, "이름", ttk.Entry(b, textvariable=self.v["name"], width=30))
        self.row(1, "크기 (인치)", ttk.Combobox(b, textvariable=self.v["size"], width=10,
                                             values=("21.5", "24", "27", "32", "34", "42", "49")))
        self.row(2, "해상도", ttk.Combobox(b, textvariable=self.v["resolution"], width=12,
                                        values=("1920x1080", "2560x1440", "3440x1440",
                                                "3840x2160", "5120x1440")))
        self.row(3, "주사율 (Hz)", ttk.Combobox(b, textvariable=self.v["refresh"], width=10,
                                             values=("60", "75", "100", "120", "144", "165",
                                                     "240", "360")))
        self.row(4, "패널", ttk.Combobox(b, textvariable=self.v["panel"], state="readonly",
                                       width=16, values=self.PANEL_LABELS))
        scale_box = ttk.Frame(b)
        ttk.Scale(scale_box, from_=0, to=100, variable=self.v["brightness"],
                  length=180).pack(side="left")
        self.bright_label = ttk.Label(scale_box, width=5)
        self.bright_label.pack(side="left", padx=6)
        self.row(5, "밝기", scale_box)
        self.row(6, "HDR", ttk.Checkbutton(b, text="HDR 켜고 사용", variable=self.hdr))
        self.row(7, "대수", ttk.Entry(b, textvariable=self.v["count"], width=6))
        self.preview = ttk.Label(b, text="", font=("TkDefaultFont", 11, "bold"))
        self.preview.grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))
        for var in list(self.v.values()) + [self.hdr]:
            var.trace_add("write", lambda *_: self.update_preview())
        self.update_preview()
        self.finish(9)

    def spec(self):
        panel = next(k for k, v in monitor.PANELS.items() if v == self.v["panel"].get())
        return monitor.make_spec(size=float(self.v["size"].get()),
                                 resolution=self.v["resolution"].get().strip(),
                                 refresh=float(self.v["refresh"].get()), panel=panel,
                                 brightness=round(self.v["brightness"].get()),
                                 hdr=self.hdr.get(), name=self.v["name"].get().strip() or None,
                                 count=max(1, int(float(self.v["count"].get()))))

    def update_preview(self):
        self.bright_label.config(text=f"{self.v['brightness'].get():.0f}%")
        try:
            spec = self.spec()
        except (ValueError, StopIteration):
            self.preview.config(text="값을 확인해주세요")
            return
        self.preview.config(text=f"켜져 있을 때 약 {monitor.monitor_power(spec):g}W / 대")

    def ok(self):
        try:
            spec = self.spec()
        except (ValueError, StopIteration):
            messagebox.showerror("입력 오류", "크기·주사율·대수는 숫자로 입력해주세요.", parent=self)
            return
        if not monitor.parse_resolution(spec["resolution"]):
            messagebox.showerror("입력 오류", "해상도는 1920x1080 형식으로 입력해주세요.", parent=self)
            return
        self.result = spec
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PC 월별 전기 사용량 계산기")
        self.geometry("1180x800")
        self.minsize(1000, 680)
        self.parts, self.monitors = [], []
        self.activities = []            # 월별 계산에 적용된 측정 시간표
        self.test_results = []
        self._detect_result = None
        self._calc_job = None
        self.sampler = None
        self.test_thread = None
        self.test_stop = threading.Event()
        self.test_progress = None
        self.test_queue = []

        self._setup_style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.start_detect)

    # ------------------------------------------------------------ 화면 구성

    def _setup_style(self):
        style = ttk.Style(self)
        if sys.platform == "win32":
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
                self.tk.call("font", "configure", name, "-family", "맑은 고딕", "-size", 10)
        style.configure("Treeview", rowheight=24)
        style.configure("Big.TLabel", font=("TkDefaultFont", 14, "bold"))
        style.configure("Live.TLabel", font=("TkDefaultFont", 12, "bold"))
        style.configure("Sub.TLabel", foreground="#555555")

    def _build(self):
        default = pc.DEFAULT_CONFIG
        u, t = default["usage"], default["tariff"]
        self.v = {
            "on_hours": tk.StringVar(value=str(u["light_hours"] + u["heavy_hours"])),
            "heavy_hours": tk.StringVar(value=str(u["heavy_hours"])),
            "days_per_week": tk.StringVar(value=str(u["days_per_week"])),
            "standby_w": tk.StringVar(value=str(u["standby_w"])),
            "monitor_ratio": tk.StringVar(value=str(round(u["monitor_on_ratio"] * 100))),
            "psu": tk.StringVar(value=str(default["psu_efficiency"])),
            "mode": tk.StringVar(value=t["mode"]),
            "household": tk.StringVar(value=str(t["household_kwh"])),
            "price": tk.StringVar(value=str(default["price_per_kwh"])),
            "climate": tk.StringVar(value=str(t["climate_per_kwh"])),
            "fuel": tk.StringVar(value=str(t["fuel_per_kwh"])),
            "fund": tk.StringVar(value=str(t["fund_rate"] * 100)),
            "year": tk.StringVar(value=str(datetime.date.today().year)),
        }
        for var in self.v.values():
            var.trace_add("write", lambda *_: self.schedule_calc())

        toolbar = ttk.Frame(self, padding=(10, 8))
        toolbar.pack(fill="x")
        self.detect_btn = ttk.Button(toolbar, text="내 컴퓨터 다시 감지", command=self.start_detect)
        self.detect_btn.pack(side="left")
        ttk.Button(toolbar, text="설정 불러오기", command=self.load).pack(side="left", padx=6)
        ttk.Button(toolbar, text="설정 저장", command=self.save).pack(side="left")
        self.status = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.status, style="Sub.TLabel").pack(side="left", padx=12)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        calc_tab = ttk.Frame(self.tabs, padding=8)
        monitor_tab = ttk.Frame(self.tabs, padding=8)
        test_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(calc_tab, text="  월별 계산  ")
        self.tabs.add(monitor_tab, text="  모니터  ")
        self.tabs.add(test_tab, text="  전력 테스트  ")
        self.tabs.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        self.test_tab = test_tab

        calc_tab.columnconfigure(0, weight=5)
        calc_tab.columnconfigure(1, weight=4)
        calc_tab.rowconfigure(0, weight=1)
        left = ttk.Frame(calc_tab)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Frame(calc_tab)
        right.grid(row=0, column=1, sticky="nsew")
        self._build_parts(left)
        self._build_settings(left)
        self._build_results(right)
        self._build_monitor_tab(monitor_tab)
        self._build_test_tab(test_tab)

    def _build_parts(self, parent):
        box = ttk.LabelFrame(parent, text="본체 부품 (더블클릭해서 수정 · 모니터는 '모니터' 탭)",
                             padding=6)
        box.pack(fill="both", expand=True)
        self.info_label = ttk.Label(box, text="", style="Sub.TLabel")
        self.info_label.pack(anchor="w")
        self.part_tree = make_tree(box, (("name", "부품", 250), ("count", "개수", 45),
                                         ("idle", "유휴W", 60), ("load", "최대W", 60),
                                         ("note", "비고", 130)), 8, stretch="name")
        self.part_tree.pack(fill="both", expand=True, pady=4)
        self.part_tree.bind("<Double-1>", lambda e: self.edit_part())
        buttons = ttk.Frame(box)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="추가", command=self.add_part).pack(side="left")
        ttk.Button(buttons, text="수정", command=self.edit_part).pack(side="left", padx=4)
        ttk.Button(buttons, text="삭제", command=self.delete_part).pack(side="left")

    def _field(self, parent, row, label, key, unit="", col=0, width=8):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=3)
        entry = ttk.Entry(parent, textvariable=self.v[key], width=width, justify="right")
        entry.grid(row=row, column=col + 1, sticky="w", pady=3)
        ttk.Label(parent, text=unit).grid(row=row, column=col + 2, sticky="w", padx=(3, 18))
        return entry

    def _build_settings(self, parent):
        box = ttk.LabelFrame(parent, text="사용 패턴", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.activity_bar = ttk.Frame(box)
        self.activity_label = ttk.Label(self.activity_bar, foreground="#0a5")
        self.activity_label.pack(side="left")
        ttk.Button(self.activity_bar, text="해제 (부품 추정으로 계산)",
                   command=self.clear_activities).pack(side="left", padx=8)
        self.activity_bar.grid(row=0, column=0, columnspan=6, sticky="w")
        self.activity_bar.grid_remove()
        self._field(box, 1, "하루 켜두는 시간 (사용하는 날)", "on_hours", "시간")
        self._field(box, 1, "그중 게임·고부하 시간", "heavy_hours", "시간", col=3)
        self._field(box, 2, "주당 사용 일수", "days_per_week", "일")
        self._field(box, 2, "본체 꺼짐/대기 전력", "standby_w", "W", col=3)
        self._field(box, 3, "사용 중 모니터 켜짐 비율", "monitor_ratio", "%")
        self.usage_label = ttk.Label(box, text="", style="Sub.TLabel", wraplength=580)
        self.usage_label.grid(row=4, column=0, columnspan=6, sticky="w", pady=(4, 0))

        box = ttk.LabelFrame(parent, text="요금", padding=8)
        box.pack(fill="x", pady=(8, 0))
        modes = ttk.Frame(box)
        modes.grid(row=0, column=0, columnspan=6, sticky="w")
        ttk.Radiobutton(modes, text="한전 주택용 누진제", value="kepco",
                        variable=self.v["mode"]).pack(side="left")
        ttk.Radiobutton(modes, text="단일 단가", value="flat",
                        variable=self.v["mode"]).pack(side="left", padx=12)
        self._field(box, 1, "컴퓨터 뺀 우리 집 월 사용량", "household", "kWh")
        self._field(box, 1, "단일 단가", "price", "원/kWh", col=3)
        self._field(box, 2, "기후환경요금", "climate", "원/kWh")
        self._field(box, 2, "연료비조정요금", "fuel", "원/kWh", col=3)
        self._field(box, 3, "전력산업기반기금", "fund", "%")
        self._field(box, 3, "파워 효율", "psu", "(0.5~1)", col=3)
        self._field(box, 4, "계산 연도", "year", "년")
        ttk.Label(box, text="집 사용량은 전기 고지서의 월 사용량에서 컴퓨터 몫을 뺀 값입니다. "
                            "0이면 컴퓨터만 따로 계량한 것처럼 계산합니다.",
                  style="Sub.TLabel", wraplength=580).grid(row=5, column=0, columnspan=6,
                                                           sticky="w", pady=(4, 0))

    def _build_results(self, parent):
        box = ttk.LabelFrame(parent, text="결과", padding=8)
        box.pack(fill="both", expand=True)
        self.summary = ttk.Label(box, text="계산 중...", style="Big.TLabel")
        self.summary.pack(anchor="w")
        self.summary_sub = ttk.Label(box, text="", style="Sub.TLabel", wraplength=440)
        self.summary_sub.pack(anchor="w", pady=(2, 6))
        self.month_tree = make_tree(box, (("month", "월", 45), ("kwh", "PC 사용량(kWh)", 105),
                                          ("house", "집 전체(kWh)", 95), ("tier", "누진", 45),
                                          ("cost", "PC 요금(원)", 100)), 13)
        self.month_tree.pack(fill="x")
        ttk.Label(box, text="항목별 비중 (사용하는 날 하루)").pack(anchor="w", pady=(10, 2))
        self.share_tree = make_tree(box, (("name", "항목", 230), ("wh", "Wh/일", 70),
                                          ("share", "비중", 60)), 7, stretch="name")
        self.share_tree.pack(fill="both", expand=True)

    def _build_monitor_tab(self, parent):
        ttk.Label(parent, text="연결된 외부 모니터를 자동으로 읽었습니다. 밝기·패널·HDR은 화면 설정에 맞게 "
                               "고쳐주세요. (노트북 내장 화면은 본체 부품에 포함)",
                  style="Sub.TLabel").pack(anchor="w")
        self.monitor_tree = make_tree(parent, (
            ("name", "모니터", 220), ("desc", "사양", 360), ("count", "대수", 50),
            ("watt", "켜짐 W/대", 80), ("day", "하루 Wh", 80), ("month", "월 kWh", 80),
            ("cost", "월 요금(원)", 90)), 8, stretch="desc")
        self.monitor_tree.pack(fill="x", pady=6)
        self.monitor_tree.bind("<Double-1>", lambda e: self.edit_monitor())
        buttons = ttk.Frame(parent)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="추가", command=self.add_monitor).pack(side="left")
        ttk.Button(buttons, text="수정", command=self.edit_monitor).pack(side="left", padx=4)
        ttk.Button(buttons, text="삭제", command=self.delete_monitor).pack(side="left")
        self.monitor_summary = ttk.Label(parent, text="", style="Big.TLabel")
        self.monitor_summary.pack(anchor="w", pady=(12, 4))

        box = ttk.LabelFrame(parent, text="모니터 전력이 달라지는 요소", padding=8)
        box.pack(fill="x", pady=(8, 0))
        lines = [
            "• 밝기: 가장 큰 요소입니다. LCD는 밝기 100% → 50%로 낮추면 화면 전력이 약 40% 줄어듭니다.",
            "• 크기·화면비: 화면 면적에 비례합니다. 32인치는 27인치보다 약 40% 더 씁니다.",
            "• 패널: OLED는 어두운 화면일수록 적게 쓰고, 미니 LED는 같은 밝기에서 LCD보다 20% 정도 더 씁니다.",
            "• 주사율·해상도: 120Hz 이상, 4K는 회로 전력이 몇 W 늘어납니다.",
            "• HDR: 밝은 장면에서 전력이 크게 늘어납니다 (약 +30%로 계산).",
            f"• 대기: PC가 꺼져 있거나 화면이 꺼지면 대당 {monitor.STANDBY_W}W로 계산합니다.",
        ]
        ttk.Label(box, text="\n".join(lines), justify="left").pack(anchor="w")

        box = ttk.LabelFrame(parent, text="밝기별 비교 (선택한 모니터, 현재 사용 시간 기준)", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.bright_tree = make_tree(box, (("bright", "밝기", 70), ("watt", "켜짐 W", 80),
                                           ("month", "월 kWh", 80), ("cost", "월 요금(원)", 100)), 6)
        self.bright_tree.pack(anchor="w")
        self.monitor_tree.bind("<<TreeviewSelect>>", lambda e: self.refresh_brightness_table())

    def _build_test_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)

        live = ttk.LabelFrame(parent, text="실시간 측정", padding=8)
        live.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.live_label = ttk.Label(live, text="측정 준비 중...", style="Live.TLabel")
        self.live_label.pack(anchor="w")
        self.source_label = ttk.Label(live, text="", style="Sub.TLabel", wraplength=1080,
                                      justify="left")
        self.source_label.pack(anchor="w", pady=(4, 0))

        auto = ttk.LabelFrame(parent, text="자동 부하 테스트 (프로그램이 직접 부하를 겁니다)", padding=8)
        auto.grid(row=1, column=0, sticky="nsew", pady=8, padx=(0, 8))
        self.test_checks = {}
        for i, (key, name, *_rest) in enumerate(measure.TESTS):
            var = tk.BooleanVar(value=True)
            self.test_checks[key] = var
            ttk.Checkbutton(auto, text=name, variable=var).grid(row=i // 2, column=i % 2,
                                                                sticky="w", padx=(0, 16))
        row = len(measure.TESTS) // 2 + 1
        opts = ttk.Frame(auto)
        opts.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="테스트당").pack(side="left")
        self.test_seconds = tk.StringVar(value="20")
        ttk.Entry(opts, textvariable=self.test_seconds, width=5,
                  justify="right").pack(side="left", padx=4)
        ttk.Label(opts, text="초").pack(side="left")
        self.auto_btn = ttk.Button(opts, text="자동 테스트 시작", command=self.start_auto_test)
        self.auto_btn.pack(side="left", padx=12)
        ttk.Label(auto, text="GPU는 이 프로그램이 부하를 걸 수 없습니다. 게임 등으로 오른쪽 '직접 측정'을 "
                             "이용하세요.", style="Sub.TLabel", wraplength=500).grid(
            row=row + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        custom = ttk.LabelFrame(parent, text="직접 측정 (측정 시작 후 원하는 프로그램을 사용하세요)",
                                padding=8)
        custom.grid(row=1, column=1, sticky="nsew", pady=8)
        self.custom_name = tk.StringVar(value="게임")
        self.custom_seconds = tk.StringVar(value="60")
        ttk.Label(custom, text="활동 이름").grid(row=0, column=0, sticky="w")
        ttk.Combobox(custom, textvariable=self.custom_name, width=22,
                     values=("게임", "유튜브/영상 시청", "웹서핑", "문서 작업", "영상 편집·렌더링",
                             "코딩/컴파일", "화상 회의")).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(custom, text="측정 시간").grid(row=1, column=0, sticky="w", pady=4)
        secs = ttk.Frame(custom)
        secs.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Entry(secs, textvariable=self.custom_seconds, width=6,
                  justify="right").pack(side="left")
        ttk.Label(secs, text="초").pack(side="left", padx=4)
        self.custom_btn = ttk.Button(custom, text="측정 시작", command=self.start_custom_test)
        self.custom_btn.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(custom, text="예) 게임을 켠 상태로 이 창에서 '측정 시작' → 게임 화면으로 돌아가 플레이. "
                               "측정이 끝나면 결과가 아래 표에 추가됩니다.",
                  style="Sub.TLabel", wraplength=480).grid(row=3, column=0, columnspan=2,
                                                           sticky="w", pady=(6, 0))

        prog = ttk.Frame(parent)
        prog.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.progress = ttk.Progressbar(prog, maximum=100, length=400)
        self.progress.pack(side="left")
        self.progress_label = ttk.Label(prog, text="")
        self.progress_label.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(prog, text="중지", command=self.test_stop.set, state="disabled")
        self.stop_btn.pack(side="left")

        box = ttk.LabelFrame(parent, text="측정 결과 (하루 시간을 더블클릭해서 입력 → 월 사용량 계산)",
                             padding=8)
        box.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        parent.rowconfigure(3, weight=1)
        self.result_tree = make_tree(box, (
            ("name", "활동/테스트", 190), ("cpu", "CPU%", 55), ("gpu", "GPU%", 55),
            ("cpu_w", "CPU W", 65), ("gpu_w", "GPU W", 65), ("body", "본체 W", 70),
            ("mon", "모니터 W", 70), ("total", "합계 W", 75), ("hour_cost", "1시간 원", 70),
            ("hours", "하루 시간", 70), ("method", "측정 방식", 200)), 7, stretch="method")
        self.result_tree.pack(fill="both", expand=True)
        self.result_tree.bind("<Double-1>", lambda e: self.set_activity_hours())
        bottom = ttk.Frame(box)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Button(bottom, text="하루 시간 입력", command=self.set_activity_hours).pack(side="left")
        ttk.Button(bottom, text="결과 삭제", command=self.delete_result).pack(side="left", padx=4)
        ttk.Button(bottom, text="CSV 저장", command=self.export_results).pack(side="left")
        self.apply_btn = ttk.Button(bottom, text="이 시간표로 월별 계산 적용",
                                    command=self.apply_activities)
        self.apply_btn.pack(side="right")
        self.schedule_label = ttk.Label(box, text="", style="Live.TLabel")
        self.schedule_label.pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------ 부품 편집

    def refresh_parts(self):
        self.part_tree.delete(*self.part_tree.get_children())
        for i, p in enumerate(self.parts):
            self.part_tree.insert("", "end", iid=str(i), values=(
                p["name"], p.get("count", 1), f"{p['idle_w']:g}", f"{p['load_w']:g}",
                p.get("note", "") + (" · 외부 전원" if p.get("external") else "")))
        self.schedule_calc()

    def selected_index(self, tree):
        sel = tree.selection()
        return int(sel[0]) if sel else None

    def add_part(self):
        dialog = PartDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.parts.append(dialog.result)
            self.refresh_parts()

    def edit_part(self):
        idx = self.selected_index(self.part_tree)
        if idx is None:
            return
        dialog = PartDialog(self, self.parts[idx])
        self.wait_window(dialog)
        if dialog.result:
            self.parts[idx] = dialog.result
            self.refresh_parts()

    def delete_part(self):
        idx = self.selected_index(self.part_tree)
        if idx is not None:
            del self.parts[idx]
            self.refresh_parts()

    # ------------------------------------------------------------ 모니터

    def add_monitor(self):
        dialog = MonitorDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.monitors.append(dialog.result)
            self.schedule_calc()

    def edit_monitor(self):
        idx = self.selected_index(self.monitor_tree)
        if idx is None:
            return
        dialog = MonitorDialog(self, self.monitors[idx])
        self.wait_window(dialog)
        if dialog.result:
            self.monitors[idx] = dialog.result
            self.schedule_calc()

    def delete_monitor(self):
        idx = self.selected_index(self.monitor_tree)
        if idx is not None:
            del self.monitors[idx]
            self.schedule_calc()

    def monitor_month(self, spec, config):
        """모니터 한 종류의 (하루 Wh, 월 kWh, 월 요금)."""
        u = config["usage"]
        on_h = u["light_hours"] + u["heavy_hours"]
        ratio = u["monitor_on_ratio"]
        count = spec.get("count", 1)
        day_wh = (monitor.monitor_power(spec) * on_h * ratio
                  + monitor.STANDBY_W * (24 - on_h * ratio)) * count
        idle_wh = monitor.STANDBY_W * 24 * count
        days = 365 / 12
        used = days * u["days_per_week"] / 7
        kwh = (day_wh * used + idle_wh * (days - used)) / 1000
        return day_wh, kwh, self.marginal_cost(config, kwh)

    def marginal_cost(self, config, kwh):
        """월 kWh가 늘어날 때 늘어나는 연평균 월 요금."""
        t = config["tariff"]
        if t["mode"] != "kepco":
            return kwh * config["price_per_kwh"]
        opts = {"climate": t["climate_per_kwh"], "fuel": t["fuel_per_kwh"],
                "fund_rate": t["fund_rate"]}
        base, extra = t["household_kwh"], 0
        for month in range(1, 13):
            before = pc.tariff.kepco_bill(base, month, **opts)["total"] if base > 0 else 0
            extra += pc.tariff.kepco_bill(base + kwh, month, **opts)["total"] - before
        return extra / 12

    def refresh_monitors(self, config):
        selected = self.monitor_tree.selection()
        self.monitor_tree.delete(*self.monitor_tree.get_children())
        total_kwh = total_cost = 0
        for i, spec in enumerate(self.monitors):
            day_wh, kwh, cost = self.monitor_month(spec, config)
            total_kwh += kwh
            total_cost += cost
            self.monitor_tree.insert("", "end", iid=str(i), values=(
                spec["name"], monitor.describe(spec), spec.get("count", 1),
                f"{monitor.monitor_power(spec):g}", f"{day_wh:.0f}", f"{kwh:.1f}", f"{cost:,.0f}"))
        if selected and self.monitor_tree.exists(selected[0]):
            self.monitor_tree.selection_set(selected)
        elif self.monitors:
            self.monitor_tree.selection_set("0")
        if self.monitors:
            self.monitor_summary.config(text=f"모니터 합계: 월 {total_kwh:.1f} kWh · 약 {total_cost:,.0f}원")
        else:
            self.monitor_summary.config(text="외부 모니터 없음 ('추가'로 직접 넣을 수 있습니다)")
        self.refresh_brightness_table(config)

    def refresh_brightness_table(self, config=None):
        self.bright_tree.delete(*self.bright_tree.get_children())
        idx = self.selected_index(self.monitor_tree)
        if idx is None or idx >= len(self.monitors):
            return
        try:
            config = config or self.build_config()
        except ValueError:
            return
        for bright in (100, 80, 60, 40, 20):
            spec = dict(self.monitors[idx], brightness=bright)
            _, kwh, cost = self.monitor_month(spec, config)
            self.bright_tree.insert("", "end", values=(
                f"{bright}%", f"{monitor.monitor_power(spec):g}", f"{kwh:.1f}", f"{cost:,.0f}"))

    # ------------------------------------------------------------ 자동 감지

    def start_detect(self):
        self.detect_btn.state(["disabled"])
        self.status.set("내 컴퓨터 정보와 사용 기록을 읽는 중입니다... (10초 정도 걸릴 수 있습니다)")
        self._detect_result = None

        def work():
            try:
                info = hardware.detect()
            except Exception:  # 감지 실패해도 화면은 계속 쓸 수 있어야 한다
                info = None
            try:
                detected = usage.detect_usage()
            except Exception:
                detected = None
            self._detect_result = (info, detected)

        threading.Thread(target=work, daemon=True).start()
        self.after(200, self._poll_detect)

    def _poll_detect(self):
        if self._detect_result is None:
            self.after(200, self._poll_detect)
            return
        info, detected = self._detect_result
        self.detect_btn.state(["!disabled"])

        if info:
            self.parts = hardware.build_parts(info)
            self.monitors = hardware.build_monitors(info)
            kind = "노트북" if info.get("laptop") else "데스크톱"
            self.info_label.config(text=f"{info.get('os')} {kind}에서 감지했습니다. "
                                        "소비전력은 모델별 추정치이니 필요하면 고쳐주세요.")
            if info.get("laptop"):
                self.v["standby_w"].set("0.5")
                self.v["psu"].set("0.88")
        else:
            self.info_label.config(text="부품을 감지하지 못했습니다. 직접 추가해주세요.")

        if detected:
            hours = detected["hours_per_active_day"]
            self.v["on_hours"].set(f"{hours:g}")
            self.v["days_per_week"].set(f"{detected['days_per_week']:g}")
            heavy = min(float(self.v["heavy_hours"].get() or 0), hours)
            self.v["heavy_hours"].set(f"{heavy:g}")
            self.usage_label.config(text=(
                f"{detected['source']} 최근 {detected['window_days']:g}일 기록: "
                f"총 {detected['total_hours']:g}시간 켜짐, 사용한 날 {detected['active_days']}일 "
                f"→ 하루 {hours:g}시간, 주 {detected['days_per_week']:g}일로 채웠습니다. "
                "게임·고부하 시간은 '전력 테스트' 탭에서 측정하거나 직접 맞춰주세요."))
        else:
            self.usage_label.config(text="사용 기록을 읽지 못해 기본값을 사용합니다. 직접 입력해주세요.")

        self.status.set("감지 완료")
        self.refresh_parts()

    # ------------------------------------------------------------ 계산

    def num(self, key):
        return float(self.v[key].get().replace(",", ""))

    def build_config(self, with_activities=True):
        config = pc.new_config()
        config["parts"] = copy.deepcopy(self.parts)
        config["monitors"] = copy.deepcopy(self.monitors)
        if with_activities:
            config["activities"] = copy.deepcopy(self.activities)
        on, heavy = self.num("on_hours"), self.num("heavy_hours")
        if not 0 <= on <= 24 or not 0 <= heavy <= on:
            raise ValueError("켜두는 시간은 0~24, 고부하 시간은 켜두는 시간 이하로 입력해주세요.")
        days = self.num("days_per_week")
        if not 0 <= days <= 7:
            raise ValueError("주당 사용 일수는 0~7 사이로 입력해주세요.")
        psu = self.num("psu")
        if not 0.5 <= psu <= 1:
            raise ValueError("파워 효율은 0.5~1 사이로 입력해주세요.")
        ratio = self.num("monitor_ratio")
        if not 0 <= ratio <= 100:
            raise ValueError("모니터 켜짐 비율은 0~100% 사이로 입력해주세요.")
        config["usage"].update(light_hours=on - heavy, heavy_hours=heavy, days_per_week=days,
                               standby_w=self.num("standby_w"), monitor_on_ratio=ratio / 100)
        config["psu_efficiency"] = psu
        config["price_per_kwh"] = self.num("price")
        config["tariff"].update(mode=self.v["mode"].get(), household_kwh=self.num("household"),
                                climate_per_kwh=self.num("climate"), fuel_per_kwh=self.num("fuel"),
                                fund_rate=self.num("fund") / 100)
        return config

    def schedule_calc(self):
        if self._calc_job:
            self.after_cancel(self._calc_job)
        self._calc_job = self.after(300, self.calculate)

    def calculate(self):
        self._calc_job = None
        try:
            config = self.build_config()
            year = int(self.num("year"))
        except ValueError as e:
            msg = str(e)
            self.status.set(msg if "입력" in msg else "숫자를 올바르게 입력해주세요.")
            return
        self.refresh_monitors(config)
        self.refresh_results_table(config)
        if self.activities:
            hours = sum(a["hours"] for a in self.activities)
            self.activity_label.config(text=f"전력 테스트 측정 시간표 적용 중 (하루 {hours:g}시간)")
            self.activity_bar.grid()
        else:
            self.activity_bar.grid_remove()
        if not config["parts"] and not config["monitors"] and not config["activities"]:
            self.summary.config(text="부품을 추가해주세요.")
            return

        rows = pc.monthly_report(config, year)
        total_day, per_part = pc.daily_kwh(config)
        kepco = config["tariff"]["mode"] == "kepco"

        self.month_tree.delete(*self.month_tree.get_children())
        for r in rows:
            self.month_tree.insert("", "end", values=(
                f"{r['month']}월", f"{r['kwh']:.1f}",
                f"{r['household_kwh']:.0f}" if kepco else "-",
                f"{r['tier']}단계" if kepco else "-",
                f"{r['cost']:,.0f}"))
        total_kwh = sum(r["kwh"] for r in rows)
        total_cost = sum(r["cost"] for r in rows)
        self.month_tree.insert("", "end", values=("연간", f"{total_kwh:.1f}", "", "",
                                                  f"{total_cost:,.0f}"))

        this_month = rows[datetime.date.today().month - 1]
        self.summary.config(text=f"월평균 {total_kwh / 12:.1f} kWh · 약 {total_cost / 12:,.0f}원")
        if kepco:
            sub = (f"이번 달 예상: PC {this_month['kwh']:.1f}kWh, PC 때문에 늘어나는 요금 "
                   f"{this_month['cost']:,.0f}원 (집 전체 {this_month['household_kwh']:.0f}kWh, "
                   f"{this_month['tier']}단계, 고지 예상액 {this_month['bill']:,.0f}원)")
        else:
            sub = (f"이번 달 예상: {this_month['kwh']:.1f}kWh × "
                   f"{config['price_per_kwh']:g}원 = {this_month['cost']:,.0f}원")
        self.summary_sub.config(text=sub)

        self.share_tree.delete(*self.share_tree.get_children())
        for name, kwh in sorted(per_part.items(), key=lambda kv: -kv[1]):
            share = kwh / total_day * 100 if total_day else 0
            self.share_tree.insert("", "end", values=(name, f"{kwh * 1000:.0f}", f"{share:.1f}%"))
        if self.status.get() != "감지 완료" and not self.status.get().startswith("내 컴퓨터"):
            self.status.set("")

    # ------------------------------------------------------------ 전력 테스트

    def on_tab_changed(self, _event):
        if self.tabs.select() == str(self.test_tab) and self.sampler is None:
            self.ensure_sampler()

    def ensure_sampler(self):
        if self.sampler is None:
            self.sampler = measure.Sampler()
            labels = (("cpu_util", "CPU 사용률"), ("cpu_w", "CPU 전력"), ("gpu_util", "GPU 사용률"),
                      ("gpu_w", "GPU 전력"), ("system_w", "시스템 전력"))
            text = " · ".join(f"{label}: {self.sampler.sources.get(key, '추정')}"
                              for key, label in labels)
            if "cpu_w_note" in self.sampler.sources:
                text += f"\n※ {self.sampler.sources['cpu_w_note']}"
            text += ("\n실측 센서가 없는 항목은 사용률 × 부품별 최대 전력으로 추정합니다. "
                     "정확한 값은 콘센트형 전력 측정기와 비교해 보세요.")
            self.source_label.config(text=text)
            self.after(1000, self._poll_live)
        return self.sampler

    def _poll_live(self):
        if self.sampler is None:
            return
        latest = dict(self.sampler.latest)
        try:
            config = self.build_config(with_activities=False)
        except ValueError:
            config = None
        if latest and config:
            est = measure.estimate(config, latest)
            self.live_label.config(text=(
                f"CPU {fmt(latest.get('cpu_util'), '%')} · GPU {fmt(latest.get('gpu_util'), '%')} · "
                f"CPU {est['cpu_w']:.0f}W · GPU {est['gpu_w']:.0f}W · 본체 {est['body_w']:.0f}W · "
                f"모니터 {est['monitor_w']:.0f}W → 합계 약 {est['total_w']:.0f}W  [{est['method']}]"))
        if self.test_progress:
            name, elapsed, total, index, count = self.test_progress
            self.progress["value"] = 100 * min(1, elapsed / total)
            self.progress_label.config(text=f"[{index}/{count}] {name} 측정 중... "
                                            f"{max(0, total - elapsed):.0f}초 남음")
        while self.test_queue:
            self.test_results.append(self.test_queue.pop(0))
            self.refresh_results_table()
        if self.test_thread and not self.test_thread.is_alive():
            self.test_thread = None
            self.test_progress = None
            self.progress["value"] = 0
            self.progress_label.config(text="측정 완료" if not self.test_stop.is_set() else "중지됨")
            for btn in (self.auto_btn, self.custom_btn):
                btn.state(["!disabled"])
            self.stop_btn.state(["disabled"])
        self.after(1000, self._poll_live)

    def run_tests(self, jobs):
        """jobs: [(이름, 초, 부하 키 또는 None)]"""
        if self.test_thread:
            return
        try:
            config = self.build_config(with_activities=False)
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e))
            return
        sampler = self.ensure_sampler()
        self.test_stop.clear()
        for btn in (self.auto_btn, self.custom_btn):
            btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])

        def work():
            for i, (name, seconds, key) in enumerate(jobs, 1):
                if self.test_stop.is_set():
                    break

                def tick(n, elapsed, total, i=i):
                    self.test_progress = (n, elapsed, total, i, len(jobs))

                result = measure.measure(sampler, config, name, seconds, self.test_stop, key, tick)
                if result["samples"]:
                    result["hours"] = 0.0
                    self.test_queue.append(result)

        self.test_thread = threading.Thread(target=work, daemon=True)
        self.test_thread.start()

    def _seconds(self, var, low=5):
        try:
            value = float(var.get())
        except ValueError:
            value = 0
        if value < low:
            messagebox.showerror("입력 오류", f"측정 시간은 {low}초 이상으로 입력해주세요.")
            return None
        return value

    def start_auto_test(self):
        seconds = self._seconds(self.test_seconds)
        keys = [key for key, var in self.test_checks.items() if var.get()]
        if seconds is None or not keys:
            return
        self.run_tests([(measure.TEST_NAMES[k], seconds, None if k == "idle" else k) for k in keys])

    def start_custom_test(self):
        seconds = self._seconds(self.custom_seconds, low=10)
        if seconds is None:
            return
        self.run_tests([(self.custom_name.get().strip() or "직접 측정", seconds, None)])

    def refresh_results_table(self, config=None):
        try:
            config = config or self.build_config()
        except ValueError:
            return
        hour_rate = self.marginal_cost(config, 1.0) if self.test_results else 0
        self.result_tree.delete(*self.result_tree.get_children())
        for i, r in enumerate(self.test_results):
            self.result_tree.insert("", "end", iid=str(i), values=(
                r["name"], fmt(r["cpu_util"], "%"), fmt(r["gpu_util"], "%"),
                f"{r['cpu_w']:.0f}", f"{r['gpu_w']:.0f}", f"{r['body_w']:.0f}",
                f"{r['monitor_w']:.0f}", f"{r['total_w']:.0f}",
                f"{r['total_w'] / 1000 * hour_rate:,.1f}",
                f"{r['hours']:g}" if r["hours"] else "-", r["method"]))
        schedule = [r for r in self.test_results if r["hours"] > 0]
        if not schedule:
            self.schedule_label.config(text="측정 결과의 '하루 시간'을 입력하면 월 사용량을 계산합니다.")
            return
        test_cfg = copy.deepcopy(config)
        test_cfg["activities"] = [{"name": r["name"], "watts": r["total_w"], "hours": r["hours"]}
                                  for r in schedule]
        rows = pc.monthly_report(test_cfg, int(self.num("year")))
        kwh = sum(r["kwh"] for r in rows) / 12
        cost = sum(r["cost"] for r in rows) / 12
        hours = sum(r["hours"] for r in schedule)
        self.schedule_label.config(text=f"측정 시간표 (하루 {hours:g}시간, 주 {self.num('days_per_week'):g}일) "
                                        f"→ 월평균 {kwh:.1f} kWh · 약 {cost:,.0f}원")

    def set_activity_hours(self):
        idx = self.selected_index(self.result_tree)
        if idx is None:
            messagebox.showinfo("하루 시간", "먼저 결과 표에서 항목을 선택해주세요.")
            return
        r = self.test_results[idx]
        hours = simpledialog.askfloat("하루 시간", f"'{r['name']}' 상태로 하루 몇 시간 쓰나요?",
                                      initialvalue=r["hours"], minvalue=0, maxvalue=24, parent=self)
        if hours is None:
            return
        others = sum(x["hours"] for j, x in enumerate(self.test_results) if j != idx)
        if others + hours > 24:
            messagebox.showerror("입력 오류", "하루 시간 합계가 24시간을 넘을 수 없습니다.")
            return
        r["hours"] = hours
        self.refresh_results_table()

    def delete_result(self):
        idx = self.selected_index(self.result_tree)
        if idx is not None:
            del self.test_results[idx]
            self.refresh_results_table()

    def apply_activities(self):
        schedule = [r for r in self.test_results if r["hours"] > 0]
        if not schedule:
            messagebox.showinfo("적용", "측정 결과에 하루 시간을 하나 이상 입력해주세요.")
            return
        self.activities = [{"name": r["name"], "watts": round(r["total_w"], 1), "hours": r["hours"]}
                           for r in schedule]
        self.schedule_calc()
        self.tabs.select(0)

    def clear_activities(self):
        self.activities = []
        self.schedule_calc()

    def export_results(self):
        if not self.test_results:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        keys = ("name", "seconds", "cpu_util", "gpu_util", "cpu_w", "gpu_w", "system_w",
                "body_w", "monitor_w", "total_w", "hours", "method")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.test_results)
        self.status.set(f"저장했습니다: {path}")

    # ------------------------------------------------------------ 저장/불러오기

    def save(self):
        try:
            config = self.build_config()
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e))
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("설정 파일", "*.json")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self.status.set(f"저장했습니다: {path}")

    def load(self):
        path = filedialog.askopenfilename(filetypes=[("설정 파일", "*.json")])
        if not path:
            return
        try:
            config = pc.load_config(path)
        except (OSError, ValueError, SystemExit) as e:
            messagebox.showerror("불러오기 실패", str(e))
            return
        u, t = config["usage"], config["tariff"]
        self.v["on_hours"].set(f"{u['light_hours'] + u['heavy_hours']:g}")
        self.v["heavy_hours"].set(f"{u['heavy_hours']:g}")
        self.v["days_per_week"].set(f"{u['days_per_week']:g}")
        self.v["standby_w"].set(f"{u['standby_w']:g}")
        self.v["monitor_ratio"].set(f"{u['monitor_on_ratio'] * 100:g}")
        self.v["psu"].set(f"{config['psu_efficiency']:g}")
        self.v["price"].set(f"{config['price_per_kwh']:g}")
        self.v["mode"].set(t["mode"])
        self.v["household"].set(f"{t['household_kwh']:g}")
        self.v["climate"].set(f"{t['climate_per_kwh']:g}")
        self.v["fuel"].set(f"{t['fuel_per_kwh']:g}")
        self.v["fund"].set(f"{t['fund_rate'] * 100:g}")
        self.parts = config["parts"]
        self.monitors = config["monitors"]
        self.activities = config["activities"]
        self.info_label.config(text=f"불러온 설정: {path}")
        self.refresh_parts()

    def on_close(self):
        self.test_stop.set()
        if self.sampler:
            self.sampler.close()
        self.destroy()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    App().mainloop()
