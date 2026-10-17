#!/usr/bin/env python3
"""컴퓨터 월별 전기 사용량 계산기.

부품 구성(부품별 유휴/최대 소비전력)과 사용 패턴(하루 사용 시간, 주당 사용 일수)으로
월별 전력 사용량(kWh)과 예상 전기요금을 계산한다.

사용법:
    python3 power_calc.py                    # 대화형 입력
    python3 power_calc.py -c my_pc.json      # 설정 파일로 계산
    python3 power_calc.py --save my_pc.json  # 대화형 입력 후 설정 저장
"""

import argparse
import calendar
import datetime
import json
import sys

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

MONITOR_PRESETS = [
    ("모니터 없음 / 계산 제외", 0, 0),
    ("24인치 모니터", 18, 25),
    ("27인치 모니터", 25, 40),
    ("32인치 이상 / 고주사율 모니터", 35, 60),
]

DEFAULT_CONFIG = {
    "parts": [],
    "usage": {
        "light_hours": 4.0,     # 웹서핑/문서작업 시간 (하루)
        "heavy_hours": 2.0,     # 게임/렌더링 시간 (하루)
        "days_per_week": 7,     # 주당 사용 일수
        "standby_w": 2.0,       # 꺼짐/대기 상태 소비전력
    },
    "psu_efficiency": 0.85,     # 파워서플라이 효율 (80 PLUS 브론즈~골드 ≈ 0.85~0.90)
    "light_factor": 0.2,        # 가벼운 작업 시 (최대-유휴) 대비 추가 부하 비율
    "heavy_factor": 0.8,        # 고부하 작업 시 (최대-유휴) 대비 부하 비율
    "price_per_kwh": 150.0,     # 전기요금 단가 (원/kWh)
}


def part_power(part, factor):
    """부하 비율에 따른 부품 1세트의 소비전력(W)."""
    idle, peak = part["idle_w"], part["load_w"]
    return (idle + (peak - idle) * factor) * part.get("count", 1)


def daily_kwh(config):
    """하루 소비전력량(kWh)과 부품별 하루 소비량을 반환한다."""
    usage = config["usage"]
    light_h, heavy_h = usage["light_hours"], usage["heavy_hours"]
    off_h = max(0.0, 24 - light_h - heavy_h)
    eff = config["psu_efficiency"]

    per_part = {}
    for part in config["parts"]:
        wh = (part_power(part, config["light_factor"]) * light_h
              + part_power(part, config["heavy_factor"]) * heavy_h)
        # 모니터는 콘센트에 직접 연결되므로 파워 효율을 적용하지 않는다.
        if not part.get("external"):
            wh /= eff
        per_part[part["name"]] = wh / 1000
    standby = usage["standby_w"] * off_h / 1000
    per_part["대기전력 (꺼짐 상태)"] = standby
    return sum(per_part.values()), per_part


def monthly_report(config, year):
    """연도의 월별 (월, 사용일수, kWh, 요금) 목록."""
    total_day, _ = daily_kwh(config)
    use_ratio = config["usage"]["days_per_week"] / 7
    # 사용하지 않는 날은 대기전력만 소비한다.
    idle_day = config["usage"]["standby_w"] * 24 / 1000
    rows = []
    for month in range(1, 13):
        days = calendar.monthrange(year, month)[1]
        used = days * use_ratio
        kwh = total_day * used + idle_day * (days - used)
        rows.append((month, days, kwh, kwh * config["price_per_kwh"]))
    return rows


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
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    print("=" * 60)
    print(" 컴퓨터 월별 전기 사용량 계산기")
    print(" (Enter를 누르면 [ ] 안의 기본값이 사용됩니다)")
    print("=" * 60)

    parts = config["parts"]
    for key, title in (("cpu", "CPU"), ("gpu", "그래픽카드"), ("motherboard", "메인보드")):
        name, idle, peak = choose(title, PRESETS[key])
        parts.append({"name": name, "idle_w": idle, "load_w": peak})

    print("\n[기타 부품 개수]")
    defaults = {"ram": 2, "ssd": 1, "hdd": 0, "fan": 3}
    for key, (name, idle, peak) in COUNTED_PARTS.items():
        count = ask_int(f"  {name}", defaults[key])
        if count:
            parts.append({"name": name.split(" (")[0], "idle_w": idle,
                          "load_w": peak, "count": count})

    name, idle, peak = choose("모니터", MONITOR_PRESETS)
    if peak:
        count = ask_int("  모니터 개수", 1, 1)
        parts.append({"name": name, "idle_w": idle, "load_w": peak,
                      "count": count, "external": True})

    print("\n[사용 패턴]")
    u = config["usage"]
    u["light_hours"] = ask_float("  하루 가벼운 작업 시간 (웹/문서/영상)", u["light_hours"], 0, 24)
    u["heavy_hours"] = ask_float("  하루 고부하 작업 시간 (게임/렌더링)", u["heavy_hours"],
                                 0, 24 - u["light_hours"])
    u["days_per_week"] = ask_float("  주당 사용 일수", u["days_per_week"], 0, 7)
    u["standby_w"] = ask_float("  꺼짐/대기 상태 소비전력 W", u["standby_w"])

    print("\n[기타 설정]")
    config["psu_efficiency"] = ask_float("  파워 효율 (0.8~0.95)", config["psu_efficiency"], 0.5, 1.0)
    config["price_per_kwh"] = ask_float("  전기요금 단가 (원/kWh)", config["price_per_kwh"])
    return config


