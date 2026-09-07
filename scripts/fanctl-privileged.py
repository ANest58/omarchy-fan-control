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
HWMON_NODE = re.compile(r"^hwmon\d+$")
PWM_NODE = re.compile(r"^pwm\d+(?:_enable)?$")
APPLE_NODE = re.compile(r"^fan\d+_(?:output|manual)$")

HELPER_DIR = Path("/usr/lib/io.github.anesturi.fan-control")
INSTALLED_HELPER = HELPER_DIR / "fanctl-privileged.py"
UNLOCK_SLEEP_HOOK = Path("/usr/lib/systemd/system-sleep/io.github.anesturi.fan-control")
UNLOCK_UNIT = Path("/etc/systemd/system/io.github.anesturi.fan-control-pwm.service")
LEGACY_UNLOCK_SCRIPT = HELPER_DIR / "pwm-unlock.sh"

POLKIT_POLICY_TEXT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <vendor>Omarchy fan control</vendor>
  <vendor_url>https://omarchy.org</vendor_url>

  <action id="io.github.anesturi.fan-control.apply-pwms">
    <description>Apply motherboard PWM values through the Tornaider helper</description>
    <message>Authentication is required to set fan speeds</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>yes</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py</annotate>
    <annotate key="org.freedesktop.policykit.exec.argv1">apply-pwms</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>

  <action id="io.github.anesturi.fan-control.grant">
    <description>Install the Tornaider privileged helper</description>
    <message>Authentication is required to install the fan-control helper</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py</annotate>
    <annotate key="org.freedesktop.policykit.exec.argv1">grant-access</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>

  <action id="io.github.anesturi.fan-control.revoke">
    <description>Remove the Tornaider privileged helper</description>
    <message>Authentication is required to remove the fan-control helper</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py</annotate>
    <annotate key="org.freedesktop.policykit.exec.argv1">revoke-access</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>

  <action id="io.github.anesturi.fan-control.install-config">
    <description>Install /etc/mbpfan.conf</description>
    <message>Authentication is required to write /etc/mbpfan.conf</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py</annotate>
    <annotate key="org.freedesktop.policykit.exec.argv1">install-config</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>

  <action id="io.github.anesturi.fan-control.apply-config">
    <description>Write /etc/mbpfan.conf from a validated curve</description>
    <message>Authentication is required to write /etc/mbpfan.conf</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py</annotate>
    <annotate key="org.freedesktop.policykit.exec.argv1">apply-config</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>
