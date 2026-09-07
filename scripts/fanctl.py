#!/usr/bin/env python3
"""CPU and GPU fan telemetry and curve helper for Omarchy.

Reads temperatures and fan RPM from hwmon (and nvidia-smi when present).
Resolves mbpfan.conf from the real system path, a user copy, and the
plugin-bundled fallback so a missing /etc/mbpfan.conf never blanks the widget.

Unprivileged by default. Privileged writes go through fanctl-privileged.py
via pkexec.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PLUGIN_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SYSTEM_CONFIG = Path("/etc/mbpfan.conf")
DEFAULT_USER_CONFIG = Path.home() / ".config" / "mbpfan" / "mbpfan.conf"
BUNDLED_CONFIG = PLUGIN_DIR / "etc" / "mbpfan.conf"

CPU_HWMON_NAMES = {
    "coretemp",
    "k10temp",
    "zenpower",
    "cpu_thermal",
    "cpu-thermal",
    "soc_thermal",
    "applesmc",
    "k8temp",
}
GPU_HWMON_NAMES = {
    "amdgpu",
    "nouveau",
    "i915",
    "xe",
    "nvidia",
}
APPLE_FAN_HINTS = ("applesmc",)
ALERT_CELSIUS = 80.0
THERMAL_CEILING = {"cpu": 100.0, "gpu": 90.0}

PRESETS = {
    "quiet": {"low_temp": 70, "high_temp": 78, "max_temp": 90},
    "balanced": {"low_temp": 63, "high_temp": 66, "max_temp": 86},
    "cool": {"low_temp": 50, "high_temp": 60, "max_temp": 75},
}

# Direct duty targets for motherboard PWM. Spread wide so Quiet→Cool is obvious
# on nct6687 boards (narrow steps are easy to miss under firmware noise).
PRESET_DUTY = {
    "quiet": 18,
    "balanced": 45,
    "cool": 100,
}

# Never drive these PWM indexes. Fan 2 on the MSI PRO Z690-A is the AIO pump
# (~4000 RPM); changing its duty does nothing useful and can upset the loop.
SKIP_PWM_INDEXES = {"2"}

DEFAULT_CURVE = dict(PRESETS["balanced"], polling_interval=1)

SYSTEMD_UNIT_NAME = "fan-control-follow.service"
SYSTEMD_USER_DIR = Path.home() / ".config/systemd/user"
FOLLOW_DEFAULT_INTERVAL = 3.0
FOLLOW_DEFAULT_MIN_DELTA = 5


def follow_state_dir() -> Path:
    override = os.environ.get("FANCTL_FOLLOW_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local/state/omarchy/fan-control"


def follow_pid_file() -> Path:
    return follow_state_dir() / "follow.pid"


def follow_state_file() -> Path:
    return follow_state_dir() / "follow-state.json"


def follow_lock_file() -> Path:
    return follow_state_dir() / "follow.lock"

_CPU_STAT_PREV: tuple[int, int] | None = None
_CPU_PRIMED = False


def thermal_percent(temp: float | None, ceiling: float) -> float | None:
    if temp is None or ceiling <= 0:
        return None
    return round(min(100.0, max(0.0, float(temp) / ceiling * 100.0)), 1)


def cpu_usage_percent() -> float | None:
    """Aggregate CPU busy % from /proc/stat deltas between snapshot polls."""
    global _CPU_STAT_PREV
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    except OSError:
        return None
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        user = int(parts[1])
        nice = int(parts[2])
        system = int(parts[3])
        idle = int(parts[4])
        iowait = int(parts[5]) if len(parts) > 5 else 0
        irq = int(parts[6]) if len(parts) > 6 else 0
        softirq = int(parts[7]) if len(parts) > 7 else 0
        steal = int(parts[8]) if len(parts) > 8 else 0
    except ValueError:
        return None
    idle_all = idle + iowait
    busy = user + nice + system + irq + softirq + steal
    total = idle_all + busy
    if _CPU_STAT_PREV is None:
        _CPU_STAT_PREV = (total, idle_all)
        return None
    prev_total, prev_idle = _CPU_STAT_PREV
    _CPU_STAT_PREV = (total, idle_all)
    dt = total - prev_total
    di = idle_all - prev_idle
    if dt <= 0:
        return None
    return round(max(0.0, min(100.0, (dt - di) * 100.0 / dt)), 1)


def gpu_usage_percent() -> float | None:
    smi = which("nvidia-smi")
    if smi:
        try:
            proc = subprocess.run(
                [smi, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc and proc.returncode == 0 and proc.stdout.strip():
            value = _smi_number(proc.stdout.strip().splitlines()[0])
            if value is not None:
                return round(max(0.0, min(100.0, value)), 1)
    root = drm_root()
    if not root.exists():
        return None
    for card in sorted(root.glob("card[0-9]")):
        device = card / "device"
        for name in ("gpu_busy_percent", "busy_percent"):
            value = read_int(device / name)
            if value is not None:
                return round(max(0.0, min(100.0, float(value))), 1)
    return None


def enrich_device_metrics(device: dict[str, Any], kind: str) -> dict[str, Any]:
    ceiling = THERMAL_CEILING.get(kind, 100.0)
    temp = device.get("temp")
    device["thermal_percent"] = thermal_percent(temp, ceiling)
    if kind == "cpu" and device.get("usage") is None:
        global _CPU_PRIMED
        if not _CPU_PRIMED:
            cpu_usage_percent()
            time.sleep(0.05)
            _CPU_PRIMED = True
        device["usage"] = cpu_usage_percent()
    if kind == "gpu" and device.get("usage") is None:
        device["usage"] = gpu_usage_percent()
    return device


def hwmon_root() -> Path:
    override = os.environ.get("FANCTL_HWMON_ROOT")
    return Path(override) if override else Path("/sys/class/hwmon")


def drm_root() -> Path:
    override = os.environ.get("FANCTL_DRM_ROOT")
    return Path(override) if override else Path("/sys/class/drm")


def config_search_paths() -> list[Path]:
    extra = os.environ.get("FANCTL_CONFIG_PATHS", "")
    paths: list[Path] = []
    if extra:
        paths.extend(Path(p) for p in extra.split(":") if p)
    paths.extend(
        [
            DEFAULT_SYSTEM_CONFIG,
            DEFAULT_USER_CONFIG,
            BUNDLED_CONFIG,
        ]
    )
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path if path.is_absolute() else (PLUGIN_DIR / path)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def read_int(path: Path) -> int | None:
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(float(raw.split()[0]))
    except (TypeError, ValueError, IndexError):
        return None


def which(name: str) -> str | None:
    return shutil.which(name)


def unit_running(name: str) -> bool:
    systemctl = which("systemctl")
    if not systemctl:
        return False
    try:
        proc = subprocess.run(
            [systemctl, "is-active", "--quiet", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def parse_mbpfan_conf(text: str) -> dict[str, Any]:
    curve = dict(DEFAULT_CURVE)
    if not text:
        return curve
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in {"low_temp", "high_temp", "max_temp", "polling_interval"}:
            try:
                curve[key] = int(float(value))
            except ValueError:
                continue
        elif key.startswith("min_fan") and key.endswith("_speed"):
            try:
                curve[key] = int(float(value))
            except ValueError:
                continue
        elif key.startswith("max_fan") and key.endswith("_speed"):
            try:
                curve[key] = int(float(value))
            except ValueError:
                continue
    if curve["low_temp"] > curve["high_temp"]:
        curve["low_temp"], curve["high_temp"] = curve["high_temp"], curve["low_temp"]
    if curve["high_temp"] > curve["max_temp"]:
        curve["max_temp"] = curve["high_temp"]
    return curve


def load_config() -> dict[str, Any]:
    missing: list[str] = []
    errors: list[str] = []
    for path in config_search_paths():
        if not path.exists():
            missing.append(str(path))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"could not read {path}: {exc}")
            continue
        curve = parse_mbpfan_conf(text)
        source = "bundled" if path == BUNDLED_CONFIG else str(path)
        return {
            **curve,
            "path": str(path),
            "source": source,
            "missing": False,
            "readable": True,
            "system_missing": not DEFAULT_SYSTEM_CONFIG.exists(),
            "tried": [str(p) for p in config_search_paths()],
            "errors": errors,
        }
    return {
        **DEFAULT_CURVE,
        "path": None,
        "source": "defaults",
        "missing": True,
        "readable": False,
        "system_missing": not DEFAULT_SYSTEM_CONFIG.exists(),
        "tried": [str(p) for p in config_search_paths()],
        "errors": errors
        or ["could not read /etc/mbpfan.conf"],
    }


def load_display_config() -> dict[str, Any]:
    """Curve for the bar panel — prefers the last preset written by apply."""
    if DEFAULT_USER_CONFIG.exists():
        try:
            text = DEFAULT_USER_CONFIG.read_text(encoding="utf-8")
        except OSError:
            pass
        else:
            curve = parse_mbpfan_conf(text)
            return {
                **curve,
                "path": str(DEFAULT_USER_CONFIG),
                "source": str(DEFAULT_USER_CONFIG),
                "missing": False,
                "readable": True,
                "system_missing": not DEFAULT_SYSTEM_CONFIG.exists(),
                "tried": [str(p) for p in config_search_paths()],
                "errors": [],
            }
    return load_config()


def list_hwmon() -> list[dict[str, Any]]:
    root = hwmon_root()
    devices: list[dict[str, Any]] = []
    if not root.exists():
        return devices
    for entry in sorted(root.glob("hwmon*")):
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        name = read_text(entry / "name") or entry.name
        devices.append({"name": name.strip(), "path": str(entry)})
    return devices


def temps_for(device: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for input_path in sorted(device.glob("temp*_input")):
        celsius_milli = read_int(input_path)
        if celsius_milli is None:
            continue
        stem = input_path.name[: -len("_input")]
        label = read_text(device / f"{stem}_label") or stem
        out.append(
            {
                "label": label,
                "celsius": round(celsius_milli / 1000.0, 1),
            }
        )
    return out


def _fan_index(name: str) -> str:
    digits = "".join(ch for ch in name if ch.isdigit())
    return digits or "1"


def _fan_record(device: Path, index: str, kind: str, chip: str, label: str | None = None) -> dict[str, Any]:
    pwm_path = device / f"pwm{index}"
    pwm_max_path = device / f"pwm{index}_max"
    pwm_enable_path = device / f"pwm{index}_enable"
    output_path = device / f"fan{index}_output"
    manual_path = device / f"fan{index}_manual"
    input_path = device / f"fan{index}_input"
    pwm = read_int(pwm_path) if pwm_path.exists() else None
    pwm_max = read_int(pwm_max_path) or 255
    rpm = read_int(input_path) if input_path.exists() else None
    pretty = label or read_text(device / f"fan{index}_label") or f"Fan {index}"
    if chip and chip not in {"coretemp", "k10temp"} and pretty.lower() in {f"fan {index}", f"fan{index}"}:
        pretty = f"{chip} {pretty}"
    return {
        "id": f"{device.name}-fan{index}",
        "label": pretty,
        "kind": kind,
        "chip": chip,
        "rpm": rpm,
        "pwm": pwm,
        "pwm_max": pwm_max,
        "pwm_percent": round(pwm * 100 / pwm_max, 1) if pwm is not None and pwm_max else None,
        "pwm_path": str(pwm_path) if pwm_path.exists() else None,
        "pwm_enable_path": str(pwm_enable_path) if pwm_enable_path.exists() else None,
        "output_path": str(output_path) if output_path.exists() else None,
        "manual_path": str(manual_path) if manual_path.exists() else None,
        # Root/pkexec can still write 0444 sysfs PWM nodes (CAP_DAC_OVERRIDE).
        "controllable": bool(
            (pwm_path.exists() and (os.access(pwm_path, os.W_OK) or os.geteuid() == 0 or which("pkexec")))
            or (output_path.exists() and (os.access(output_path, os.W_OK) or os.geteuid() == 0 or which("pkexec")))
        ),
    }


def fans_for(device: Path, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    chip = read_text(device / "name") or device.name
    for input_path in sorted(device.glob("fan*_input")):
        index = _fan_index(input_path.name)
        label = read_text(device / f"fan{index}_label")
        rec = _fan_record(device, index, kind, chip, label)
        out.append(rec)
        seen.add(index)

    for output_path in sorted(device.glob("fan*_output")):
        index = _fan_index(output_path.name)
        if index in seen:
            continue
        rec = _fan_record(device, index, kind, chip)
        rec["target_rpm"] = read_int(output_path)
        if rec["rpm"] is None:
            rec["rpm"] = rec["target_rpm"]
        out.append(rec)
        seen.add(index)

    for pwm_path in sorted(device.glob("pwm[0-9]")):
        index = _fan_index(pwm_path.name)
        if index in seen:
            continue
        rec = _fan_record(device, index, kind, chip)
        if rec["pwm"] is None and rec["rpm"] is None:
            continue
        out.append(rec)
        seen.add(index)
    return out


def collect_case_fans(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every non-GPU fan node. coretemp has none; nct6687 / it87 chips do."""
    fans: list[dict[str, Any]] = []
    skip = GPU_HWMON_NAMES | {"nvme", "acpitz", "iwlwifi"}
    for device in devices:
        chip = (device["name"] or "").split()[0].lower()
        if chip in skip or chip.startswith("spd5118") or chip.startswith("iwlwifi"):
            continue
        found = fans_for(Path(device["path"]), "cpu")
        fans.extend(found)
    fans.sort(key=lambda f: (-(f.get("rpm") or 0), f.get("label") or ""))
    return fans


