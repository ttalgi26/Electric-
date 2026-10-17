# 컴퓨터 월별 전기 사용량 계산기

PC 부품 구성과 사용 시간을 입력하면 **월별 전력 사용량(kWh)과 예상 전기요금**을 계산합니다.
Python 3만 있으면 실행됩니다 (외부 라이브러리 없음).

## 실행

```bash
python3 power_calc.py                     # 대화형으로 부품/사용시간 입력
python3 power_calc.py --save my_pc.json   # 입력 후 설정 저장
python3 power_calc.py -c example_pc.json  # 저장된 설정으로 바로 계산
python3 power_calc.py -c my_pc.json -p 200 -y 2027   # 단가·연도 지정
```

## 계산 방식

- 부품마다 **유휴 전력**과 **최대 전력**을 가집니다.
  - 가벼운 작업: `유휴 + (최대 - 유휴) × 0.2`
  - 고부하 작업: `유휴 + (최대 - 유휴) × 0.8`
- 본체 부품 합계는 파워 효율(기본 85%)로 나눠 콘센트 기준 전력으로 환산합니다. 모니터는 제외(`"external": true`).
- 컴퓨터를 쓰지 않는 시간과 쉬는 날은 대기전력만 계산합니다.
- 각 월의 실제 일수(28~31일)와 주당 사용 일수를 반영해 월별 값을 냅니다.
- 요금 = 사용량 × 단가(기본 150원/kWh). 누진제·기본요금은 반영하지 않습니다.

## 설정 파일 항목

| 항목 | 설명 |
|---|---|
| `parts[].idle_w` / `load_w` | 부품 유휴 / 최대 소비전력 (W) |
| `parts[].count` | 개수 (기본 1) |
| `parts[].external` | `true`면 파워 효율 미적용 (모니터 등) |
| `usage.light_hours` | 하루 가벼운 작업 시간 |
| `usage.heavy_hours` | 하루 게임/렌더링 시간 |
| `usage.days_per_week` | 주당 사용 일수 |
| `usage.standby_w` | 꺼짐/대기 소비전력 (W) |
| `psu_efficiency` | 파워 효율 (0.5~1.0) |
| `light_factor` / `heavy_factor` | 작업 강도별 부하 비율 (기본 0.2 / 0.8) |
| `price_per_kwh` | 전기요금 단가 (원/kWh) |

프리셋 소비전력은 대략적인 값입니다. 정확하게 측정하려면 콘센트형 전력 측정기 값을 직접 넣는 게 좋습니다.
