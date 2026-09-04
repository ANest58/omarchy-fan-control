#!/usr/bin/env python3
"""pkexec helper for fan-control. Only writes known config files and PWM nodes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ALLOWED_CONFIG = Path("/etc/mbpfan.conf")
UDEV_RULE = Path("/etc/udev/rules.d/99-io.github.anesturi.fan-control.rules")
POLKIT_POLICY = Path("/usr/share/polkit-1/actions/io.github.anesturi.fan-control.policy")
HWMON_PWM = re.compile(
    r"^/sys/(?:class/hwmon/hwmon\d+|devices/platform/nct668[37]\.\d+/hwmon/hwmon\d+)/pwm\d+$"
)
HWMON_ENABLE = re.compile(
    r"^/sys/(?:class/hwmon/hwmon\d+|devices/platform/nct668[37]\.\d+/hwmon/hwmon\d+)/pwm\d+_enable$"
)
APPLE_OUTPUT = re.compile(r"^/sys/devices/platform/applesmc\.\d+/fan\d+_output$")
APPLE_MANUAL = re.compile(r"^/sys/devices/platform/applesmc\.\d+/fan\d+_manual$")
PLUGIN_DIR = Path(__file__).resolve().parent.parent
BUNDLED_UDEV = PLUGIN_DIR / "udev" / "99-io.github.anesturi.fan-control.rules"
BUNDLED_POLKIT = PLUGIN_DIR / "polkit" / "io.github.anesturi.fan-control.policy"
BUNDLED_UNLOCK = PLUGIN_DIR / "scripts" / "pwm-unlock.sh"
BUNDLED_UNLOCK_UNIT = PLUGIN_DIR / "scripts" / "fan-control-pwm-unlock.service"
UNLOCK_SCRIPT = Path("/usr/lib/io.github.anesturi.fan-control/pwm-unlock.sh")
UNLOCK_SLEEP_HOOK = Path("/usr/lib/systemd/system-sleep/io.github.anesturi.fan-control")
UNLOCK_UNIT = Path("/etc/systemd/system/io.github.anesturi.fan-control-pwm.service")


def fail(message: str, code: int = 1) -> int:
    sys.stderr.write(message + "\n")
    return code


def emit(payload: dict) -> int:
    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


def read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("payload must be an object")
    return data


def render_conf(curve: dict) -> str:
    return (
        "[general]\n"
        "# Written by the Omarchy fan-control widget.\n"
        f"low_temp = {int(curve['low_temp'])}\n"
        f"high_temp = {int(curve['high_temp'])}\n"
        f"max_temp = {int(curve['max_temp'])}\n"
        f"polling_interval = {int(curve.get('polling_interval', 1))}\n"
    )


def validate_curve(curve: dict) -> dict:
    out = {}
    for key in ("low_temp", "high_temp", "max_temp", "polling_interval"):
        if key not in curve and key == "polling_interval":
            out[key] = 1
            continue
        value = int(curve[key])
        if key == "polling_interval":
            if value < 1 or value > 10:
                raise ValueError("polling_interval out of range")
        elif value < 30 or value > 105:
            raise ValueError(f"{key} out of range")
        out[key] = value
    if out["low_temp"] > out["high_temp"] or out["high_temp"] > out["max_temp"]:
        raise ValueError("temps must satisfy low <= high <= max")
    return out


def restart_units() -> list[str]:
    restarted = []
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return restarted
    for unit in ("mbpfan.service", "t2fanrd.service"):
        probe = subprocess.run(
            [systemctl, "is-enabled", "--quiet", unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        active = subprocess.run(
            [systemctl, "is-active", "--quiet", unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode != 0 and active.returncode != 0:
            continue
        subprocess.run([systemctl, "restart", unit], check=False)
        restarted.append(unit)
    return restarted


def install_config(source: str) -> int:
    src = Path(source).resolve()
    if not src.is_file():
        return fail(f"could not read {source}")
    text = src.read_text(encoding="utf-8")
    if "[general]" not in text or "low_temp" not in text:
        return fail("source is not an mbpfan.conf")
    ALLOWED_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_CONFIG.write_text(text, encoding="utf-8")
    os.chmod(ALLOWED_CONFIG, 0o644)
    restarted = restart_units()
    return emit({"ok": True, "path": str(ALLOWED_CONFIG), "restarted": restarted})


def apply_config() -> int:
    payload = read_stdin_json()
    curve = validate_curve(payload.get("curve") or payload)
    ALLOWED_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_CONFIG.write_text(render_conf(curve), encoding="utf-8")
    os.chmod(ALLOWED_CONFIG, 0o644)
    restarted = restart_units()
    return emit({"ok": True, "path": str(ALLOWED_CONFIG), "restarted": restarted, "curve": curve})


def allowed_sysfs(path_str: str) -> bool:
    # Validate both the /sys/class/hwmon path and the resolved platform path.
    # nct6687d resolves class links under /sys/devices/platform/nct6687.*.
    candidates = {path_str}
    try:
        candidates.add(str(Path(path_str).resolve()))
    except OSError:
        pass
    return any(
        HWMON_PWM.match(p)
        or HWMON_ENABLE.match(p)
        or APPLE_OUTPUT.match(p)
        or APPLE_MANUAL.match(p)
        for p in candidates
    )


def apply_pwms() -> int:
    """Write many PWM/enable nodes in one privileged session."""
    payload = read_stdin_json()
    writes = payload.get("writes") or []
    if not isinstance(writes, list) or not writes:
        return fail("apply-pwms requires writes[]")
    done = []
    for item in writes:
        if not isinstance(item, dict):
            return fail("each write must be an object")
        path = Path(str(item.get("path") or ""))
        value = int(item.get("value"))
        path_str = str(path)
        if not allowed_sysfs(path_str):
            return fail(f"refusing to write {path_str}")
        if value < 0 or value > 25500:
            return fail(f"value out of range for {path_str}")
        enable = item.get("enable_path")
        if enable:
            enable_path = Path(str(enable))
            enable_str = str(enable_path)
            if not allowed_sysfs(enable_str):
                return fail(f"refusing to write {enable_str}")
            # 1 = manual mode on most hwmon PWM controllers
            enable_path.write_text("1", encoding="utf-8")
        path.write_text(str(value), encoding="utf-8")
        done.append({"path": path_str, "value": value})
    return emit({"ok": True, "wrote": done, "count": len(done)})


def chmod_pwm_tree() -> list[str]:
    changed: list[str] = []
    root = Path("/sys/class/hwmon")
    if not root.exists():
        return changed
    for hwmon in sorted(root.glob("hwmon*")):
        for path in list(hwmon.glob("pwm[0-9]")) + list(hwmon.glob("pwm[0-9]_enable")):
            try:
                os.chmod(path, 0o666)
                changed.append(str(path))
            except OSError:
                continue
    return changed


def install_pwm_unlock_hooks() -> dict[str, str | None]:
    """Boot oneshot + resume hook so PWM nodes stay user-writable after udev races."""
    unlock_path = None
    sleep_path = None
    unit_path = None
    if BUNDLED_UNLOCK.is_file():
        UNLOCK_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
        text = BUNDLED_UNLOCK.read_text(encoding="utf-8")
        UNLOCK_SCRIPT.write_text(text, encoding="utf-8")
        os.chmod(UNLOCK_SCRIPT, 0o755)
        unlock_path = str(UNLOCK_SCRIPT)
        UNLOCK_SLEEP_HOOK.parent.mkdir(parents=True, exist_ok=True)
        UNLOCK_SLEEP_HOOK.write_text(text, encoding="utf-8")
        os.chmod(UNLOCK_SLEEP_HOOK, 0o755)
        sleep_path = str(UNLOCK_SLEEP_HOOK)
    if BUNDLED_UNLOCK_UNIT.is_file():
        UNLOCK_UNIT.write_text(BUNDLED_UNLOCK_UNIT.read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(UNLOCK_UNIT, 0o644)
        unit_path = str(UNLOCK_UNIT)
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
        subprocess.run(
            ["systemctl", "enable", "--now", UNLOCK_UNIT.name],
            check=False,
            capture_output=True,
        )
    return {"unlock": unlock_path, "sleep": sleep_path, "unit": unit_path}


def grant_access() -> int:
    """One-time: chmod live PWM nodes + install udev so presets stay passwordless."""
    changed = chmod_pwm_tree()
    udev_path = None
    if BUNDLED_UDEV.is_file():
        UDEV_RULE.parent.mkdir(parents=True, exist_ok=True)
        UDEV_RULE.write_text(BUNDLED_UDEV.read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(UDEV_RULE, 0o644)
        udev_path = str(UDEV_RULE)
        subprocess.run(["udevadm", "control", "--reload"], check=False, capture_output=True)
        subprocess.run(["udevadm", "trigger", "--subsystem-match=hwmon"], check=False, capture_output=True)
        changed = chmod_pwm_tree() or changed

    polkit_path = None
    if BUNDLED_POLKIT.is_file():
        POLKIT_POLICY.parent.mkdir(parents=True, exist_ok=True)
        POLKIT_POLICY.write_text(BUNDLED_POLKIT.read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(POLKIT_POLICY, 0o644)
        polkit_path = str(POLKIT_POLICY)

    hooks = install_pwm_unlock_hooks()
    return emit(
        {
            "ok": True,
            "chmod": changed,
            "count": len(changed),
            "udev": udev_path,
            "polkit": polkit_path,
            **hooks,
        }
    )


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        return fail("fanctl-privileged must run as root (use pkexec)")
    if not argv:
        return fail(
            "usage: fanctl-privileged.py install-config PATH | apply-config | "
            "apply-pwms | grant-access"
        )
    cmd = argv[0]
    try:
        if cmd == "install-config":
            if len(argv) < 2:
                return fail("install-config requires a source path")
            return install_config(argv[1])
        if cmd == "apply-config":
            return apply_config()
        if cmd == "apply-pwms":
            return apply_pwms()
        if cmd == "grant-access":
            return grant_access()
        return fail(f"unknown command {cmd}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
