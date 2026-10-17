"""현재 컴퓨터의 부품 정보를 읽고 부품별 소비전력을 추정한다.

Windows(PowerShell/CIM), Linux(/proc, /sys, lspci), macOS(sysctl, system_profiler)를 지원한다.
"""

import glob
import json
import math
import os
import platform
import re
import subprocess

import monitor

CREATE_NO_WINDOW = 0x08000000


def run(cmd, timeout=40):
    kwargs = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def powershell(script, timeout=40):
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n" + script
    return run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", script], timeout)


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def read_file(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return b"" if "b" in mode else ""


# ---------------------------------------------------------------- 감지

WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$r = [ordered]@{}
$r.cpu = @(Get-CimInstance Win32_Processor | ForEach-Object { $_.Name.Trim() })
$r.gpu = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name })
$r.ram = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [int64]$_.Capacity })
$r.ramTotal = [int64](Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$r.disks = @(Get-PhysicalDisk | ForEach-Object {
    [ordered]@{ name = $_.FriendlyName; media = [string]$_.MediaType; bus = [string]$_.BusType } })
if ($r.disks.Count -eq 0) {
    $r.disks = @(Get-CimInstance Win32_DiskDrive | ForEach-Object {
        [ordered]@{ name = $_.Model; media = ''; bus = [string]$_.InterfaceType } })
}
$r.battery = [bool](Get-CimInstance Win32_Battery)
$r.chassis = @((Get-CimInstance Win32_SystemEnclosure).ChassisTypes | ForEach-Object { [int]$_ })
$conn = @{}
Get-CimInstance -Namespace root\wmi WmiMonitorConnectionParams | ForEach-Object {
    $conn[$_.InstanceName] = [int64]$_.VideoOutputTechnology }
$names = @{}
Get-CimInstance -Namespace root\wmi WmiMonitorID | ForEach-Object {
    $names[$_.InstanceName] = (($_.UserFriendlyName | Where-Object { $_ -ne 0 } |
                                ForEach-Object { [char]$_ }) -join '') }
$bright = @{}
Get-CimInstance -Namespace root\wmi WmiMonitorBrightness | ForEach-Object {
    $bright[$_.InstanceName] = [int]$_.CurrentBrightness }
$r.monitors = @(Get-CimInstance -Namespace root\wmi WmiMonitorBasicDisplayParams | ForEach-Object {
    [ordered]@{ id = $_.InstanceName; name = $names[$_.InstanceName];
                w = [int]$_.MaxHorizontalImageSize; h = [int]$_.MaxVerticalImageSize;
                brightness = $bright[$_.InstanceName];
                internal = ($conn[$_.InstanceName] -eq 2147483648) } })
$r | ConvertTo-Json -Depth 4 -Compress
"""

LAPTOP_CHASSIS = {8, 9, 10, 14, 30, 31, 32}


def inches(w_cm, h_cm):
    if not w_cm or not h_cm:
        return None
    return round(math.hypot(w_cm, h_cm) / 2.54, 1)


def windows_display_modes():
    """연결된 화면별 (모니터 모델 코드, 해상도, 주사율)."""
    import ctypes
    from ctypes import wintypes

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    class DEVMODEW(ctypes.Structure):
        _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                    ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                    ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                    ("dmUnion", ctypes.c_byte * 16), ("dmColor", ctypes.c_short),
                    ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                    ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                    ("dmFormName", wintypes.WCHAR * 32), ("dmLogPixels", wintypes.WORD),
                    ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                    ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                    ("dmDisplayFrequency", wintypes.DWORD), ("dmRest", wintypes.DWORD * 8)]

    user32 = ctypes.windll.user32
    user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                            ctypes.POINTER(DEVMODEW)]
    modes, index = [], 0
    while True:
        adapter = DISPLAY_DEVICEW(cb=ctypes.sizeof(DISPLAY_DEVICEW))
        if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(adapter), 0):
            break
        index += 1
        if not adapter.StateFlags & 1:      # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
            continue
        mode = DEVMODEW(dmSize=ctypes.sizeof(DEVMODEW))
        if not user32.EnumDisplaySettingsW(adapter.DeviceName, 0xFFFFFFFF, ctypes.byref(mode)):
            continue
        mon = DISPLAY_DEVICEW(cb=ctypes.sizeof(DISPLAY_DEVICEW))
        code = None
        if user32.EnumDisplayDevicesW(adapter.DeviceName, 0, ctypes.byref(mon), 0):
            m = re.search(r"MONITOR\\([A-Z0-9]+)\\", mon.DeviceID, re.I)
            code = m.group(1).upper() if m else None
        modes.append({"code": code, "resolution": f"{mode.dmPelsWidth}x{mode.dmPelsHeight}",
                      "refresh": mode.dmDisplayFrequency})
    return modes


def match_modes(monitors, modes):
    """WMI 모니터 목록에 해상도·주사율을 모델 코드로 짝지어 붙인다."""
    unused = list(modes)
    for mon in monitors:
        m = re.search(r"DISPLAY\\([A-Z0-9]+)\\", mon.get("id") or "", re.I)
        code = m.group(1).upper() if m else None
        pick = next((x for x in unused if code and x["code"] == code), None)
        if pick is None and len(unused) == 1 and len(monitors) == 1:
            pick = unused[0]
        if pick:
            unused.remove(pick)
            mon.update(resolution=pick["resolution"], refresh=pick["refresh"])


def detect_windows():
    raw = powershell(WINDOWS_SCRIPT)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    ram = [c for c in as_list(data.get("ram")) if c]
    total = data.get("ramTotal") or sum(ram)
    disks = []
    for d in as_list(data.get("disks")):
        if (d.get("bus") or "").upper() in ("USB", "SD", "MMC"):
            continue
        name = d.get("name") or "디스크"
        media = (d.get("media") or "").upper()
        if media in ("SSD", "HDD"):
            kind = media
        else:
            kind = "HDD" if "HDD" in name.upper() else "SSD"
        disks.append({"name": name, "type": kind})
    laptop = bool(data.get("battery")) or bool(set(as_list(data.get("chassis"))) & LAPTOP_CHASSIS)
    monitors = [{"id": m.get("id"), "name": (m.get("name") or "").strip() or None,
                 "inches": inches(m.get("w"), m.get("h")), "brightness": m.get("brightness")}
                for m in as_list(data.get("monitors")) if not m.get("internal")]
    try:
        match_modes(monitors, windows_display_modes())
    except (OSError, AttributeError, ValueError):
        pass
    return {
        "os": "Windows",
        "cpu": as_list(data.get("cpu")),
        "gpu": as_list(data.get("gpu")),
        "ram_modules": len(ram) or (2 if total > 12 * 2**30 else 1),
        "ram_gb": round(total / 2**30) if total else None,
        "disks": disks,
        "laptop": laptop,
        "monitors": monitors,
    }


def edid_name(edid):
    """EDID 디스크립터에서 모니터 이름(태그 0xFC)을 읽는다."""
    for start in (54, 72, 90, 108):
        block = edid[start:start + 18]
        if len(block) == 18 and block[:3] == b"\0\0\0" and block[3] == 0xFC:
            return block[5:].split(b"\n")[0].decode("ascii", "ignore").strip() or None
    return None


def detect_linux():
    cpus = re.findall(r"^model name\s*:\s*(.+)$", read_file("/proc/cpuinfo"), re.M)
    sockets = set(re.findall(r"^physical id\s*:\s*(\d+)$", read_file("/proc/cpuinfo"), re.M))
    cpu = [cpus[0].strip()] * max(1, len(sockets)) if cpus else []

    gpu = []
    for line in run(["lspci"]).splitlines():
        if re.search(r"VGA compatible|3D controller|Display controller", line):
            gpu.append(line.split(": ", 1)[-1])

    mem_kb = re.search(r"MemTotal:\s*(\d+)", read_file("/proc/meminfo"))
    total = int(mem_kb.group(1)) * 1024 if mem_kb else 0
    modules = len(re.findall(r"^\s*Size:\s*\d+\s*[GM]B", run(["dmidecode", "-t", "17"]), re.M))

    disks = []
    for path in glob.glob("/sys/block/*"):
        name = os.path.basename(path)
        if re.match(r"(loop|ram|zram|dm-|sr|md|fd|nbd)", name):
            continue
        if read_file(f"{path}/removable").strip() == "1":
            continue
        rotational = read_file(f"{path}/queue/rotational").strip() == "1"
        disks.append({"name": name, "type": "HDD" if rotational else "SSD"})

    monitors = []
    for status in glob.glob("/sys/class/drm/card*-*/status"):
        if read_file(status).strip() != "connected":
            continue
        connector = os.path.basename(os.path.dirname(status))
        if re.search(r"eDP|LVDS|DSI", connector):
            continue
        base = os.path.dirname(status)
        edid = read_file(os.path.join(base, "edid"), "rb")
        modes = read_file(os.path.join(base, "modes")).split()
        monitors.append({"inches": inches(edid[21], edid[22]) if len(edid) > 22 else None,
                         "name": edid_name(edid),
                         "resolution": modes[0] if modes else None})

    laptop = bool(glob.glob("/sys/class/power_supply/BAT*"))
    return {
        "os": "Linux",
        "cpu": cpu,
        "gpu": gpu,
        "ram_modules": modules or (2 if total > 12 * 2**30 else 1),
        "ram_gb": round(total / 2**30) if total else None,
        "disks": disks,
        "laptop": laptop,
        "monitors": monitors,
    }


def detect_mac():
    cpu = run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
    gpu, monitors = [], []
    try:
        data = json.loads(run(["system_profiler", "SPDisplaysDataType", "-json"]))
        for card in data.get("SPDisplaysDataType", []):
            gpu.append(card.get("sppci_model", ""))
            for disp in card.get("spdisplays_ndrvs", []):
                if disp.get("spdisplays_connection_type") == "spdisplays_internal":
                    continue
                res = re.search(r"(\d+) x (\d+)(?: @ ([\d.]+)\s*Hz)?",
                                str(disp.get("_spdisplays_resolution", "")))
                monitors.append({"inches": None, "name": disp.get("_name"),
                                 "resolution": f"{res.group(1)}x{res.group(2)}" if res else None,
                                 "refresh": float(res.group(3)) if res and res.group(3) else None})
    except ValueError:
        pass
    total = int(run(["sysctl", "-n", "hw.memsize"]).strip() or 0)
    apple = cpu.startswith("Apple")
    return {
        "os": "macOS",
        "cpu": [cpu] if cpu else [],
        "gpu": [] if apple else gpu,
        "ram_modules": 0 if apple else 2,   # Apple 실리콘은 메모리가 칩에 포함됨
        "ram_gb": round(total / 2**30) if total else None,
        "disks": [{"name": "내장 SSD", "type": "SSD"}],
        "laptop": "InternalBattery" in run(["pmset", "-g", "batt"]),
        "monitors": monitors,
    }


def detect():
    """현재 컴퓨터 정보. 감지 실패 시 빈 항목으로 채운 dict."""
    system = platform.system()
    info = None
    if system == "Windows":
        info = detect_windows()
    elif system == "Linux":
        info = detect_linux()
    elif system == "Darwin":
        info = detect_mac()
    return info or {"os": system, "cpu": [], "gpu": [], "ram_modules": 2, "ram_gb": None,
                    "disks": [], "laptop": False, "monitors": []}


# ---------------------------------------------------------------- 소비전력 추정

def clean(name):
    name = re.sub(r"\((TM|R|C)\)|®|™", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip()


def estimate_cpu(name, laptop):
    """CPU 이름으로 (유휴 W, 최대 W) 추정."""
    n = clean(name).upper()
    if n.startswith("APPLE"):
        if "ULTRA" in n:
            return 15, 120
        if "MAX" in n:
            return 8, 70
        if "PRO" in n:
            return 5, 40
        return 3, 22
    if "THREADRIPPER" in n:
        return 40, 350
    if "XEON" in n or "EPYC" in n:
        return 30, 250
    if "RYZEN AI" in n:
        return 4, 54

    m = (re.search(r"\bI([3579])-\d{4,5}([A-Z]*\d?)", n)
         or re.search(r"ULTRA ([3579]) \d{3}([A-Z]*)", n)
         or re.search(r"CORE ([3579]) \d{3}([A-Z]*)", n))
    if m:
        tier, suffix = int(m.group(1)), m.group(2)
        if re.match(r"(U|Y|V|G\d)", suffix):
            return 3, 28
        if suffix.startswith("HX"):
            return 6, 140
        if suffix.startswith(("H", "P")):
            return 5, 60
        if suffix.startswith("T"):
            return 8, 35
        if "K" in suffix:
            return 10, {9: 250, 7: 220, 5: 160, 3: 100}[tier]
        return 10, {9: 180, 7: 160, 5: 120, 3: 80}[tier]

    m = re.search(r"RYZEN ([3579]) (?:PRO )?\d{3,4}([A-Z0-9]*)", n)
    if m:
        tier, suffix = int(m.group(1)), m.group(2)
        if suffix.startswith("U"):
            return 3, 28
        if suffix.startswith("HX"):
            return 6, 120
        if suffix.startswith("H"):
            return 5, 54
        if suffix.startswith("GE"):
            return 8, 45
        if suffix.startswith("G"):
            return 8, 88
        if suffix.endswith("X3D"):
            return 20, {9: 160, 7: 120, 5: 100, 3: 90}[tier]
        if suffix.startswith("X"):
            return 20, {9: 220, 7: 140, 5: 110, 3: 80}[tier]
        return 20, {9: 120, 7: 88, 5: 88, 3: 65}[tier]
    return (5, 45) if laptop else (15, 120)


# (모델, 최대 W) — 위에서부터 먼저 맞는 항목을 쓰므로 긴 이름을 앞에 둔다.
GPU_TABLE = [
    ("RTX 5090", 575), ("RTX 5080", 360), ("RTX 5070 TI", 300), ("RTX 5070", 250),
    ("RTX 5060 TI", 180), ("RTX 5060", 145), ("RTX 5050", 130),
    ("RTX 4090", 450), ("RTX 4080", 320), ("RTX 4070 TI", 285), ("RTX 4070 SUPER", 220),
    ("RTX 4070", 200), ("RTX 4060 TI", 160), ("RTX 4060", 115), ("RTX 4050", 100),
    ("RTX 3090 TI", 450), ("RTX 3090", 350), ("RTX 3080 TI", 350), ("RTX 3080", 320),
    ("RTX 3070 TI", 290), ("RTX 3070", 220), ("RTX 3060 TI", 200), ("RTX 3060", 170),
    ("RTX 3050", 130), ("RTX 2080 TI", 250), ("RTX 2080", 215), ("RTX 2070", 175),
    ("RTX 2060", 160), ("GTX 1660", 120), ("GTX 1650", 75), ("GTX 1080 TI", 250),
    ("GTX 1080", 180), ("GTX 1070", 150), ("GTX 1060", 120), ("GTX 1050", 75),
    ("RX 9070 XT", 304), ("RX 9070", 220), ("RX 9060 XT", 160),
    ("RX 7900 XTX", 355), ("RX 7900 XT", 315), ("RX 7900 GRE", 260), ("RX 7800 XT", 263),
    ("RX 7700 XT", 245), ("RX 7600", 165), ("RX 6950", 335), ("RX 6900", 300),
    ("RX 6800 XT", 300), ("RX 6800", 250), ("RX 6750", 250), ("RX 6700", 230),
    ("RX 6650", 180), ("RX 6600", 132), ("RX 6500", 107), ("RX 580", 185), ("RX 570", 150),
    ("ARC A770", 225), ("ARC A750", 225), ("ARC A580", 185), ("ARC A380", 75),
    ("ARC B580", 190), ("ARC B570", 150),
]
GPU_PATTERNS = [(re.compile(r"\b" + r"\s*".join(map(re.escape, model.split())) + r"\b"), watt)
                for model, watt in GPU_TABLE]
VIRTUAL_GPU = ("BASIC DISPLAY", "BASIC RENDER", "REMOTE", "VIRTUAL", "PARSEC", "MIRROR",
               "VMWARE", "VIRTUALBOX", "HYPER-V", "QXL", "CIRRUS", "SPICE", "LLVMPIPE")
INTEGRATED_GPU = re.compile(r"UHD|IRIS|HD GRAPHICS|INTEL GRAPHICS|ARC GRAPHICS|"
                            r"RADEON GRAPHICS|RADEON \d{3}M|VEGA \d|APPLE|ADRENO|"
                            r"INTEL CORPORATION")


def estimate_gpu(name):
    """(유휴 W, 최대 W, 외장 여부). 가상 어댑터면 None."""
    n = clean(name).upper()
    if not n or any(k in n for k in VIRTUAL_GPU):
        return None
    mobile = bool(re.search(r"LAPTOP|MOBILE|MAX-Q", n))
    for pattern, watt in GPU_PATTERNS:
        if pattern.search(n):
            if mobile:
                return 3, round(watt * 0.45), True
            return max(8, round(watt * 0.05)), watt, True
    if INTEGRATED_GPU.search(n):
        return 0, 10, False
    if re.search(r"NVIDIA|GEFORCE|QUADRO|\bRX\b|RADEON PRO|\bARC\b", n):
        return (3, 60, True) if mobile else (12, 180, True)
    return 0, 10, False


def build_parts(info):
    """감지 정보를 계산용 부품 목록으로 바꾼다. (모니터는 build_monitors)"""
    laptop = info.get("laptop")
    parts = []

    cpus = info.get("cpu") or []
    for name in cpus:
        idle, load = estimate_cpu(name, laptop)
        parts.append({"name": clean(name), "kind": "cpu", "idle_w": idle, "load_w": load,
                      "note": "자동 감지"})
    if not cpus:
        idle, load = (5, 45) if laptop else (15, 120)
        parts.append({"name": "CPU (감지 실패)", "kind": "cpu", "idle_w": idle, "load_w": load,
                      "note": "기본값"})

    gpus = []
    for name in info.get("gpu") or []:
        est = estimate_gpu(name)
        if est:
            gpus.append((clean(name), *est))
    if any(discrete for *_, discrete in gpus):
        gpus = [g for g in gpus if g[3]]     # 외장 GPU가 있으면 내장 그래픽은 제외
    for name, idle, load, discrete in gpus:
        parts.append({"name": name, "kind": "gpu" if discrete else "igpu", "idle_w": idle,
                      "load_w": load, "note": "자동 감지" if discrete else "내장 그래픽"})

    if laptop:
        parts.append({"name": "노트북 본체 (메인보드·내장 화면 등)", "kind": "board",
                      "idle_w": 6, "load_w": 15, "note": "추정값"})
    else:
        parts.append({"name": "메인보드·칩셋", "kind": "board", "idle_w": 20, "load_w": 40,
                      "note": "추정값"})

    modules = info.get("ram_modules") or 0
    if modules:
        gb = f", 총 {info['ram_gb']}GB" if info.get("ram_gb") else ""
        idle, load = (1, 3) if laptop else (2, 4)
        parts.append({"name": f"RAM{gb}", "kind": "ram", "idle_w": idle, "load_w": load,
                      "count": modules, "note": "자동 감지"})

    ssd = sum(d["type"] == "SSD" for d in info.get("disks", []))
    hdd = sum(d["type"] == "HDD" for d in info.get("disks", []))
    if ssd:
        parts.append({"name": "SSD", "kind": "ssd", "idle_w": 1, "load_w": 5, "count": ssd,
                      "note": "자동 감지"})
    if hdd:
        parts.append({"name": "HDD", "kind": "hdd", "idle_w": 5, "load_w": 9, "count": hdd,
                      "note": "자동 감지"})

    if not laptop:
        parts.append({"name": "케이스 팬", "kind": "fan", "idle_w": 1, "load_w": 3, "count": 3,
                      "note": "감지 불가 · 개수 확인"})
    return parts


def build_monitors(info):
    """감지된 외부 모니터를 모니터 사양 목록으로 바꾼다."""
    specs = []
    for mon in info.get("monitors", []):
        size = mon.get("inches") or 27
        name = mon.get("name") or (f"{size:.0f}인치 모니터" if mon.get("inches")
                                   else "모니터 (크기 미확인)")
        specs.append(monitor.make_spec(
            size=round(size, 1), resolution=mon.get("resolution") or "1920x1080",
            refresh=mon.get("refresh") or 60,
            panel="OLED" if "OLED" in name.upper() else "LCD",
            brightness=mon.get("brightness") if mon.get("brightness") is not None else 70,
            name=name))
    return specs