</policyconfig>
"""


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


def install_config() -> int:
    """Write /etc/mbpfan.conf from a validated curve. Never copy a user path."""
    return apply_config()


def apply_config() -> int:
    payload = read_stdin_json()
    curve = validate_curve(payload.get("curve") or payload)
    ALLOWED_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_CONFIG.write_text(render_conf(curve), encoding="utf-8")
    os.chmod(ALLOWED_CONFIG, 0o644)
    restarted = restart_units()
    return emit({"ok": True, "path": str(ALLOWED_CONFIG), "restarted": restarted, "curve": curve})


def allowed_sysfs(path_str: str) -> bool:
    """Allow only resolved hwmon PWM or Apple SMC fan nodes under /sys."""
    try:
        resolved = Path(path_str).resolve()
    except OSError:
        return False
    text = str(resolved)
    if ".." in resolved.parts:
        return False
    under_hwmon = text.startswith("/sys/class/hwmon/") or text.startswith("/sys/devices/")
    if not under_hwmon:
        return False
    name = resolved.name
    parent = resolved.parent.name
    if PWM_NODE.match(name) and HWMON_NODE.match(parent):
        return True
    if APPLE_NODE.match(name) and parent.startswith("applesmc."):
        return True
    return False


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


def restore_pwm_tree() -> list[str]:
    """Put PWM sysfs nodes back to root-only (0644). Never chmod them 0666."""
    restored: list[str] = []
    root = Path("/sys/class/hwmon")
    if not root.exists():
        return restored
    for hwmon in sorted(root.glob("hwmon*")):
        for path in list(hwmon.glob("pwm[0-9]")) + list(hwmon.glob("pwm[0-9]_enable")):
            try:
                os.chmod(path, 0o644)
                restored.append(str(path))
            except OSError:
                continue
    return restored


def _owned_root_not_group_or_world_writable(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_uid == 0 and (st.st_mode & 0o022) == 0


def install_trusted_helper() -> str:
    """Copy this running helper into a root-owned path and verify the result."""
    HELPER_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(HELPER_DIR, 0, 0)
    os.chmod(HELPER_DIR, 0o755)
    payload = Path(__file__).resolve().read_bytes()
    INSTALLED_HELPER.write_bytes(payload)
    os.chown(INSTALLED_HELPER, 0, 0)
    os.chmod(INSTALLED_HELPER, 0o755)
    if not _owned_root_not_group_or_world_writable(INSTALLED_HELPER):
        raise OSError("installed helper is not root-owned and non-writable")
    if INSTALLED_HELPER.read_bytes() != payload:
        raise OSError("installed helper content does not match the running helper")
    return str(INSTALLED_HELPER)


def remove_legacy_world_writable_pwm() -> list[str]:
    removed: list[str] = []
    for path in (
        UDEV_RULE,
        UNLOCK_SLEEP_HOOK,
        UNLOCK_UNIT,
        LEGACY_UNLOCK_SCRIPT,
    ):
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    if UNLOCK_UNIT.name:
        subprocess.run(
            ["systemctl", "disable", "--now", UNLOCK_UNIT.name],
            check=False,
            capture_output=True,
        )
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
    subprocess.run(["udevadm", "control", "--reload"], check=False, capture_output=True)
    subprocess.run(
        ["udevadm", "trigger", "--subsystem-match=hwmon"],
        check=False,
        capture_output=True,
    )
    return removed


def grant_access() -> int:
    """Install the root-owned helper + polkit. Do not chmod PWM world-writable."""
    helper_path = install_trusted_helper()
    POLKIT_POLICY.parent.mkdir(parents=True, exist_ok=True)
    POLKIT_POLICY.write_text(POLKIT_POLICY_TEXT, encoding="utf-8")
    os.chown(POLKIT_POLICY, 0, 0)
    os.chmod(POLKIT_POLICY, 0o644)
    if not _owned_root_not_group_or_world_writable(POLKIT_POLICY):
        return fail("installed polkit policy is not root-owned and non-writable")
    if POLKIT_POLICY.read_text(encoding="utf-8") != POLKIT_POLICY_TEXT:
        return fail("installed polkit policy does not match the bound helper text")
    restored = restore_pwm_tree()
    removed = remove_legacy_world_writable_pwm()
    restored = restore_pwm_tree() or restored
    return emit(
        {
            "ok": True,
            "helper": helper_path,
            "polkit": str(POLKIT_POLICY),
            "pwm_restored": restored,
            "removed_legacy": removed,
            "count": len(restored),
        }
    )


def revoke_access() -> int:
    """Remove helper, polkit, leftover udev/hooks, and restore PWM perms."""
    removed = remove_legacy_world_writable_pwm()
    restored = restore_pwm_tree()
    if POLKIT_POLICY.is_file():
        POLKIT_POLICY.unlink()
        removed.append(str(POLKIT_POLICY))
    if INSTALLED_HELPER.is_file():
        INSTALLED_HELPER.unlink()
        removed.append(str(INSTALLED_HELPER))
    if HELPER_DIR.is_dir():
        for leftover in HELPER_DIR.iterdir():
            leftover.unlink()
            removed.append(str(leftover))
        HELPER_DIR.rmdir()
        removed.append(str(HELPER_DIR))
    return emit({"ok": True, "removed": removed, "pwm_restored": restored})


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        return fail("fanctl-privileged must run as root (use pkexec)")
    if not argv:
        return fail(
            "usage: fanctl-privileged.py install-config PATH | apply-config | "
            "apply-pwms | grant-access | revoke-access"
        )
    cmd = argv[0]
    try:
        if cmd == "install-config":
            return install_config()
        if cmd == "apply-config":
            return apply_config()
        if cmd == "apply-pwms":
            return apply_pwms()
        if cmd == "grant-access":
            return grant_access()
        if cmd == "revoke-access":
            return revoke_access()
        return fail(f"unknown command {cmd}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
