#!/usr/bin/env python3
"""컴퓨터 월별 전기 사용량 계산기 (명령줄 버전).

부품 구성(부품별 유휴/최대 소비전력)과 사용 패턴(하루 사용 시간, 주당 사용 일수)으로
월별 전력 사용량(kWh)과 한전 누진제 기준 전기요금을 계산한다.

사용법:
    python3 power_calc.py --auto             # 내 컴퓨터 자동 감지 후 계산
    python3 power_calc.py                    # 대화형 입력
    python3 power_calc.py -c my_pc.json      # 설정 파일로 계산
    python3 power_calc.py --save my_pc.json  # 입력 결과를 설정 파일로 저장
"""

import argparse
import calendar
import copy
import datetime
import json
import sys
import unicodedata

import monitor
import tariff

# 부품 프리셋: (이름, 유휴 W, 최대 W). 값은 대표적인 실측 기준의 근사치다.
PRESETS = {
    "cpu": [
        ("저전력 CPU (i3 / Ryzen 3급, TDP 65W)", 10, 65),
        ("보급형 CPU (i5 / Ryzen 5급)", 15, 120),
        ("고성능 CPU (i7 / Ryzen 7급)", 20, 180),
        ("최상급 CPU (i9 / Ryzen 9급)", 25, 250),
    ],
    "gpu": [
        ("내장 그래픽", 0, 15),
        ("보급형 GPU (RTX 4060 / RX 7600급)", 10, 120),
        ("중급 GPU (RTX 4070 / RX 7800 XT급)", 12, 220),
        ("고급 GPU (RTX 4080 / RX 7900 XTX급)", 15, 330),
        ("최상급 GPU (RTX 4090 / 5090급)", 20, 450),
    ],
    "motherboard": [
        ("일반 메인보드", 20, 40),
        ("고급 메인보드 (X / Z 칩셋)", 30, 60),
    ],
}

# 개수 단위로 입력받는 부품: 키 -> (이름, 개당 유휴 W, 개당 최대 W)
COUNTED_PARTS = {
    "ram": ("RAM (개)", 2, 4),
    "ssd": ("SSD (개)", 1, 6),
    "hdd": ("HDD (개)", 5, 9),
    "fan": ("케이스 팬 (개)", 1, 3),
}

# 모니터 프리셋: (이름, 크기, 해상도, 주사율)
MONITOR_PRESETS = [
    ("24인치 FHD 60Hz", 24, "1920x1080", 60),
    ("27인치 QHD 144Hz", 27, "2560x1440", 144),
    ("27인치 4K 60Hz", 27, "3840x2160", 60),
    ("32인치 4K 144Hz", 32, "3840x2160", 144),
    ("34인치 울트라와이드 144Hz", 34, "3440x1440", 144),
]

DEFAULT_CONFIG = {
    "parts": [],
    "monitors": [],             # monitor.make_spec() 형식
    "activities": [],           # 전력 테스트 결과로 만든 시간표 [{name, watts, hours}]
    "usage": {
        "light_hours": 4.0,     # 웹서핑/문서작업 시간 (사용하는 날 하루)
        "heavy_hours": 2.0,     # 게임/렌더링 시간 (사용하는 날 하루)
        "days_per_week": 7,     # 주당 사용 일수
        "standby_w": 2.0,       # 꺼짐/대기 상태 소비전력 (본체)
        "monitor_on_ratio": 1.0,  # PC 사용 중 모니터 화면이 켜져 있는 비율
    },
    "psu_efficiency": 0.85,     # 파워서플라이 효율 (80 PLUS 브론즈~골드 ≈ 0.85~0.90)
    "light_factor": 0.2,        # 가벼운 작업 시 (최대-유휴) 대비 추가 부하 비율
    "heavy_factor": 0.8,        # 고부하 작업 시 (최대-유휴) 대비 부하 비율
    "price_per_kwh": 150.0,     # 단일 단가 방식일 때 전기요금 단가 (원/kWh)
    "tariff": {
        "mode": "kepco",        # "kepco" = 한전 주택용 누진제, "flat" = 단일 단가
        "household_kwh": 250,   # 컴퓨터를 뺀 우리 집 월 사용량 (kWh)
        "climate_per_kwh": tariff.CLIMATE_RATE,
        "fuel_per_kwh": tariff.FUEL_RATE,
        "fund_rate": tariff.FUND_RATE,
    },
}


