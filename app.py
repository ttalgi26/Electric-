"""컴퓨터 월별 전기 사용량 계산기 (화면 버전).

실행하면 내 컴퓨터의 부품과 최근 사용 기록을 자동으로 읽어 월별 전기 사용량과
한전 누진제 기준 요금을 보여준다. 값은 화면에서 바로 고칠 수 있다.
"""

import copy
import datetime
import json
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import hardware
import power_calc as pc
import usage


class PartDialog(tk.Toplevel):
    """부품 추가/수정 창."""

    def __init__(self, master, part=None):
        super().__init__(master)
        self.title("부품 수정" if part else "부품 추가")
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        part = part or {"name": "", "idle_w": 5, "load_w": 20, "count": 1}

        self.vars = {
            "name": tk.StringVar(value=part["name"]),
            "count": tk.StringVar(value=str(part.get("count", 1))),
            "idle_w": tk.StringVar(value=str(part["idle_w"])),
            "load_w": tk.StringVar(value=str(part["load_w"])),
        }
        self.external = tk.BooleanVar(value=part.get("external", False))
        labels = (("name", "부품 이름"), ("count", "개수"),
                  ("idle_w", "유휴 소비전력 (W)"), ("load_w", "최대 소비전력 (W)"))
        for row, (key, label) in enumerate(labels):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=4)
            ttk.Entry(self, textvariable=self.vars[key], width=34).grid(row=row, column=1,
                                                                         padx=10, pady=4)
        ttk.Checkbutton(self, text="콘센트에 따로 연결 (모니터 등, 파워 효율 미적용)",
                        variable=self.external).grid(row=4, column=0, columnspan=2,
                                                     sticky="w", padx=10)
        buttons = ttk.Frame(self)
        buttons.grid(row=5, column=0, columnspan=2, pady=10)
        ttk.Button(buttons, text="확인", command=self.ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="left", padx=4)
        self.bind("<Return>", lambda e: self.ok())
        self.grab_set()

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
        self.result = {"name": self.vars["name"].get().strip() or "부품",
                       "idle_w": idle, "load_w": load, "count": count, "note": "직접 입력"}
        if self.external.get():
            self.result["external"] = True
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PC 월별 전기 사용량 계산기")
        self.geometry("1120x760")
        self.minsize(960, 640)
        self.parts = []
        self._detect_result = None
        self._calc_job = None

        self._setup_style()
        self._build()
        self.after(100, self.start_detect)

    # ------------------------------------------------------------ 화면 구성

    def _setup_style(self):
        style = ttk.Style(self)
        if sys.platform == "win32":
            family = "맑은 고딕"
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
                self.tk.call("font", "configure", name, "-family", family, "-size", 10)
        style.configure("Treeview", rowheight=24)
        style.configure("Big.TLabel", font=("TkDefaultFont", 14, "bold"))
        style.configure("Sub.TLabel", foreground="#555555")

    def _build(self):
        default = pc.DEFAULT_CONFIG
        u, t = default["usage"], default["tariff"]
        self.v = {
            "on_hours": tk.StringVar(value=str(u["light_hours"] + u["heavy_hours"])),
            "heavy_hours": tk.StringVar(value=str(u["heavy_hours"])),
            "days_per_week": tk.StringVar(value=str(u["days_per_week"])),
            "standby_w": tk.StringVar(value=str(u["standby_w"])),
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

        body = ttk.Frame(self, padding=(10, 0, 10, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=5)
        body.columnconfigure(1, weight=4)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        self._build_parts(left)
        self._build_settings(left)
        self._build_results(right)

    def _build_parts(self, parent):
        box = ttk.LabelFrame(parent, text="부품 (더블클릭해서 수정)", padding=6)
        box.pack(fill="both", expand=True)
        self.info_label = ttk.Label(box, text="", style="Sub.TLabel")
        self.info_label.pack(anchor="w")
        cols = (("name", "부품", 250), ("count", "개수", 45), ("idle", "유휴W", 60),
                ("load", "최대W", 60), ("note", "비고", 130))
        self.part_tree = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", height=9)
        for key, label, width in cols:
            self.part_tree.heading(key, text=label)
            self.part_tree.column(key, width=width, anchor="w" if key in ("name", "note") else "e")
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
        self._field(box, 0, "하루 켜두는 시간 (사용하는 날)", "on_hours", "시간")
        self._field(box, 0, "그중 게임·고부하 시간", "heavy_hours", "시간", col=3)
        self._field(box, 1, "주당 사용 일수", "days_per_week", "일")
        self._field(box, 1, "꺼짐/대기 전력", "standby_w", "W", col=3)
        self.usage_label = ttk.Label(box, text="", style="Sub.TLabel", wraplength=560)
        self.usage_label.grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))

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
                  style="Sub.TLabel", wraplength=560).grid(row=5, column=0, columnspan=6,
                                                           sticky="w", pady=(4, 0))

    def _build_results(self, parent):
        box = ttk.LabelFrame(parent, text="결과", padding=8)
        box.pack(fill="both", expand=True)
        self.summary = ttk.Label(box, text="계산 중...", style="Big.TLabel")
        self.summary.pack(anchor="w")
        self.summary_sub = ttk.Label(box, text="", style="Sub.TLabel", wraplength=440)
        self.summary_sub.pack(anchor="w", pady=(2, 6))

        cols = (("month", "월", 45), ("kwh", "PC 사용량(kWh)", 105),
                ("house", "집 전체(kWh)", 95), ("tier", "누진", 45), ("cost", "PC 요금(원)", 100))
        self.month_tree = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", height=13)
        for key, label, width in cols:
            self.month_tree.heading(key, text=label)
            self.month_tree.column(key, width=width, anchor="e")
        self.month_tree.pack(fill="x")

        ttk.Label(box, text="부품별 비중 (사용하는 날 하루)").pack(anchor="w", pady=(10, 2))
        cols = (("name", "부품", 230), ("wh", "Wh/일", 70), ("share", "비중", 60))
        self.share_tree = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", height=7)
        for key, label, width in cols:
            self.share_tree.heading(key, text=label)
            self.share_tree.column(key, width=width, anchor="w" if key == "name" else "e")
        self.share_tree.pack(fill="both", expand=True)

    # ------------------------------------------------------------ 부품 편집

    def refresh_parts(self):
        self.part_tree.delete(*self.part_tree.get_children())
        for i, p in enumerate(self.parts):
            self.part_tree.insert("", "end", iid=str(i), values=(
                p["name"], p.get("count", 1), f"{p['idle_w']:g}", f"{p['load_w']:g}",
                p.get("note", "") + (" · 외부 전원" if p.get("external") else "")))
        self.schedule_calc()

    def selected_index(self):
        sel = self.part_tree.selection()
        return int(sel[0]) if sel else None

    def add_part(self):
        dialog = PartDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.parts.append(dialog.result)
            self.refresh_parts()

    def edit_part(self):
        idx = self.selected_index()
        if idx is None:
            return
        dialog = PartDialog(self, self.parts[idx])
        self.wait_window(dialog)
        if dialog.result:
            self.parts[idx] = dialog.result
            self.refresh_parts()

    def delete_part(self):
        idx = self.selected_index()
        if idx is not None:
            del self.parts[idx]
            self.refresh_parts()

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
                "게임·고부하 시간은 기록으로 알 수 없으니 직접 맞춰주세요."))
        else:
            self.usage_label.config(text="사용 기록을 읽지 못해 기본값을 사용합니다. 직접 입력해주세요.")

        self.status.set("감지 완료")
        self.refresh_parts()

    # ------------------------------------------------------------ 계산

    def num(self, key):
        return float(self.v[key].get().replace(",", ""))

    def build_config(self):
        config = pc.new_config()
        config["parts"] = copy.deepcopy(self.parts)
        on, heavy = self.num("on_hours"), self.num("heavy_hours")
        if not 0 <= on <= 24 or not 0 <= heavy <= on:
            raise ValueError("켜두는 시간은 0~24, 고부하 시간은 켜두는 시간 이하로 입력해주세요.")
        days = self.num("days_per_week")
        if not 0 <= days <= 7:
            raise ValueError("주당 사용 일수는 0~7 사이로 입력해주세요.")
        psu = self.num("psu")
        if not 0.5 <= psu <= 1:
            raise ValueError("파워 효율은 0.5~1 사이로 입력해주세요.")
        config["usage"].update(light_hours=on - heavy, heavy_hours=heavy, days_per_week=days,
                               standby_w=self.num("standby_w"))
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
        if not config["parts"]:
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
        self.v["psu"].set(f"{config['psu_efficiency']:g}")
        self.v["price"].set(f"{config['price_per_kwh']:g}")
        self.v["mode"].set(t["mode"])
        self.v["household"].set(f"{t['household_kwh']:g}")
        self.v["climate"].set(f"{t['climate_per_kwh']:g}")
        self.v["fuel"].set(f"{t['fuel_per_kwh']:g}")
        self.v["fund"].set(f"{t['fund_rate'] * 100:g}")
        self.parts = config["parts"]
        self.info_label.config(text=f"불러온 설정: {path}")
        self.refresh_parts()


if __name__ == "__main__":
    App().mainloop()
