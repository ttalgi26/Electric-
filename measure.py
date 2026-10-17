"""실제로 부하를 걸거나 프로그램을 실행하면서 컴퓨터 전력을 측정한다.

센서가 있으면 실측값을, 없으면 사용률(%)과 부품별 전력 추정치로 계산한다.
- CPU 사용률: Windows 성능 카운터 / Linux /proc/stat
- CPU 전력 (실측): Linux Intel·AMD RAPL (/sys/class/powercap)
- GPU 사용률·전력 (실측): NVIDIA nvidia-smi, Linux AMD sysfs, Windows GPU 엔진 카운터(사용률)
- 시스템 전력 (실측): 노트북을 배터리로 쓰는 동안의 방전 전력

사용법:
    python measure.py                 # 내장 부하 테스트 전체 (각 20초)
    python measure.py --seconds 30 --tests idle cpu_all
    python measure.py --custom 게임 --seconds 120   # 직접 프로그램을 실행하며 측정
"""

import argparse
import glob
import multiprocessing as mp
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time

import monitor
import power_calc as pc

CREATE_NO_WINDOW = 0x08000000
WARMUP_S = 3          # 부하 시작 직후 값은 평균에서 제외
IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------- 부하 작업 (별도 프로세스)

def cpu_worker(stop):
    x = 1
    while not stop.is_set():
        for _ in range(100000):
            x = (x * 1103515245 + 12345) & 0xFFFFFFFF


def memory_worker(stop):
    src = bytearray(os.urandom(1 << 20)) * 128      # 128MB
    dst = bytearray(len(src))
    while not stop.is_set():
        dst[:] = src


def disk_worker(stop):
    path = os.path.join(tempfile.gettempdir(), f"pcpower_test_{os.getpid()}.bin")
    chunk = os.urandom(8 << 20)
    try:
        with open(path, "wb+") as f:
            while not stop.is_set():
                f.seek(0)
                for _ in range(16):                 # 128MB 쓰기
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
                f.seek(0)
                while f.read(8 << 20):
                    pass
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


CORES = os.cpu_count() or 1