def new_config():
    return copy.deepcopy(DEFAULT_CONFIG)


def part_power(part, factor):
    """부하 비율에 따른 부품의 소비전력(W, 개수 반영)."""
    idle, peak = part["idle_w"], part["load_w"]
    return (idle + (peak - idle) * factor) * part.get("count", 1)


def standby_watts(config):
    """PC가 꺼져 있을 때 본체와 모니터의 대기전력 합(W)."""
    monitors = sum(m.get("count", 1) for m in config.get("monitors", []))
    return config["usage"]["standby_w"] + monitor.STANDBY_W * monitors


def daily_kwh(config):
    """사용하는 날 하루 소비전력량(kWh)과 항목별 하루 소비량을 반환한다."""
    usage = config["usage"]
    activities = config.get("activities") or []
    per_part = {}

    if activities:
        # 전력 테스트로 측정한 활동별 전력(모니터 포함) × 하루 시간
        on_h = sum(a["hours"] for a in activities)
        for a in activities:
            per_part[f"[측정] {a['name']}"] = a["watts"] * a["hours"] / 1000
    else:
        light_h, heavy_h = usage["light_hours"], usage["heavy_hours"]
        on_h = light_h + heavy_h
        eff = config["psu_efficiency"]
        for part in config["parts"]:
            wh = (part_power(part, config["light_factor"]) * light_h
                  + part_power(part, config["heavy_factor"]) * heavy_h)
            # 모니터처럼 콘센트에 직접 연결된 기기는 파워 효율을 적용하지 않는다.
            if not part.get("external"):
                wh /= eff
            per_part[part["name"]] = per_part.get(part["name"], 0) + wh / 1000
        ratio = usage.get("monitor_on_ratio", 1.0)
        for spec in config.get("monitors", []):
            count = spec.get("count", 1)
            wh = (monitor.monitor_power(spec) * on_h * ratio
                  + monitor.STANDBY_W * on_h * (1 - ratio)) * count
            name = f"모니터: {spec['name']}"
            per_part[name] = per_part.get(name, 0) + wh / 1000

    off_h = max(0.0, 24 - on_h)
    per_part["대기전력 (꺼짐 상태)"] = standby_watts(config) * off_h / 1000
    return sum(per_part.values()), per_part


def monthly_report(config, year):
    """월별 결과 목록. 각 항목: month, days, kwh, cost (+누진제일 때 household_kwh, tier, bill)."""
    total_day, _ = daily_kwh(config)
    usage = config["usage"]
    use_ratio = usage["days_per_week"] / 7
    # 사용하지 않는 날은 대기전력만 소비한다.
    idle_day = standby_watts(config) * 24 / 1000
    t = config["tariff"]
    opts = {"climate": t["climate_per_kwh"], "fuel": t["fuel_per_kwh"], "fund_rate": t["fund_rate"]}

    rows = []
    for month in range(1, 13):
        days = calendar.monthrange(year, month)[1]
        used = days * use_ratio
        kwh = total_day * used + idle_day * (days - used)
        row = {"month": month, "days": days, "kwh": kwh}
        if t["mode"] == "kepco":
            base = t["household_kwh"]
            with_pc = tariff.kepco_bill(base + kwh, month, **opts)
            # 누진제는 집 전체 사용량에 매겨지므로, 컴퓨터 때문에 늘어난 요금만 따로 계산한다.
            without = tariff.kepco_bill(base, month, **opts)["total"] if base > 0 else 0
            row.update(household_kwh=base + kwh, tier=with_pc["tier"], bill=with_pc["total"],
                       cost=with_pc["total"] - without)
        else:
            row["cost"] = kwh * config["price_per_kwh"]
        rows.append(row)
    return rows