def hottest(temps: list[dict[str, Any]]) -> float | None:
    if not temps:
        return None
    return max(t["celsius"] for t in temps)


def pick_device(devices: list[dict[str, Any]], names: set[str]) -> dict[str, Any] | None:
    for device in devices:
        if device["name"] in names:
            return device
    return None


def gpu_from_drm() -> dict[str, Any] | None:
    root = drm_root()
    if not root.exists():
        return None
    for card in sorted(root.glob("card[0-9]")):
        device = card / "device"
        hwmon_dirs = sorted((device / "hwmon").glob("hwmon*")) if (device / "hwmon").exists() else []
        if not hwmon_dirs:
            continue
        hw = hwmon_dirs[0]
        name = read_text(hw / "name") or read_text(device / "vendor") or card.name
        temps = temps_for(hw)
        fans = fans_for(hw, "gpu")
        if not temps and not fans:
            continue
        return {
            "name": name,
            "path": str(hw),
            "temp": hottest(temps),
            "temps": temps,
            "fans": fans,
        }
    return None


def _smi_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.upper() in {"N/A", "[N/A]", "NA"}:
        return None
    try:
        return float(text.split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def nvidia_fan_rpm() -> int | None:
    """RPM is not in the usual nvidia-smi CSV. nvidia-settings exposes it when Coolbits is on."""
    settings = which("nvidia-settings")
    if not settings:
        return None
    for query in (
        "[fan:0]/GPUCurrentFanSpeedRPM",
        "GPUCurrentFanSpeedRPM",
    ):
        try:
            proc = subprocess.run(
                [settings, "-t", "-q", query],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        value = _smi_number(proc.stdout)
        if value is not None and value >= 0:
            return int(value)
    return None


def nvidia_gpu() -> dict[str, Any] | None:
    smi = which("nvidia-smi")
    if not smi:
        return None
    try:
        proc = subprocess.run(
            [
                smi,
                "--query-gpu=name,temperature.gpu,utilization.gpu,fan.speed",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    line = proc.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    name = parts[0] if parts else "NVIDIA"
    temp = _smi_number(parts[1]) if len(parts) > 1 else None
    util = _smi_number(parts[2]) if len(parts) > 2 else None
    fan = _smi_number(parts[3]) if len(parts) > 3 else None
    rpm = nvidia_fan_rpm()
    fans = []
    if fan is not None or rpm is not None:
        fans.append(
            {
                "id": "nvidia-fan",
                "label": "GPU fan",
                "kind": "gpu",
                "rpm": rpm,
                "pwm": None,
                "pwm_max": None,
                "pwm_percent": int(fan) if fan is not None else None,
                "pwm_path": None,
                "pwm_enable_path": None,
                "output_path": None,
                "manual_path": None,
                "controllable": False,
            }
        )
    temps = [{"label": "GPU", "celsius": temp}] if temp is not None else []
    return {
        "name": name,
        "path": None,
        "temp": temp,
        "usage": util,
        "temps": temps,
        "fans": fans,
    }


def detect_backend(devices: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    names = {d["name"] for d in devices}
    apple = bool(names & set(APPLE_FAN_HINTS)) or any(
        "applesmc" in d["path"] for d in devices
    )
    mbpfan_bin = which("mbpfan")
    t2fanrd_bin = which("t2fanrd")
    notes: list[str] = []
    name = "hwmon"
    running = False
    can_control = False
    passwordless = False

    if apple and mbpfan_bin:
        name = "mbpfan"
        running = unit_running("mbpfan.service") or unit_running("mbpfan")
        can_control = True
        if config.get("system_missing"):
            notes.append(
                "mbpfan is installed but /etc/mbpfan.conf is missing. "
                "The widget is using the bundled curve until you install the config."
            )
    elif t2fanrd_bin or unit_running("t2fanrd.service"):
        name = "t2fanrd"
        running = unit_running("t2fanrd.service")
        can_control = True
        notes.append("T2 Mac fan daemon detected. Firmware plus t2fanrd own the curve.")
    elif apple and not mbpfan_bin:
        name = "applesmc"
        notes.append(
            "Apple SMC fans are present. Install mbpfan-git from the AUR, then "
            "write /etc/mbpfan.conf from this panel."
        )
    else:
        pwm_fans = [f for f in collect_case_fans(devices) if f.get("pwm_path")]
        if pwm_fans:
            name = "hwmon"
            can_control = True
            passwordless = trusted_installed_helper() is not None
            if passwordless:
                notes.append(
                    "Fan duty is applied by the root-owned helper. "
                    "Quiet / Balanced / Cool and Follow curve do not prompt again."
                )
            else:
                notes.append(
                    "Motherboard PWM needs a one-time unlock. Click “Allow passwordless control”, "
                    "enter your password once. PWM nodes stay root-only; only the installed helper can write them."
                )
        else:
            name = "monitor"
            nvidia = any(d["name"] == "nvidia" for d in devices) or bool(which("nvidia-smi"))
            if nvidia:
                notes.append(
                    "NVIDIA reports fan percent through nvidia-smi. Many 30-series cards stay at 0% until about 50-60°C."
                )
                if not collect_case_fans(devices):
                    notes.append(
                        "coretemp has no fan RPM. Case/CPU fans need a Super I/O chip (nct6687, nct6775, it87). "
                        "If sensors shows none: sudo pacman -S lm_sensors && sudo sensors-detect"
                    )
            else:
                notes.append(
                    "No motherboard PWM nodes found. Apply a curve only after lm_sensors sees pwm1…pwmN."
                )

    if config.get("system_missing") and name == "mbpfan":
        can_control = True

    return {
        "name": name,
        "running": running,
        "mbpfan_present": bool(mbpfan_bin),
        "t2fanrd_present": bool(t2fanrd_bin),
        "apple": apple,
        "can_control": can_control,
        "passwordless": passwordless,
        "config_path": config.get("path"),
        "config_missing": bool(config.get("missing") or config.get("system_missing")),
        "config_error": (config.get("errors") or [None])[0],
        "notes": notes,
    }


def group_cpu(devices: list[dict[str, Any]]) -> dict[str, Any]:
    picked = pick_device(devices, CPU_HWMON_NAMES)
    apple = pick_device(devices, set(APPLE_FAN_HINTS))
    cpu_path = Path(picked["path"]) if picked else None
    temps = temps_for(cpu_path) if cpu_path else []
    fans = collect_case_fans(devices)
    name = picked["name"] if picked else (apple["name"] if apple else "CPU")
    fan_chips = []
    for fan in fans:
        chip = fan.get("chip")
        if chip and chip not in fan_chips and chip not in CPU_HWMON_NAMES:
            fan_chips.append(chip)
    if fan_chips:
        name = name + " · " + ", ".join(fan_chips)
    spinning = [f for f in fans if (f.get("rpm") or 0) > 0]
    idle_fans = len(fans) - len(spinning)
    empty = None
    if not fans:
        empty = (
            "coretemp has no fan nodes. No motherboard Super I/O fans were found. "
            "Run: sudo pacman -S lm_sensors && sudo sensors-detect"
        )
    return {
        "name": name,
        "path": str(cpu_path) if cpu_path else None,
        "temp": hottest(temps),
        "temps": temps,
        "fans": spinning,
        "idle_fans": idle_fans,
        "empty": empty,
    }


def merge_gpu_fans(hw_fans: list[dict[str, Any]], smi_fans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer hwmon RPM when present; keep nvidia-smi percent."""
    if not hw_fans:
        return smi_fans
    if not smi_fans:
        return hw_fans
    smi = smi_fans[0]
    merged = []
    for i, fan in enumerate(hw_fans):
        row = dict(fan)
        if row.get("pwm_percent") is None and smi.get("pwm_percent") is not None:
            row["pwm_percent"] = smi["pwm_percent"]
        if row.get("rpm") is None and smi.get("rpm") is not None:
            row["rpm"] = smi["rpm"]
        if i == 0 and (not row.get("label") or row["label"].lower().startswith("fan")):
            row["label"] = smi.get("label") or row["label"]
        merged.append(row)
    if not merged:
        return smi_fans
    return merged


def group_gpu(devices: list[dict[str, Any]]) -> dict[str, Any]:
    nvidia = nvidia_gpu()
    drm = gpu_from_drm()
    picked = pick_device(devices, GPU_HWMON_NAMES)
    hw = None
    if picked:
        path = Path(picked["path"])
        hw = {
            "name": picked["name"],
            "path": str(path),
            "temp": hottest(temps_for(path)),
            "temps": temps_for(path),
            "fans": fans_for(path, "gpu"),
        }
    gpu = drm or hw
    if nvidia and gpu is None:
        gpu = nvidia
    elif nvidia and gpu:
        if gpu.get("name") in GPU_HWMON_NAMES or not gpu.get("name"):
            gpu["name"] = nvidia["name"]
        if nvidia.get("temp") is not None:
            if gpu.get("temp") is None:
                gpu["temp"] = nvidia["temp"]
            if not gpu.get("temps"):
                gpu["temps"] = nvidia["temps"]
        if nvidia.get("usage") is not None and gpu.get("usage") is None:
            gpu["usage"] = nvidia["usage"]
        if nvidia.get("fans"):
            if not gpu.get("fans"):
                gpu["fans"] = nvidia["fans"]
            else:
                gpu["fans"] = merge_gpu_fans(gpu["fans"], nvidia["fans"])
    if gpu is None:
        return {"name": None, "path": None, "temp": None, "temps": [], "fans": [], "empty": None}
    empty = None
    if not gpu.get("fans"):
        empty = "No NVIDIA fan percent or RPM yet. nvidia-smi fan.speed is often N/A with the card idle."
    gpu["empty"] = empty
    return gpu


def demo_snapshot(config: dict[str, Any], tick: float | None = None) -> dict[str, Any]:
    t = time.time() if tick is None else tick
    cpu_temp = 48.0 + 10.0 * (0.5 + 0.5 * __import__("math").sin(t / 8.0))
    gpu_temp = 54.0 + 12.0 * (0.5 + 0.5 * __import__("math").sin(t / 11.0 + 1.2))
    cpu_usage = 28.0 + 35.0 * (0.5 + 0.5 * __import__("math").sin(t / 5.0 + 0.4))
    gpu_usage = 22.0 + 48.0 * (0.5 + 0.5 * __import__("math").sin(t / 6.5 + 1.1))
    cpu_rpm = int(1800 + (cpu_temp - 40) * 40)
    gpu_rpm = int(1600 + (gpu_temp - 40) * 45)
    curve = {k: config[k] for k in ("low_temp", "high_temp", "max_temp", "polling_interval")}
    return {
        "ok": True,
        "demo": True,
        "error": None,
        "hottest": round(max(cpu_temp, gpu_temp), 1),
        "alert": max(cpu_temp, gpu_temp) >= ALERT_CELSIUS,
        "backend": {
            "name": "demo",
            "running": False,
            "mbpfan_present": False,
            "t2fanrd_present": False,
            "apple": False,
            "can_control": True,
            "passwordless": True,
            "config_path": config.get("path"),
            "config_missing": bool(config.get("system_missing")),
            "config_error": None
            if not config.get("system_missing")
            else "could not read /etc/mbpfan.conf",
            "notes": [
                "Demo sensors. On Omarchy this panel reads live hwmon, Apple SMC, and nvidia-smi."
            ],
        },
        "config": {
            **curve,
            "path": config.get("path"),
            "source": config.get("source"),
            "missing": bool(config.get("missing")),
            "system_missing": bool(config.get("system_missing")),
            "errors": config.get("errors") or [],
        },
        "cpu": {
            "name": "coretemp",
            "path": None,
            "temp": round(cpu_temp, 1),
            "usage": round(cpu_usage, 1),
            "thermal_percent": thermal_percent(cpu_temp, THERMAL_CEILING["cpu"]),
            "temps": [{"label": "Package", "celsius": round(cpu_temp, 1)}],
            "fans": [
                {
                    "id": "demo-cpu",
                    "label": "CPU fan",
                    "kind": "cpu",
                    "rpm": cpu_rpm,
                    "pwm": 90,
                    "pwm_max": 255,
                    "pwm_percent": 35.0,
                    "controllable": True,
                }
            ],
        },
        "gpu": {
            "name": "AMD Radeon",
            "path": None,
            "temp": round(gpu_temp, 1),
            "usage": round(gpu_usage, 1),
            "thermal_percent": thermal_percent(gpu_temp, THERMAL_CEILING["gpu"]),
            "temps": [{"label": "GPU", "celsius": round(gpu_temp, 1)}],
            "fans": [
                {
                    "id": "demo-gpu",
                    "label": "GPU fan",
                    "kind": "gpu",
                    "rpm": gpu_rpm,
                    "pwm": 110,
                    "pwm_max": 255,
                    "pwm_percent": 43.0,
                    "controllable": True,
                }
            ],
        },
        "presets": presets_payload(curve),
    }


def presets_payload(curve: dict[str, Any]) -> list[dict[str, Any]]:
    active = None
    for key, values in PRESETS.items():
        if all(int(curve.get(k, -1)) == values[k] for k in values):
            active = key
            break
    return [
        {"id": key, "label": key.capitalize(), **values, "active": key == active}
        for key, values in PRESETS.items()
    ]


def format_pill(snapshot: dict[str, Any]) -> str:
    cpu = (snapshot.get("cpu") or {}).get("temp")
    gpu = (snapshot.get("gpu") or {}).get("temp")
    if cpu is None and gpu is None:
        return "T …"
    if cpu is not None and gpu is not None:
        return f"{int(round(cpu))}° {int(round(gpu))}°"
    value = cpu if cpu is not None else gpu
    return f"{int(round(value))}°"


def empty_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    curve = {k: config[k] for k in ("low_temp", "high_temp", "max_temp", "polling_interval")}
    empty_device = {
        "name": None,
        "path": None,
        "temp": None,
        "temps": [],
        "fans": [],
        "idle_fans": 0,
        "empty": (
            "No temperature or fan sensors found. Install lm_sensors and run sensors-detect, "
            "or use snapshot --demo to preview the panel."
        ),
    }
    return {
        "ok": True,
        "demo": False,
        "error": None,
        "hottest": None,
        "alert": False,
        "backend": {
            "name": "monitor",
            "running": False,
            "mbpfan_present": bool(which("mbpfan")),
            "t2fanrd_present": bool(which("t2fanrd")),
            "apple": False,
            "can_control": False,
            "passwordless": False,
            "config_path": config.get("path"),
            "config_missing": bool(config.get("missing") or config.get("system_missing")),
            "config_error": (config.get("errors") or [None])[0],
            "notes": [empty_device["empty"]],
        },
        "config": {
            **curve,
            "path": config.get("path"),
            "source": config.get("source"),
            "missing": bool(config.get("missing")),
            "system_missing": bool(config.get("system_missing")),
            "errors": config.get("errors") or [],
        },
        "cpu": dict(empty_device),
        "gpu": dict(empty_device),
        "presets": presets_payload(curve),
    }


def snapshot(force_demo: bool = False) -> dict[str, Any]:
    config = load_config()
    display = load_display_config()
    if force_demo or os.environ.get("FANCTL_DEMO") == "1":
        data = demo_snapshot(display)
        data["label"] = format_pill(data)
        data["tooltip"] = tooltip_text(data)
        return attach_follow(data)

    devices = list_hwmon()
    cpu = enrich_device_metrics(group_cpu(devices), "cpu")
    gpu = enrich_device_metrics(group_gpu(devices), "gpu")
    if cpu["temp"] is None and gpu["temp"] is None and not cpu["fans"] and not gpu["fans"]:
        data = empty_snapshot(display)
        data["label"] = format_pill(data)
        data["tooltip"] = tooltip_text(data)
        return attach_follow(data)

    backend = detect_backend(devices, config)
    hottest_temp = max([t for t in (cpu.get("temp"), gpu.get("temp")) if t is not None], default=None)
    curve = {k: display[k] for k in ("low_temp", "high_temp", "max_temp", "polling_interval")}
    data = {
        "ok": True,
        "demo": False,
        "error": None,
        "hottest": hottest_temp,
        "alert": bool(hottest_temp is not None and hottest_temp >= ALERT_CELSIUS),
        "backend": backend,
        "config": {
            **curve,
            "path": display.get("path"),
            "source": display.get("source"),
            "missing": bool(display.get("missing")),
            "system_missing": bool(config.get("system_missing")),
            "errors": config.get("errors") or [],
        },
        "cpu": cpu,
        "gpu": gpu,
        "presets": presets_payload(curve),
    }
    data["label"] = format_pill(data)
    data["tooltip"] = tooltip_text(data)
    return attach_follow(data)


def attach_follow(data: dict[str, Any]) -> dict[str, Any]:
    data["follow"] = follow_status()
    return data


def tooltip_text(data: dict[str, Any]) -> str:
    cpu = data.get("cpu") or {}
    gpu = data.get("gpu") or {}
    backend = data.get("backend") or {}
    parts = []
    if cpu.get("temp") is not None:
        parts.append(f"CPU {int(round(cpu['temp']))}°")
    if gpu.get("temp") is not None:
        parts.append(f"GPU {int(round(gpu['temp']))}°")
    fans = (cpu.get("fans") or []) + (gpu.get("fans") or [])
    rpm = [f.get("rpm") for f in fans if f.get("rpm")]
    if rpm:
        parts.append(f"{max(rpm)} RPM")
    name = backend.get("name")
    if name:
        parts.append(str(name))
    return " · ".join(parts) if parts else "Fan control"


def render_mbpfan_conf(curve: dict[str, Any]) -> str:
    lines = [
        "[general]",
        "# Written by the Omarchy fan-control widget.",
        f"low_temp = {int(curve['low_temp'])}",
        f"high_temp = {int(curve['high_temp'])}",
        f"max_temp = {int(curve['max_temp'])}",
        f"polling_interval = {int(curve.get('polling_interval', 1))}",
        "",
    ]
    return "\n".join(lines)


def write_user_config(curve: dict[str, Any]) -> Path:
    DEFAULT_USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_USER_CONFIG.write_text(render_mbpfan_conf(curve), encoding="utf-8")
    return DEFAULT_USER_CONFIG


INSTALLED_HELPER = Path("/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py")
BOOTSTRAP_PRIVILEGED_COMMANDS = frozenset(
    {"grant-access", "revoke-access", "install-config", "apply-config"}
)


def trusted_installed_helper() -> Path | None:
    """Return the root-owned helper, or None if missing or not trustworthy."""
    override = os.environ.get("FANCTL_INSTALLED_HELPER")
    path = Path(override) if override else INSTALLED_HELPER
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    expected_uid = int(os.environ.get("FANCTL_HELPER_UID", "0"))
    if st.st_uid != expected_uid:
        return None
    if st.st_mode & 0o022:
        return None
    return path


def privileged_helper() -> Path:
    return PLUGIN_DIR / "scripts" / "fanctl-privileged.py"


def run_privileged(args: list[str], stdin_text: str | None = None) -> dict[str, Any]:
    """pkexec the root-owned helper. Checkout python3 is only for first grant/install."""
    trusted = trusted_installed_helper()
    pkexec = which("pkexec")
    cmd = args[0] if args else ""
    if trusted:
        argv = [str(trusted), *args]
    elif cmd in BOOTSTRAP_PRIVILEGED_COMMANDS:
        argv = [sys.executable, str(privileged_helper()), *args]
    else:
        return {
            "ok": False,
            "error": "privileged helper is not installed or is not root-owned",
            "hint": "python3 scripts/fanctl.py grant-access",
        }
    if pkexec:
        argv = [pkexec, *argv]
    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return {"ok": False, "error": err}
    if proc.stdout.strip():
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"ok": True, "output": proc.stdout.strip()}
    return {"ok": True}


def apply_curve(curve: dict[str, Any], install_system: bool = True) -> dict[str, Any]:
    if read_follow_pid() is not None:
        follow_stop()
    merged = dict(DEFAULT_CURVE)
    merged.update({k: int(curve[k]) for k in ("low_temp", "high_temp", "max_temp") if k in curve})
    if "polling_interval" in curve:
        merged["polling_interval"] = int(curve["polling_interval"])
    preset = curve.get("preset")
    user_path = write_user_config(merged)
    result: dict[str, Any] = {
        "ok": True,
        "user_path": str(user_path),
        "system_path": None,
        "pwm": None,
        "preset": preset,
        "curve": {k: merged[k] for k in ("low_temp", "high_temp", "max_temp", "polling_interval")},
    }

    devices = list_hwmon()
    backend = detect_backend(devices, load_config())
    apple_like = backend["name"] in {"mbpfan", "t2fanrd", "applesmc"}

    if install_system and apple_like:
        payload = json.dumps({"curve": merged, "source": str(BUNDLED_CONFIG)})
        privileged = run_privileged(["apply-config"], payload)
        result["privileged"] = privileged
        if privileged.get("ok"):
            result["system_path"] = str(DEFAULT_SYSTEM_CONFIG)
        else:
            result["ok"] = False
            result["error"] = privileged.get("error") or "could not write /etc/mbpfan.conf"

    pwm_result = apply_hwmon_pwms(merged, preset=preset if isinstance(preset, str) else None)
    result["pwm"] = pwm_result
    if pwm_result.get("attempted"):
        if pwm_result.get("ok"):
            result["ok"] = True
            result.pop("error", None)
        elif not apple_like:
            result["ok"] = False
            result["error"] = pwm_result.get("error") or "could not write motherboard PWM"
    elif not apple_like and backend["name"] == "monitor":
        result["warning"] = (
            "Curve saved, but no motherboard PWM nodes were found. "
            "Fans stay on the firmware curve."
        )
    return result


def active_curve() -> dict[str, Any]:
    config = load_display_config()
    curve = dict(DEFAULT_CURVE)
    for key in ("low_temp", "high_temp", "max_temp", "polling_interval"):
        if key in config:
            curve[key] = int(config[key])
    return curve


def hwmon_pwm_fans(*, require_spinning: bool = True) -> list[dict[str, Any]]:
    """Case/CPU PWM channels. Follow mode includes idle headers so fans can ramp up."""
    fans = collect_case_fans(list_hwmon())
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fan in fans:
        path = fan.get("pwm_path")
        if not path or path in seen:
            continue
        index = _fan_index(Path(path).name)
        if index in SKIP_PWM_INDEXES:
            continue
        rpm = fan.get("rpm")
        if require_spinning and (rpm is None or int(rpm) <= 0):
            continue
        seen.add(path)
        out.append(fan)
    return out


def release_skipped_pwms() -> list[dict[str, Any]]:
    """Hand SKIP_PWM_INDEXES channels back to EC auto mode (enable=2)."""
    released: list[dict[str, Any]] = []
    for fan in collect_case_fans(list_hwmon()):
        path = fan.get("pwm_path")
        if not path:
            continue
        index = _fan_index(Path(path).name)
        if index not in SKIP_PWM_INDEXES:
            continue
        enable = fan.get("pwm_enable_path")
        if not enable:
            continue
        try:
            Path(enable).write_text("2", encoding="utf-8")
            released.append({"path": enable, "value": 2, "pwm_path": path})
        except OSError:
            continue
    return released


def release_controlled_pwms() -> list[str]:
    """Return case fan PWM channels to EC auto mode on shutdown."""
    released: list[str] = []
    for fan in collect_case_fans(list_hwmon()):
        path = fan.get("pwm_path")
        if not path:
            continue
        index = _fan_index(Path(path).name)
        if index in SKIP_PWM_INDEXES:
            continue
        enable = fan.get("pwm_enable_path")
        if not enable:
            continue
        try:
            Path(enable).write_text("2", encoding="utf-8")
            released.append(enable)
        except OSError:
            continue
    return released


def apply_hwmon_percent(
    percent: int,
    *,
    require_spinning: bool = True,
    allow_pkexec: bool = True,
) -> dict[str, Any]:
    fans = hwmon_pwm_fans(require_spinning=require_spinning)
    released = release_skipped_pwms()
    if not fans:
        return {
            "ok": True,
            "attempted": False,
            "percent": percent,
            "wrote": [],
            "count": 0,
            "skipped_pump": released,
        }

    writes = []
    for fan in fans:
        pwm_max = int(fan.get("pwm_max") or 255)
        value = max(0, min(pwm_max, int(round(pwm_max * percent / 100.0))))
        item: dict[str, Any] = {"path": fan["pwm_path"], "value": value}
        if fan.get("pwm_enable_path"):
            item["enable_path"] = fan["pwm_enable_path"]
        writes.append(item)

    direct_ok, direct_err, kind = _write_pwms_unprivileged(writes)
    if direct_ok:
        result = {
            "ok": True,
            "attempted": True,
            "percent": percent,
            "count": len(writes),
            "wrote": writes,
            "method": "direct",
            "skipped_pump": released,
        }
        if kind == "nostick" and direct_err:
            result["warning"] = direct_err
        return result

    # Follow may pkexec the installed helper (allow_active=yes, no prompt).
    # Never fall back to pkexec of user-writable python3.
    if allow_pkexec and kind == "permission":
        privileged = run_privileged(["apply-pwms"], json.dumps({"writes": writes, "percent": percent}))
        if privileged.get("ok"):
            return {
                "ok": True,
                "attempted": True,
                "percent": percent,
                "count": privileged.get("count", len(writes)),
                "wrote": privileged.get("wrote") or writes,
                "method": "pkexec",
                "privileged": privileged,
                "skipped_pump": released,
            }
        return {
            "ok": False,
            "attempted": True,
            "percent": percent,
            "count": 0,
            "wrote": [],
            "skipped_pump": released,
            "error": privileged.get("error")
            or direct_err
            or "PWM write failed — run grant-access once to unlock passwordless control",
            "hint": "python3 scripts/fanctl.py grant-access",
        }
    return {
        "ok": False,
        "attempted": True,
        "percent": percent,
        "count": 0,
        "wrote": [],
        "skipped_pump": released,
        "error": direct_err
        or "PWM write failed — run grant-access once to unlock passwordless control",
        "hint": "python3 scripts/fanctl.py grant-access",
    }


def apply_hwmon_pwms(curve: dict[str, Any], preset: str | None = None) -> dict[str, Any]:
    devices = list_hwmon()
    cpu = group_cpu(devices)
    temp = cpu.get("temp")
    if preset in PRESET_DUTY:
        percent = PRESET_DUTY[preset]
    else:
        percent = max(curve_pwm_percent(temp, curve), 25)
    return apply_hwmon_percent(percent, require_spinning=True)


def follow_tick(
    last_percent: int | None,
    min_delta: int,
    curve: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int | None]:
    curve = curve or active_curve()
    devices = list_hwmon()
    cpu = group_cpu(devices)
    temp = cpu.get("temp")
    percent = curve_pwm_percent(temp, curve)
    payload: dict[str, Any] = {
        "ok": True,
        "temp": temp,
        "percent": percent,
        "curve": {k: curve[k] for k in ("low_temp", "high_temp", "max_temp", "polling_interval")},
        "skipped": False,
    }
    if last_percent is not None and abs(percent - last_percent) < min_delta:
        payload["skipped"] = True
        return payload, last_percent
    pwm = apply_hwmon_percent(percent, require_spinning=False, allow_pkexec=True)
    payload.update(pwm)
    if not pwm.get("ok", False):
        payload["ok"] = False
    return payload, percent


def write_follow_state(payload: dict[str, Any]) -> None:
    follow_state_dir().mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["running"] = True
    body["updated"] = int(time.time())
    body["pid"] = os.getpid()
    follow_state_file().write_text(json.dumps(body), encoding="utf-8")
    follow_pid_file().write_text(str(os.getpid()), encoding="utf-8")


def clear_follow_state() -> None:
    for path in (follow_pid_file(), follow_state_file(), follow_lock_file()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def read_follow_pid() -> int | None:
    if not follow_pid_file().exists():
        return None
    try:
        pid = int(follow_pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def read_follow_state() -> dict[str, Any]:
    if not follow_state_file().exists():
        return {"running": False}
    try:
        data = json.loads(follow_state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"running": False}
    pid = read_follow_pid()
    data["running"] = pid is not None
    if pid is not None:
        data["pid"] = pid
    return data


@contextmanager
def follow_lock() -> Iterator[None]:
    follow_state_dir().mkdir(parents=True, exist_ok=True)
    handle = open(follow_lock_file(), "w", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        except OSError as exc:
            raise RuntimeError("fan curve follower is already running") from exc
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()
        clear_follow_state()


def follow_loop(interval: float, min_delta: int) -> int:
    stop = False

    def handle_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    last_percent: int | None = None
    with follow_lock():
        write_follow_state({"ok": True, "starting": True})
        while not stop:
            try:
                result, last_percent = follow_tick(last_percent, min_delta)
                write_follow_state(result)
                if not result.get("ok", True) and result.get("error"):
                    sys.stderr.write(f"fanctl follow: {result['error']}\n")
            except Exception as exc:
                sys.stderr.write(f"fanctl follow: {exc}\n")
                write_follow_state({"ok": False, "error": str(exc), "running": True})
            for _ in range(max(1, int(interval * 10))):
                if stop:
                    break
                time.sleep(0.1)
        release_controlled_pwms()
    return 0


def ensure_follow_service() -> Path:
    unit_path = SYSTEMD_USER_DIR / SYSTEMD_UNIT_NAME
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    unit_path.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Omarchy fan curve follower",
                "After=default.target",
                "",
                "[Service]",
                "Type=simple",
                f"ExecStart={sys.executable} {script} follow run "
                f"--interval {FOLLOW_DEFAULT_INTERVAL} --min-delta {FOLLOW_DEFAULT_MIN_DELTA}",
                "Restart=on-failure",
                "RestartSec=5",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return unit_path


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def wait_for_follow_running(timeout: float = 5.0, interval: float = 0.15) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        status = follow_status()
        if status.get("running"):
            status["ok"] = True
            return status
        time.sleep(interval)
    status = follow_status()
    status["ok"] = bool(status.get("running"))
    return status


def follow_start() -> dict[str, Any]:
    if read_follow_pid() is not None:
        return {**follow_status(), "ok": True, "message": "already running"}
    if not shutil.which("systemctl"):
        return follow_start_detached()
    ensure_follow_service()
    _systemctl("daemon-reload")
    enable = _systemctl("enable", "--now", SYSTEMD_UNIT_NAME)
    if enable.returncode != 0:
        err = (enable.stderr or enable.stdout or "systemctl enable failed").strip()
        fallback = follow_start_detached()
        if fallback.get("ok"):
            fallback["warning"] = err
        return fallback
    status = wait_for_follow_running()
    if not status.get("running"):
        status["ok"] = False
        status["error"] = "follow service did not start"
    return status


def follow_start_detached() -> dict[str, Any]:
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "follow",
                "run",
                "--interval",
                str(FOLLOW_DEFAULT_INTERVAL),
                "--min-delta",
                str(FOLLOW_DEFAULT_MIN_DELTA),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    status = wait_for_follow_running()
    if status.get("running"):
        status["ok"] = True
        return status
    if proc.poll() is not None:
        status["ok"] = False
        status["error"] = f"follower exited immediately (code {proc.returncode})"
    else:
        status["ok"] = False
        status["error"] = "could not start detached follower"
    return status


def follow_stop() -> dict[str, Any]:
    if shutil.which("systemctl"):
        _systemctl("disable", "--now", SYSTEMD_UNIT_NAME)
    pid = read_follow_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if read_follow_pid() is None:
                break
            time.sleep(0.1)
    release_controlled_pwms()
    clear_follow_state()
    return {"ok": True, "running": False}


def follow_status() -> dict[str, Any]:
    state = read_follow_state()
    pid = read_follow_pid()
    running = pid is not None
    out: dict[str, Any] = {"ok": True, "running": running, "pid": pid}
    if running:
        out.update({k: state[k] for k in ("temp", "percent", "curve", "updated", "skipped") if k in state})
    return out


def _write_pwms_unprivileged(writes: list[dict[str, Any]]) -> tuple[bool, str | None, str]:
    """Write enable then PWM; retry once if the EC ignores the first store.

    Returns (ok, error, kind) with kind of "ok", "permission", "nostick", or "error".
    A store that the EC overwrites is not a permission failure — do not pkexec.
    """
    last_err: str | None = None
    permission = False
    wrote = False
    for _attempt in range(2):
        ok = True
        permission = False
        for item in writes:
            path = Path(item["path"])
            try:
                enable = item.get("enable_path")
                if enable:
                    Path(enable).write_text("1", encoding="utf-8")
                path.write_text(str(int(item["value"])), encoding="utf-8")
                wrote = True
            except OSError as exc:
                last_err = str(exc)
                ok = False
                if exc.errno in (errno.EACCES, errno.EPERM):
                    permission = True
                break
        if not ok:
            continue
        time.sleep(0.2)
        stuck = 0
        for item in writes:
            try:
                if int(Path(item["path"]).read_text().strip()) == int(item["value"]):
                    stuck += 1
            except (OSError, ValueError):
                continue
        if stuck:
            return True, None, "ok"
        last_err = (
            "PWM write did not stick (motherboard EC still owns the fans). "
            "On MSI boards install nct6687d-dkms-git and blacklist nct6683."
        )
        time.sleep(0.15)
    if permission:
        return False, last_err, "permission"
    if wrote:
        return True, last_err, "nostick"
    return False, last_err, "error"


def grant_access() -> dict[str, Any]:
    """One password prompt: install the root-owned helper and polkit policy."""
    result = run_privileged(["grant-access"])
    if result.get("ok"):
        return result
    return {
        "ok": False,
        "error": result.get("error") or "could not install the privileged helper",
        "hint": "pkexec installs /usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py",
    }


def revoke_access() -> dict[str, Any]:
    result = run_privileged(["revoke-access"])
    if result.get("ok"):
        return result
    return {
        "ok": False,
        "error": result.get("error") or "could not remove the privileged helper",
    }


def install_bundled_config() -> dict[str, Any]:
    source = BUNDLED_CONFIG
    curve = parse_mbpfan_conf(source.read_text(encoding="utf-8")) if source.exists() else DEFAULT_CURVE
    write_user_config(curve)
    payload = json.dumps(
        {
            "curve": {
                k: curve[k]
                for k in ("low_temp", "high_temp", "max_temp", "polling_interval")
            }
        }
    )
    privileged = run_privileged(["install-config"], payload)
    if privileged.get("ok"):
        return {"ok": True, "path": str(DEFAULT_SYSTEM_CONFIG), "privileged": privileged}
    return {
        "ok": False,
        "error": privileged.get("error") or "could not read /etc/mbpfan.conf",
        "user_path": str(DEFAULT_USER_CONFIG),
        "hint": "pkexec writes a validated curve to /etc/mbpfan.conf",
    }


def curve_pwm_percent(temp: float | None, curve: dict[str, Any]) -> int:
    if temp is None:
        return 30
    low = float(curve["low_temp"])
    high = float(curve["high_temp"])
    maximum = float(curve["max_temp"])
    if temp <= low:
        return 25
    if temp >= maximum:
        return 100
    if temp <= high or high <= low:
        span = max(high - low, 1.0)
        return int(25 + (temp - low) / span * 50)
    span = max(maximum - high, 1.0)
    return int(75 + (temp - high) / span * 25)


def emit(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Omarchy CPU/GPU fan control helper")
    sub = parser.add_subparsers(dest="cmd")

    snap = sub.add_parser("snapshot", help="print one JSON snapshot")
    snap.add_argument("--demo", action="store_true")
    snap.add_argument("--interval", type=float, default=0, help="repeat every N seconds")

    inst = sub.add_parser("install-config", help="copy bundled mbpfan.conf to /etc")
    inst.add_argument("--user-only", action="store_true")

    sub.add_parser(
        "grant-access",
        help="install the root-owned PWM helper (password once)",
    )
    sub.add_parser(
        "revoke-access",
        help="remove the helper, polkit policy, and leftover udev chmod rules",
    )

    apply_cmd = sub.add_parser("apply", help="write a fan curve")
    apply_cmd.add_argument("--preset", choices=sorted(PRESETS))
    apply_cmd.add_argument("--low", type=int)
    apply_cmd.add_argument("--high", type=int)
    apply_cmd.add_argument("--max", type=int)
    apply_cmd.add_argument("--user-only", action="store_true")

    follow = sub.add_parser("follow", help="continuous temperature curve follower")
    follow_sub = follow.add_subparsers(dest="follow_cmd")

    follow_run = follow_sub.add_parser("run", help="run follower loop (foreground)")
    follow_run.add_argument("--interval", type=float, default=FOLLOW_DEFAULT_INTERVAL)
    follow_run.add_argument("--min-delta", type=int, default=FOLLOW_DEFAULT_MIN_DELTA)

    follow_sub.add_parser("start", help="start follower (systemd user service or detached)")
    follow_sub.add_parser("stop", help="stop follower and return fans to EC auto mode")
    follow_sub.add_parser("status", help="print follower state JSON")

    follow_once = follow_sub.add_parser("once", help="apply one curve tick (testing)")
    follow_once.add_argument("--min-delta", type=int, default=FOLLOW_DEFAULT_MIN_DELTA)

    args = parser.parse_args(argv)
    cmd = args.cmd or "snapshot"

    if cmd == "follow":
        follow_cmd = args.follow_cmd or "status"
        if follow_cmd == "run":
            return follow_loop(float(args.interval), int(args.min_delta))
        if follow_cmd == "start":
            emit(follow_start())
            return 0
        if follow_cmd == "stop":
            emit(follow_stop())
            return 0
        if follow_cmd == "once":
            result, _ = follow_tick(None, int(args.min_delta))
            emit(result)
            return 0
        emit(follow_status())
        return 0

    if cmd == "grant-access":
        emit(grant_access())
        return 0

    if cmd == "revoke-access":
        emit(revoke_access())
        return 0

    if cmd == "install-config":
        if args.user_only:
            source = BUNDLED_CONFIG if BUNDLED_CONFIG.exists() else None
            curve = parse_mbpfan_conf(source.read_text(encoding="utf-8")) if source else DEFAULT_CURVE
            path = write_user_config(curve)
            emit({"ok": True, "path": str(path)})
            return 0
        emit(install_bundled_config())
        return 0

    if cmd == "apply":
        curve = dict(DEFAULT_CURVE)
        if args.preset:
            curve.update(PRESETS[args.preset])
            curve["preset"] = args.preset
        if args.low is not None:
            curve["low_temp"] = args.low
        if args.high is not None:
            curve["high_temp"] = args.high
        if args.max is not None:
            curve["max_temp"] = args.max
        emit(apply_curve(curve, install_system=not args.user_only))
        return 0

    force_demo = bool(getattr(args, "demo", False))
    interval = float(getattr(args, "interval", 0) or 0)
    if interval > 0:
        while True:
            try:
                emit(snapshot(force_demo=force_demo))
            except Exception as exc:
                emit({"ok": False, "error": str(exc), "cpu": None, "gpu": None})
            time.sleep(interval)
    emit(snapshot(force_demo=force_demo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