# (키, 이름, 작업 함수, 프로세스 수)
TESTS = [
    ("idle", "유휴 (아무것도 안 함)", None, 0),
    ("cpu_1", "CPU 1코어 (가벼운 작업)", cpu_worker, 1),
    ("cpu_half", "CPU 절반 코어", cpu_worker, max(1, CORES // 2)),
    ("cpu_all", "CPU 전체 코어 (최대 부하)", cpu_worker, CORES),
    ("memory", "메모리 복사", memory_worker, max(1, CORES // 2)),
    ("disk", "디스크 읽기/쓰기", disk_worker, 1),
]
TEST_NAMES = {key: name for key, name, *_ in TESTS}


class Load:
    """부하 작업 프로세스 묶음."""

    def __init__(self, key):
        _, _, func, procs = next(t for t in TESTS if t[0] == key)
        self.ctx = mp.get_context("spawn")
        self.stop_event = self.ctx.Event()
        self.procs = [self.ctx.Process(target=func, args=(self.stop_event,), daemon=True)
                      for _ in range(procs if func else 0)]

    def __enter__(self):
        for p in self.procs:
            p.start()
        return self

    def __exit__(self, *exc):
        self.stop_event.set()
        for p in self.procs:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()


# ---------------------------------------------------------------- 센서

class LineReader:
    """계속 출력하는 외부 프로그램의 마지막 줄을 읽는다."""

    def __init__(self, cmd, parse):
        self.parse, self.value = parse, None
        kwargs = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, **kwargs)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            try:
                self.parse(self, raw.decode("utf-8", "replace").strip())
            except (ValueError, IndexError):
                pass

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()


class Pdh:
    """Windows 성능 카운터 (영문 경로 사용 → 한글 Windows에서도 동작)."""

    def __init__(self, paths):
        import ctypes
        from ctypes import wintypes

        class Value(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class Item(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", Value)]

        self.ctypes, self.wintypes, self.Value, self.Item = ctypes, wintypes, Value, Item
        self.pdh = ctypes.WinDLL("pdh")
        for fn in ("PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData",
                   "PdhGetFormattedCounterArrayW"):
            getattr(self.pdh, fn).restype = ctypes.c_uint32
        self.query = ctypes.c_void_p()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)):
            raise OSError("PdhOpenQuery 실패")
        self.counters = {}
        for key, path in paths.items():
            handle = ctypes.c_void_p()
            if self.pdh.PdhAddEnglishCounterW(self.query, ctypes.c_wchar_p(path), None,
                                              ctypes.byref(handle)) == 0:
                self.counters[key] = handle
        self.pdh.PdhCollectQueryData(self.query)

    def read(self):
        """{키: [(인스턴스 이름, 값), ...]}"""
        ctypes, wintypes = self.ctypes, self.wintypes
        if self.pdh.PdhCollectQueryData(self.query):
            return {}
        fmt = 0x00000200 | 0x00008000           # PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        out = {}
        for key, handle in self.counters.items():
            size, count = wintypes.DWORD(0), wintypes.DWORD(0)
            status = self.pdh.PdhGetFormattedCounterArrayW(handle, fmt, ctypes.byref(size),
                                                           ctypes.byref(count), None)
            if status != 0x800007D2 or not size.value:     # PDH_MORE_DATA
                continue
            buf = (ctypes.c_byte * size.value)()
            if self.pdh.PdhGetFormattedCounterArrayW(handle, fmt, ctypes.byref(size),
                                                     ctypes.byref(count), buf):
                continue
            items = ctypes.cast(buf, ctypes.POINTER(self.Item))
            out[key] = [(items[i].szName, items[i].FmtValue.doubleValue)
                        for i in range(count.value) if items[i].FmtValue.CStatus in (0, 1)]
        return out


def read_int(path):
    with open(path) as f:
        return int(f.read().strip())


class Sampler:
    """1초마다 사용률과 센서 값을 기록한다."""

    def __init__(self):
        self.samples, self.lock = [], threading.Lock()
        self.latest = {}
        self.sources = {}       # 항목 -> 측정 방식 설명
        self.readers = []
        self._stop = threading.Event()
        self._prev_stat = None
        self._prev_rapl = None
        self.pdh = None
        self._setup()
        threading.Thread(target=self._loop, daemon=True).start()

    # -------------------------------------------------------- 준비

    def _setup(self):
        if IS_WINDOWS:
            try:
                self.pdh = Pdh({
                    "cpu": r"\Processor Information(_Total)\% Processor Utility",
                    "cpu_time": r"\Processor(_Total)\% Processor Time",
                    "gpu": r"\GPU Engine(*)\Utilization Percentage",
                })
                self.sources["cpu_util"] = "Windows 성능 카운터"
            except OSError:
                self.pdh = None
        elif os.path.exists("/proc/stat"):
            self.sources["cpu_util"] = "/proc/stat"

        self.rapl = []
        for path in glob.glob("/sys/class/powercap/intel-rapl:*"):
            if re.search(r"intel-rapl:\d+$", path):
                try:
                    read_int(f"{path}/energy_uj")
                    self.rapl.append(path)
                except (OSError, ValueError):
                    self.sources.setdefault("cpu_w_note", "RAPL 센서는 관리자(root) 권한 필요")
        if self.rapl:
            self.sources["cpu_w"] = "RAPL 실측"

        smi = shutil.which("nvidia-smi")
        if not smi and IS_WINDOWS:
            for cand in (r"C:\Windows\System32\nvidia-smi.exe",
                         r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
                if os.path.exists(cand):
                    smi = cand
        if smi:
            self.nvidia = {}

            def parse(reader, line):
                idx, watt, util = [x.strip() for x in line.split(",")]
                self.nvidia[idx] = (float(watt) if re.match(r"[\d.]+$", watt) else None,
                                    float(util) if re.match(r"[\d.]+$", util) else None)

            try:
                self.readers.append(LineReader(
                    [smi, "--query-gpu=index,power.draw,utilization.gpu",
                     "--format=csv,noheader,nounits", "-lms", "1000"], parse))
                self.sources["gpu_w"] = "NVIDIA 실측 (nvidia-smi)"
                self.sources["gpu_util"] = "nvidia-smi"
            except OSError:
                self.nvidia = None
        else:
            self.nvidia = None

        self.amd = [os.path.dirname(os.path.dirname(p)) for p in
                    glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*/power1_average")]
        if self.amd and not self.nvidia:
            self.sources["gpu_w"] = "AMD 실측 (sysfs)"
            self.sources["gpu_util"] = "AMD sysfs"
        if "gpu_util" not in self.sources and self.pdh and "gpu" in self.pdh.counters:
            self.sources["gpu_util"] = "Windows GPU 엔진 카운터"

        self.batteries = glob.glob("/sys/class/power_supply/BAT*")
        self.win_battery = None
        if IS_WINDOWS and self._windows_has_battery():
            def parse_batt(reader, line):
                rate, online = line.split("|")
                reader.value = (float(rate), online.strip() == "1")

            script = ("while ($true) { $b = Get-CimInstance -Namespace root\\wmi "
                      "-ClassName BatteryStatus -ErrorAction SilentlyContinue | "
                      "Select-Object -First 1; if ($b) { '{0}|{1}' -f $b.DischargeRate, "
                      "[int]$b.PowerOnline }; Start-Sleep -Seconds 2 }")
            try:
                self.win_battery = LineReader(["powershell", "-NoProfile", "-NonInteractive",
                                               "-Command", script], parse_batt)
                self.readers.append(self.win_battery)
            except OSError:
                pass
        if self.batteries or self.win_battery:
            self.sources["system_w"] = "배터리 방전 실측 (전원선을 뽑았을 때만)"

    @staticmethod
    def _windows_has_battery():
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

        status = Status()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return False
        return (status.BatteryFlag & 0xFF) not in (128, 255)

    # -------------------------------------------------------- 읽기

    def _cpu_util(self, counters):
        if counters:
            for key in ("cpu", "cpu_time"):
                if counters.get(key):
                    return min(100.0, counters[key][0][1])
            return None
        try:
            with open("/proc/stat") as f:
                fields = [int(x) for x in f.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle, total = fields[3] + fields[4], sum(fields[:8])
        prev, self._prev_stat = self._prev_stat, (idle, total)
        if not prev or total == prev[1]:
            return None
        return 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))

    def _gpu(self, counters):
        """(GPU 전력 W 또는 None, GPU 사용률 % 또는 None)"""
        if self.nvidia:
            vals = list(self.nvidia.values())
            watts = [w for w, _ in vals if w is not None]
            utils = [u for _, u in vals if u is not None]
            return (sum(watts) if watts else None, max(utils) if utils else None)
        if self.amd:
            watts, utils = [], []
            for hw in self.amd:
                try:
                    watts.append(read_int(glob.glob(f"{hw}/hwmon/hwmon*/power1_average")[0]) / 1e6)
                    utils.append(read_int(f"{hw}/gpu_busy_percent"))
                except (OSError, ValueError, IndexError):
                    pass
            return (sum(watts) if watts else None, max(utils) if utils else None)
        if counters and counters.get("gpu") is not None:
            per_type = {}
            for name, value in counters["gpu"]:
                m = re.search(r"engtype_(\w+)", name or "")
                if m:
                    per_type[m.group(1)] = per_type.get(m.group(1), 0) + value
            return None, min(100.0, max(per_type.values())) if per_type else 0.0
        return None, None

    def _rapl_watts(self, now):
        if not self.rapl:
            return None
        try:
            energy = [read_int(f"{p}/energy_uj") for p in self.rapl]
            ranges = [read_int(f"{p}/max_energy_range_uj") for p in self.rapl]
        except (OSError, ValueError):
            return None
        prev, self._prev_rapl = self._prev_rapl, (now, energy)
        if not prev:
            return None
        total = 0
        for e, p, r in zip(energy, prev[1], ranges):
            total += e - p if e >= p else e + r - p       # 카운터 한 바퀴 돌았을 때
        return total / 1e6 / (now - prev[0])

    def _system_watts(self):
        if self.win_battery and self.win_battery.value:
            rate, online = self.win_battery.value
            return rate / 1000 if not online and rate > 0 else None
        for bat in self.batteries:
            try:
                with open(f"{bat}/status") as f:
                    if f.read().strip() != "Discharging":
                        continue
                if os.path.exists(f"{bat}/power_now"):
                    return read_int(f"{bat}/power_now") / 1e6
                return read_int(f"{bat}/current_now") * read_int(f"{bat}/voltage_now") / 1e12
            except (OSError, ValueError):
                continue
        return None

    def _loop(self):
        while not self._stop.wait(1.0):
            now = time.time()
            counters = self.pdh.read() if self.pdh else None
            gpu_w, gpu_util = self._gpu(counters)
            sample = {"t": now, "cpu_util": self._cpu_util(counters), "gpu_util": gpu_util,
                      "cpu_w": self._rapl_watts(now), "gpu_w": gpu_w,
                      "system_w": self._system_watts()}
            with self.lock:
                self.samples.append(sample)
                self.samples = self.samples[-3600:]
                self.latest = sample

    def average(self, start, end):
        with self.lock:
            rows = [s for s in self.samples if start <= s["t"] <= end]
        result = {"samples": len(rows)}
        for key in ("cpu_util", "gpu_util", "cpu_w", "gpu_w", "system_w"):
            vals = [r[key] for r in rows if r[key] is not None]
            result[key] = sum(vals) / len(vals) if vals else None
        return result

    def close(self):
        self._stop.set()
        for reader in self.readers:
            reader.close()


# ---------------------------------------------------------------- 전력 계산

def estimate(config, avg):
    """측정 평균값으로 콘센트 기준 전력(W)을 계산한다."""
    cpu_u = (avg.get("cpu_util") or 0) / 100
    gpu_u = (avg.get("gpu_util") or 0) / 100
    heavy = config["heavy_factor"]
    cpu_w = gpu_w = other_w = 0.0
    measured = []
    for part in config["parts"]:
        kind = part.get("kind")
        if kind == "cpu":
            cpu_w += pc.part_power(part, cpu_u)
        elif kind == "gpu":
            gpu_w += pc.part_power(part, gpu_u)
        elif kind == "igpu":
            if avg.get("cpu_w") is None:          # RAPL 값에는 내장 그래픽이 포함됨
                cpu_w += pc.part_power(part, gpu_u)
        elif not part.get("external"):
            other_w += pc.part_power(part, max(cpu_u, gpu_u) * heavy)
    if avg.get("cpu_w") is not None:
        cpu_w = avg["cpu_w"]
        measured.append("CPU")
    if avg.get("gpu_w") is not None:
        gpu_w = avg["gpu_w"]
        measured.append("GPU")
    body_w = (cpu_w + gpu_w + other_w) / config["psu_efficiency"]
    if avg.get("system_w") is not None:
        body_w = avg["system_w"]                  # 배터리 방전량 = 본체 전체 실측
        measured = ["본체 전체(배터리)"]
    external_w = sum(part["load_w"] * part.get("count", 1)
                     for part in config["parts"] if part.get("external"))
    monitor_w = sum(monitor.monitor_power(m) * m.get("count", 1)
                    for m in config.get("monitors", [])) + external_w
    return {"cpu_w": cpu_w, "gpu_w": gpu_w, "body_w": body_w, "monitor_w": monitor_w,
            "total_w": body_w + monitor_w,
            "method": ("실측: " + ", ".join(measured)) if measured else "추정 (사용률 기반)"}


def measure(sampler, config, name, seconds, stop=None, load_key=None, on_tick=None):
    """seconds 동안 측정한 결과 dict. stop(Event)이 설정되면 중단."""
    with Load(load_key) if load_key else _Nothing():
        start = time.time()
        while time.time() - start < seconds:
            if stop is not None and stop.is_set():
                break
            if on_tick:
                on_tick(name, time.time() - start, seconds)
            time.sleep(0.25)
        end = time.time()
    avg = sampler.average(start + min(WARMUP_S, seconds / 3), end)
    result = {"name": name, **avg, **estimate(config, avg), "seconds": round(end - start)}
    return result


class _Nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


# ---------------------------------------------------------------- 명령줄

def fmt(value, unit="", digits=0):
    return "-" if value is None else f"{value:.{digits}f}{unit}"


def print_result(r):
    print(f"  {pc.pad(r['name'], 26)} CPU {fmt(r['cpu_util'], '%'):>5}  GPU {fmt(r['gpu_util'], '%'):>5}"
          f"  본체 {r['body_w']:6.1f}W  모니터 {r['monitor_w']:5.1f}W  합계 {r['total_w']:6.1f}W"
          f"  [{r['method']}]")


def main():
    parser = argparse.ArgumentParser(description="PC 전력 테스트")
    parser.add_argument("--seconds", type=float, default=20, help="테스트당 측정 시간(초)")
    parser.add_argument("--tests", nargs="+", choices=[t[0] for t in TESTS],
                        help="실행할 내장 테스트 (기본: 전체)")
    parser.add_argument("--custom", metavar="이름",
                        help="부하를 걸지 않고, 직접 실행한 프로그램을 측정할 때 이름")
    parser.add_argument("-c", "--config", help="부품 설정 JSON (기본: 자동 감지)")
    args = parser.parse_args()

    if args.config:
        config = pc.load_config(args.config)
    else:
        config, info, _ = pc.auto_config()
        pc.print_detection(info, None)
    sampler = Sampler()
    print("\n측정 방식:")
    for key, label in (("cpu_util", "CPU 사용률"), ("cpu_w", "CPU 전력"),
                       ("gpu_util", "GPU 사용률"), ("gpu_w", "GPU 전력"), ("system_w", "시스템 전력")):
        print(f"  {label}: {sampler.sources.get(key, '추정')}")
    if "cpu_w_note" in sampler.sources:
        print(f"  ※ {sampler.sources['cpu_w_note']}")
    print()
    try:
        if args.custom:
            print(f"'{args.custom}' 측정 중... 지금 측정할 프로그램을 사용하세요 ({args.seconds:g}초)")
            print_result(measure(sampler, config, args.custom, args.seconds))
        else:
            for key in args.tests or [t[0] for t in TESTS]:
                print(f"  ... {TEST_NAMES[key]} 측정 중 ({args.seconds:g}초)", end="\r", flush=True)
                result = measure(sampler, config, TEST_NAMES[key], args.seconds,
                                 load_key=key if key != "idle" else None)
                print(" " * 60, end="\r")
                print_result(result)
    finally:
        sampler.close()


if __name__ == "__main__":
    mp.freeze_support()
    main()