def apply_detected_usage(config, detected):
    """사용 기록 감지 결과를 설정에 반영한다."""
    if not detected:
        return
    usage = config["usage"]
    total = detected["hours_per_active_day"]
    usage["heavy_hours"] = min(usage["heavy_hours"], total)
    usage["light_hours"] = round(total - usage["heavy_hours"], 1)
    usage["days_per_week"] = detected["days_per_week"]


def auto_config():
    """현재 컴퓨터를 감지해 설정을 만든다. (설정, 하드웨어 정보, 사용 기록)"""
    import hardware
    import usage as usage_mod

    config = new_config()
    info = hardware.detect()
    config["parts"] = hardware.build_parts(info)
    config["monitors"] = hardware.build_monitors(info)
    if info.get("laptop"):
        config["usage"]["standby_w"] = 0.5
        config["psu_efficiency"] = 0.88
    detected = usage_mod.detect_usage()
    apply_detected_usage(config, detected)
    return config, info, detected


# ---------------------------------------------------------------- 입력 처리

def ask_float(prompt, default, lo=0.0, hi=None):
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            print("  숫자를 입력해주세요.")
            continue
        if value < lo or (hi is not None and value > hi):
            print(f"  {lo} ~ {hi if hi is not None else '∞'} 범위로 입력해주세요.")
            continue
        return value


def ask_int(prompt, default, lo=0, hi=None):
    return int(ask_float(prompt, default, lo, hi))


def choose(title, options):
    """프리셋 중 하나를 고르거나 직접 입력. (이름, 유휴W, 최대W) 반환."""
    print(f"\n[{title}]")
    for i, (name, idle, peak) in enumerate(options, 1):
        print(f"  {i}. {name}  (유휴 {idle}W / 최대 {peak}W)")
    print(f"  {len(options) + 1}. 직접 입력")
    pick = ask_int("번호 선택", 1, 1, len(options) + 1)
    if pick <= len(options):
        return options[pick - 1]
    name = input("  부품 이름: ").strip() or title
    peak = ask_float("  최대 소비전력 W (TDP/TBP)", 100.0)
    idle = ask_float("  유휴 소비전력 W", round(peak * 0.1, 1), 0.0, peak)
    return (name, idle, peak)


