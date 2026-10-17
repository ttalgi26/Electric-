"""한전 주택용(저압) 누진제 전기요금 계산.

요금표가 바뀌면 아래 상수만 고치면 된다.
기준: 2023년 5월 인상 이후 동결된 주택용 저압 요금 (2026년 4분기 동결 보도 기준).
"""

import math

BASIC_CHARGE = (910, 1600, 7300)        # 기본요금 (원/호), 1·2·3단계
ENERGY_RATE = (120.0, 214.6, 307.3)     # 전력량요금 (원/kWh), 1·2·3단계
TIER_LIMITS = (200, 400)                # 기타계절 단계 경계 (kWh)
SUMMER_TIER_LIMITS = (300, 450)         # 하계(7~8월) 단계 경계 (kWh)
SUMMER_MONTHS = (7, 8)
CLIMATE_RATE = 9.0                      # 기후환경요금 (원/kWh)
FUEL_RATE = 5.0                         # 연료비조정요금 (원/kWh)
VAT_RATE = 0.10                         # 부가가치세
FUND_RATE = 0.027                       # 전력산업기반기금


def tier_limits(month):
    return SUMMER_TIER_LIMITS if month in SUMMER_MONTHS else TIER_LIMITS


def tier_of(kwh, month):
    low, high = tier_limits(month)
    return 1 if kwh <= low else 2 if kwh <= high else 3


def kepco_bill(kwh, month, climate=CLIMATE_RATE, fuel=FUEL_RATE, fund_rate=FUND_RATE):
    """한 달 사용량(kWh)에 대한 청구 요금 내역(원)."""
    kwh = max(0, round(kwh))
    low, high = tier_limits(month)
    tier = tier_of(kwh, month)
    basic = BASIC_CHARGE[tier - 1]
    energy = (min(kwh, low) * ENERGY_RATE[0]
              + max(0, min(kwh, high) - low) * ENERGY_RATE[1]
              + max(0, kwh - high) * ENERGY_RATE[2])
    # 전기요금계는 원 미만 절사, 부가세는 반올림, 기금과 청구액은 10원 미만 절사.
    subtotal = math.floor(basic + energy + kwh * climate + kwh * fuel)
    vat = round(subtotal * VAT_RATE)
    fund = math.floor(subtotal * fund_rate / 10) * 10
    total = math.floor((subtotal + vat + fund) / 10) * 10
    return {"kwh": kwh, "tier": tier, "basic": basic, "energy": energy,
            "subtotal": subtotal, "vat": vat, "fund": fund, "total": total}
