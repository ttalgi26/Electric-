"""시스템 기록에서 최근 컴퓨터가 켜져 있던 시간을 읽는다.

- Windows: 시스템 이벤트 로그 (부팅/종료/절전/절전 해제)
- Linux: journalctl --list-boots (절전 시간은 구분하지 못함)
"""

import datetime
import json
import platform
import re
from collections import defaultdict

from hardware import powershell, run

WINDOWS_SCRIPT = r"""
$since = (Get-Date).AddDays(-{days})
$oldest = Get-WinEvent -LogName System -MaxEvents 1 -Oldest -ErrorAction SilentlyContinue
if ($oldest) {{ 'OLDEST|' + $oldest.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss') }}
$filter = @(
  @{{LogName='System'; ProviderName='Microsoft-Windows-Kernel-General'; Id=12,13; StartTime=$since}},
  @{{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=42,107,506,507; StartTime=$since}},
  @{{LogName='System'; ProviderName='Microsoft-Windows-Power-Troubleshooter'; Id=1; StartTime=$since}}
)
Get-WinEvent -FilterHashtable $filter -ErrorAction SilentlyContinue | Sort-Object TimeCreated |
  ForEach-Object {{ '{{0}}|{{1}}|{{2}}' -f $_.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss'), $_.ProviderName, $_.Id }}
"""

# (공급자 접미사, 이벤트 ID) -> 상태
WINDOWS_EVENTS = {
    ("Kernel-General", 12): "boot",     # 운영체제 시작
    ("Kernel-General", 13): "off",      # 운영체제 종료
    ("Kernel-Power", 42): "off",        # 절전/최대 절전 진입
    ("Kernel-Power", 506): "off",       # 모던 스탠바이 진입
    ("Kernel-Power", 107): "on",        # 절전에서 복귀
    ("Kernel-Power", 507): "on",        # 모던 스탠바이 해제
    ("Power-Troubleshooter", 1): "on",  # 절전에서 복귀
}

TIME_FMT = "%Y-%m-%dT%H:%M:%S"


def windows_intervals(since, now, days):
    events, oldest = [], None
    for line in powershell(WINDOWS_SCRIPT.format(days=days), timeout=90).splitlines():
        parts = line.strip().split("|")
        try:
            when = datetime.datetime.strptime(parts[1] if parts[0] == "OLDEST" else parts[0],
                                              TIME_FMT)
        except (ValueError, IndexError):
            continue
        if parts[0] == "OLDEST":
            oldest = when
            continue
        provider = parts[1].replace("Microsoft-Windows-", "")
        kind = WINDOWS_EVENTS.get((provider, int(parts[2])))
        if kind:
            events.append((when, kind))
    if not events:
        return None, None

    intervals, start, last = [], None, since
    if events[0][1] == "off":
        start = since
    for when, kind in events:
        if kind == "off":
            if start is not None:
                intervals.append((start, when))
                start = None
        elif kind == "boot":
            if start is not None:   # 비정상 종료: 마지막 기록 시점에 꺼진 것으로 본다
                intervals.append((start, last))
            start = when
        elif start is None:
            start = when
        last = when
    if start is not None:
        intervals.append((start, now))
    return intervals, oldest


def linux_intervals(since, now):
    boots = []
    try:
        for boot in json.loads(run(["journalctl", "--list-boots", "-o", "json", "--no-pager"])):
            boots.append((datetime.datetime.fromtimestamp(boot["first_entry"] / 1e6),
                          datetime.datetime.fromtimestamp(boot["last_entry"] / 1e6)))
    except (ValueError, KeyError, TypeError):
        stamp = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
        for line in run(["journalctl", "--list-boots", "--no-pager"]).splitlines():
            m = re.search(stamp + r".*?" + stamp, line)
            if m:
                boots.append(tuple(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                                   for s in m.groups()))
    if not boots:
        return None, None
    boots.sort()
    boots[-1] = (boots[-1][0], now)     # 현재 부팅은 지금까지 켜져 있음
    return boots, boots[0][0]


def summarize(intervals, since, now, oldest):
    start = max(since, oldest or since)
    window_days = (now - start).total_seconds() / 86400
    if window_days < 1:
        return None
    per_day = defaultdict(float)
    for s, e in intervals:
        s, e = max(s, start), min(e, now)
        while s < e:
            midnight = datetime.datetime.combine(s.date() + datetime.timedelta(days=1),
                                                 datetime.time())
            chunk = min(e, midnight)
            per_day[s.date()] += (chunk - s).total_seconds() / 3600
            s = chunk
    total = sum(per_day.values())
    active = sum(1 for h in per_day.values() if h >= 0.5)
    if not active:
        return None
    return {
        "window_days": round(window_days, 1),
        "total_hours": round(total, 1),
        "active_days": active,
        "hours_per_active_day": round(min(24.0, total / active), 1),
        "days_per_week": round(min(7.0, active / window_days * 7), 1),
    }


def detect_usage(days=30):
    """최근 days일 동안의 사용 통계. 기록을 읽을 수 없으면 None."""
    now = datetime.datetime.now()
    since = now - datetime.timedelta(days=days)
    system = platform.system()
    if system == "Windows":
        intervals, oldest = windows_intervals(since, now, days)
        source = "Windows 이벤트 로그"
    elif system == "Linux":
        intervals, oldest = linux_intervals(since, now)
        source = "systemd 부팅 기록"
    else:
        return None
    if not intervals:
        return None
    result = summarize(intervals, since, now, oldest)
    if result:
        result["source"] = source
    return result