def interactive_config():
    config = new_config()
    print("=" * 60)
    print(" 컴퓨터 월별 전기 사용량 계산기")
    print(" (Enter를 누르면 [ ] 안의 기본값이 사용됩니다)")
    print("=" * 60)

    parts = config["parts"]
    for key, title in (("cpu", "CPU"), ("gpu", "그래픽카드"), ("motherboard", "메인보드")):
        name, idle, peak = choose(title, PRESETS[key])
        kind = {"cpu": "cpu", "gpu": "gpu" if peak > 15 else "igpu", "motherboard": "board"}[key]
        parts.append({"name": name, "kind": kind, "idle_w": idle, "load_w": peak})

    print("\n[기타 부품 개수]")
    defaults = {"ram": 2, "ssd": 1, "hdd": 0, "fan": 3}
    for key, (name, idle, peak) in COUNTED_PARTS.items():
        count = ask_int(f"  {name}", defaults[key])
        if count:
            parts.append({"name": name.split(" (")[0], "kind": key, "idle_w": idle,
                          "load_w": peak, "count": count})

    print("\n[모니터]")
    for i, (name, *_rest) in enumerate(MONITOR_PRESETS, 1):
        print(f"  {i}. {name}")
    print("  0. 모니터 없음 / 계산 제외")
    pick = ask_int("번호 선택", 2, 0, len(MONITOR_PRESETS))
    if pick:
        name, size, res, hz = MONITOR_PRESETS[pick - 1]
        count = ask_int("  모니터 개수", 1, 1)
        brightness = ask_float("  밝기 (%)", 70.0, 0, 100)
        oled = ask_int("  패널 (1: LCD, 2: OLED, 3: 미니 LED)", 1, 1, 3)
        spec = monitor.make_spec(size, res, hz, ("LCD", "OLED", "MINILED")[oled - 1],
                                 brightness, name=name, count=count)
        config["monitors"].append(spec)
        print(f"  → 켜져 있을 때 약 {monitor.monitor_power(spec):g}W (1대)")

    print("\n[사용 패턴]")
    u = config["usage"]
    u["light_hours"] = ask_float("  하루 가벼운 작업 시간 (웹/문서/영상)", u["light_hours"], 0, 24)
    u["heavy_hours"] = ask_float("  하루 고부하 작업 시간 (게임/렌더링)", u["heavy_hours"],
                                 0, 24 - u["light_hours"])
    u["days_per_week"] = ask_float("  주당 사용 일수", u["days_per_week"], 0, 7)
    u["standby_w"] = ask_float("  꺼짐/대기 상태 소비전력 W", u["standby_w"])

    print("\n[요금 설정]")
    config["psu_efficiency"] = ask_float("  파워 효율 (0.8~0.95)", config["psu_efficiency"], 0.5, 1.0)
    t = config["tariff"]
    mode = ask_int("  요금 방식 (1: 한전 누진제, 2: 단일 단가)", 1, 1, 2)
    if mode == 1:
        t["household_kwh"] = ask_float("  컴퓨터를 뺀 우리 집 월 사용량 kWh (0 = 컴퓨터만)",
                                       t["household_kwh"])
    else:
        t["mode"] = "flat"
        config["price_per_kwh"] = ask_float("  전기요금 단가 (원/kWh)", config["price_per_kwh"])
    return config


# ---------------------------------------------------------------- 출력

def pad(text, width):
    """한글처럼 두 칸을 차지하는 글자를 고려해 왼쪽 정렬한다."""
    out, used = "", 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width:
            break
        out, used = out + ch, used + w
    return out + " " * (width - used)


def print_detection(info, detected):
    print("=" * 60)
    print(f" 감지된 컴퓨터 ({info.get('os')}, {'노트북' if info.get('laptop') else '데스크톱'})")
    print("-" * 60)
    for label, key in (("CPU", "cpu"), ("GPU", "gpu")):
        for name in info.get(key) or ["감지 실패"]:
            print(f"  {label}: {name}")
    for mon in info.get("monitors") or []:
        print(f"  모니터: {mon.get('name') or '이름 미확인'} "
              f"({mon.get('inches') or '?'}인치, {mon.get('resolution') or '?'}, "
              f"{mon.get('refresh') or '?'}Hz)")
    if detected:
        print(f"  사용 기록 ({detected['source']}, 최근 {detected['window_days']}일): "
              f"사용한 날 {detected['active_days']}일, "
              f"하루 평균 {detected['hours_per_active_day']}시간 켜짐")
    else:
        print("  사용 기록: 읽지 못함 (기본값 사용)")