# ---------------------------------------------------------------- 출력

def print_report(config, year):
    total_day, per_part = daily_kwh(config)
    rows = monthly_report(config, year)
    price = config["price_per_kwh"]

    print("\n" + "=" * 60)
    print(" 부품별 하루 소비 전력량")
    print("-" * 60)
    for name, kwh in sorted(per_part.items(), key=lambda kv: -kv[1]):
        share = kwh / total_day * 100 if total_day else 0
        print(f"  {name:<36} {kwh * 1000:8.1f} Wh  ({share:4.1f}%)")
    print("-" * 60)
    print(f"  {'하루 합계 (사용하는 날)':<34} {total_day:8.3f} kWh")

    print("\n" + "=" * 60)
    print(f" {year}년 월별 전기 사용량 (단가 {price:,.1f}원/kWh)")
    print("-" * 60)
    print(f"  {'월':>4} {'일수':>5} {'사용량(kWh)':>12} {'요금(원)':>12}")
    for month, days, kwh, cost in rows:
        print(f"  {month:>3}월 {days:>5} {kwh:>12.2f} {cost:>12,.0f}")
    total_kwh = sum(r[2] for r in rows)
    total_cost = sum(r[3] for r in rows)
    print("-" * 60)
    print(f"  {'연간':>4} {'':>5} {total_kwh:>12.2f} {total_cost:>12,.0f}")
    print(f"  {'월평균':>3} {'':>5} {total_kwh / 12:>12.2f} {total_cost / 12:>12,.0f}")
    print("=" * 60)
    print(" ※ 요금은 단순 단가 기준입니다. 실제 고지서는 누진제·기본요금·")
    print("   다른 가전 사용량에 따라 달라집니다.")


def load_config(path):
    with open(path, encoding="utf-8") as f:
        user = json.load(f)
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config.update({k: v for k, v in user.items() if k != "usage"})
    config["usage"].update(user.get("usage", {}))
    if not config["parts"]:
        sys.exit("설정 파일에 parts 항목이 없습니다.")
    return config


def main():
    parser = argparse.ArgumentParser(description="컴퓨터 월별 전기 사용량 계산기")
    parser.add_argument("-c", "--config", help="설정 JSON 파일 경로")
    parser.add_argument("-s", "--save", help="대화형 입력 결과를 저장할 JSON 경로")
    parser.add_argument("-y", "--year", type=int, default=datetime.date.today().year,
                        help="월별 일수 계산에 사용할 연도 (기본: 올해)")
    parser.add_argument("-p", "--price", type=float, help="전기요금 단가 (원/kWh) 덮어쓰기")
    args = parser.parse_args()

    try:
        config = load_config(args.config) if args.config else interactive_config()
    except (KeyboardInterrupt, EOFError):
        print("\n취소되었습니다.")
        return
    if args.price is not None:
        config["price_per_kwh"] = args.price

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print(f"\n설정을 저장했습니다: {args.save}")

    print_report(config, args.year)


if __name__ == "__main__":
    main()
