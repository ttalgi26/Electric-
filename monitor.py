"""모니터 소비전력 모델.

크기·화면비(해상도)·밝기·패널 종류·주사율·HDR로 켜져 있을 때의 소비전력을 추정한다.
기준: 27인치 QHD LCD, 밝기 70%에서 약 25W (일반적인 제조사 표기 범위).
"""

import re

PANELS = {"LCD": "LCD (IPS/VA/TN)", "OLED": "OLED", "MINILED": "미니 LED"}
STANDBY_W = 0.3          # 모니터 대기 전력 (PC 꺼짐/화면 꺼짐)
LIGHT_W_PER_IN2 = 0.085  # 밝기 100%일 때 화면 면적(제곱인치)당 백라이트/발광 전력
ELECTRONICS_W = 5.0      # 회로·스케일러 기본 전력


def parse_resolution(text):
    m = re.match(r"\s*(\d+)\s*[x×*]\s*(\d+)", str(text or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def make_spec(size=27, resolution="1920x1080", refresh=60, panel="LCD", brightness=70,
              hdr=False, name=None, count=1):
    return {"name": name or f"{size:g}인치 모니터", "size": size, "resolution": resolution,
            "refresh": refresh, "panel": panel, "brightness": brightness, "hdr": hdr,
            "count": count}


def monitor_power(spec):
    """화면이 켜져 있을 때 모니터 1대의 소비전력(W)."""
    size = float(spec.get("size") or 27)
    w, h = parse_resolution(spec.get("resolution")) or (16, 9)
    area = size ** 2 * w * h / (w * w + h * h)
    bright = min(max(float(spec.get("brightness", 70)) / 100, 0.0), 1.0)
    panel = spec.get("panel", "LCD")

    full = LIGHT_W_PER_IN2 * area
    if panel == "OLED":
        light = full * 0.9 * bright           # 자발광: 밝기에 거의 비례
    else:
        light = full * (0.2 + 0.8 * bright)   # 백라이트: 밝기 0%여도 일부 소비
        if panel == "MINILED":
            light *= 1.2
    if spec.get("hdr"):
        light *= 1.3

    refresh = float(spec.get("refresh") or 60)
    electronics = ELECTRONICS_W
    if refresh >= 120:
        electronics += 3
    if refresh >= 200:
        electronics += 3
    if w * h >= 3840 * 2160 * 0.9:
        electronics += 2
    return round(electronics + light, 1)


def describe(spec):
    parts = [f"{float(spec.get('size') or 0):g}\"", spec.get("resolution") or "?",
             f"{spec.get('refresh') or 60:g}Hz", PANELS.get(spec.get("panel"), "LCD"),
             f"밝기 {spec.get('brightness', 70):g}%"]
    if spec.get("hdr"):
        parts.append("HDR")
    return " · ".join(parts)