def print_report(config, year):
    total_day, per_part = daily_kwh(config)
    rows = monthly_report(config, year)
    kepco = config["tariff"]["mode"] == "kepco"

    print("\n" + "=" * 60)
    print(" 부품별 하루 소비 전력량 (사용하는 날 기준)")
    print("-" * 60)
    for name, kwh in sorted(per_part.items(), key=lambda kv: -kv[1]):
        share = kwh / total_day * 100 if total_day else 0
        print(f"  {pad(name, 36)} {kwh * 1000:8.1f} Wh  ({share:4.1f}%)")
    print("-" * 60)
    print(f"  {pad('하루 합계', 36)} {total_day:8.3f} kWh")
    for spec in config.get("monitors", []):
        print(f"  모니터 {spec['name']}: {monitor.describe(spec)} → 켜짐 "
              f"{monitor.monitor_power(spec):g}W × {spec.get('count', 1)}대")

    print("\n" + "=" * 60)
    if kepco:
        base = config["tariff"]["household_kwh"]
        print(f" {year}년 월별 전기 사용량 (한전 주택용 누진제, 집 기본 사용량 {base:g}kWh)")
        print("-" * 60)
        print(f"  {'월':>4} {'PC(kWh)':>9} {'집 전체(kWh)':>12} {'단계':>4} {'PC 요금(원)':>11}")
        for r in rows:
            print(f"  {r['month']:>3}월 {r['kwh']:>9.1f} {r['household_kwh']:>12.1f} "
                  f"{r['tier']:>4} {r['cost']:>11,.0f}")
    else:
        print(f" {year}년 월별 전기 사용량 (단가 {config['price_per_kwh']:,.1f}원/kWh)")
        print("-" * 60)
        print(f"  {'월':>4} {'일수':>5} {'PC(kWh)':>12} {'요금(원)':>12}")
        for r in rows:
            print(f"  {r['month']:>3}월 {r['days']:>5} {r['kwh']:>12.1f} {r['cost']:>12,.0f}")
    total_kwh = sum(r["kwh"] for r in rows)
    total_cost = sum(r["cost"] for r in rows)
    print("-" * 60)
    print(f"  연간   PC 사용량 {total_kwh:9.1f} kWh   PC 요금 {total_cost:>10,.0f}원")
    print(f"  월평균 PC 사용량 {total_kwh / 12:9.1f} kWh   PC 요금 {total_cost / 12:>10,.0f}원")
    print("=" * 60)
    if kepco:
        print(" ※ PC 요금 = (집 전체 요금) - (컴퓨터를 뺀 요금). 누진 단계에 따라 달라집니다.")
        print("   부가세·전력산업기반기금·기후환경·연료비조정요금 포함, 복지할인 미반영.")


def load_config(path):
    with open(path, encoding="utf-8") as f:
        user = json.load(f)
    config = new_config()
    config.update({k: v for k, v in user.items() if k not in ("usage", "tariff")})
    config["usage"].update(user.get("usage", {}))
    config["tariff"].update(user.get("tariff", {}))
    if not config["parts"]:
        sys.exit("설정 파일에 parts 항목이 없습니다.")
    return config


def main():
    parser = argparse.ArgumentParser(description="컴퓨터 월별 전기 사용량 계산기")
    parser.add_argument("-a", "--auto", action="store_true", help="내 컴퓨터 자동 감지")
    parser.add_argument("-c", "--config", help="설정 JSON 파일 경로")
    parser.add_argument("-s", "--save", help="결과 설정을 저장할 JSON 경로")
    parser.add_argument("-y", "--year", type=int, default=datetime.date.today().year,
                        help="월별 일수 계산에 사용할 연도 (기본: 올해)")
    parser.add_argument("-p", "--price", type=float, help="단일 단가(원/kWh)로 계산")
    parser.add_argument("-k", "--household", type=float,
                        help="컴퓨터를 뺀 우리 집 월 사용량 kWh (누진제 계산용)")
    args = parser.parse_args()

    try:
        if args.config:
            config = load_config(args.config)
        elif args.auto:
            config, info, detected = auto_config()
            print_detection(info, detected)
        else:
            config = interactive_config()
    except (KeyboardInterrupt, EOFError):
        print("\n취소되었습니다.")
        return
    if args.price is not None:
        config["tariff"]["mode"] = "flat"
        config["price_per_kwh"] = args.price
    if args.household is not None:
        config["tariff"]["household_kwh"] = args.household

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print(f"\n설정을 저장했습니다: {args.save}")

    print_report(config, args.year)


if __name__ == "__main__":
    main()
