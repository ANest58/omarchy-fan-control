#!/usr/bin/env python3
"""Self-test for scripts/fanctl.py using a fake hwmon tree."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fanctl  # noqa: E402

_priv_spec = importlib.util.spec_from_file_location(
    "fanctl_privileged", ROOT / "scripts" / "fanctl-privileged.py"
)
assert _priv_spec and _priv_spec.loader
privileged = importlib.util.module_from_spec(_priv_spec)
_priv_spec.loader.exec_module(privileged)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ConfigTests(unittest.TestCase):
    def test_parse_ignores_comments_and_blank_lines(self) -> None:
        text = """
# heading
[general]
low_temp = 55 # warm
high_temp = 70
max_temp = 88
polling_interval = 2
"""
        curve = fanctl.parse_mbpfan_conf(text)
        self.assertEqual(curve["low_temp"], 55)
        self.assertEqual(curve["high_temp"], 70)
        self.assertEqual(curve["max_temp"], 88)
        self.assertEqual(curve["polling_interval"], 2)

    def test_parse_swaps_inverted_low_high(self) -> None:
        curve = fanctl.parse_mbpfan_conf("low_temp = 80\nhigh_temp = 60\nmax_temp = 90\n")
        self.assertEqual(curve["low_temp"], 60)
        self.assertEqual(curve["high_temp"], 80)

    def test_bundled_config_is_readable(self) -> None:
        bundled = ROOT / "etc" / "mbpfan.conf"
        self.assertTrue(bundled.is_file(), "plugin must ship etc/mbpfan.conf")
        curve = fanctl.parse_mbpfan_conf(bundled.read_text(encoding="utf-8"))
        self.assertIn("low_temp", curve)

    def test_display_config_prefers_user_over_system(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            system = root / "system.conf"
            user = root / "user.conf"
            write(system, "low_temp = 50\nhigh_temp = 60\nmax_temp = 75\npolling_interval = 1\n")
            write(user, "low_temp = 70\nhigh_temp = 78\nmax_temp = 90\npolling_interval = 1\n")
            original_system = fanctl.DEFAULT_SYSTEM_CONFIG
            original_user = fanctl.DEFAULT_USER_CONFIG
            fanctl.DEFAULT_SYSTEM_CONFIG = system
            fanctl.DEFAULT_USER_CONFIG = user
            try:
                system_cfg = fanctl.load_config()
                display_cfg = fanctl.load_display_config()
            finally:
                fanctl.DEFAULT_SYSTEM_CONFIG = original_system
                fanctl.DEFAULT_USER_CONFIG = original_user
            self.assertEqual(system_cfg["low_temp"], 50)
            self.assertEqual(display_cfg["low_temp"], 70)


class MetricTests(unittest.TestCase):
    def test_thermal_percent_scales_to_ceiling(self) -> None:
        self.assertEqual(fanctl.thermal_percent(40, 100), 40.0)
        self.assertEqual(fanctl.thermal_percent(120, 100), 100.0)

    def test_cpu_usage_from_stat_delta(self) -> None:
        original = fanctl._CPU_STAT_PREV
        fanctl._CPU_STAT_PREV = (1000, 800)
        try:
            with mock.patch.object(
                Path,
                "read_text",
                return_value="cpu 200 0 200 0 820 0 0 0 0 0 0\n",
            ):
                usage = fanctl.cpu_usage_percent()
        finally:
            fanctl._CPU_STAT_PREV = original
        self.assertAlmostEqual(usage, 90.9, places=1)


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.hwmon = self.root / "hwmon"
        self.hwmon.mkdir()
        self._saved = {key: os.environ.get(key) for key in (
            "FANCTL_HWMON_ROOT", "FANCTL_DRM_ROOT", "FANCTL_CONFIG_PATHS", "FANCTL_DEMO"
        )}
        os.environ["FANCTL_HWMON_ROOT"] = str(self.hwmon)
        os.environ["FANCTL_DRM_ROOT"] = str(self.root / "missing-drm")
        os.environ.pop("FANCTL_DEMO", None)
        os.environ.pop("FANCTL_CONFIG_PATHS", None)

        def restore() -> None:
            for key, value in self._saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        self.addCleanup(self.tmpdir.cleanup)

    def test_reads_cpu_gpu_and_fans(self) -> None:
        cpu = self.hwmon / "hwmon0"
        write(cpu / "name", "coretemp")
        write(cpu / "temp1_input", "54000")
        write(cpu / "temp1_label", "Package id 0")
        write(cpu / "fan1_input", "2100")
        write(cpu / "fan1_label", "CPU fan")
        write(cpu / "pwm1", "90")
        write(cpu / "pwm1_max", "255")

        gpu = self.hwmon / "hwmon1"
        write(gpu / "name", "amdgpu")
        write(gpu / "temp1_input", "61000")
        write(gpu / "temp1_label", "edge")
        write(gpu / "fan1_input", "1800")
        write(gpu / "pwm1", "120")

        config = self.root / "etc" / "mbpfan.conf"
        write(config, "[general]\nlow_temp = 63\nhigh_temp = 66\nmax_temp = 86\n")
        os.environ["FANCTL_CONFIG_PATHS"] = str(config)

        data = fanctl.snapshot()
        self.assertFalse(data["demo"])
        self.assertEqual(data["cpu"]["temp"], 54.0)
        self.assertEqual(data["gpu"]["temp"], 61.0)
        self.assertEqual(data["cpu"]["fans"][0]["rpm"], 2100)
        self.assertEqual(data["gpu"]["fans"][0]["rpm"], 1800)
        self.assertEqual(data["label"], "54° 61°")
        self.assertFalse(data["alert"])
        self.assertFalse(data["config"]["missing"])

    def test_missing_system_config_uses_bundled_and_reports_the_error(self) -> None:
        os.environ["FANCTL_CONFIG_PATHS"] = str(self.root / "absent.conf")
        # Point bundled lookup at the real plugin files by leaving PLUGIN_DIR.
        cpu = self.hwmon / "hwmon0"
        write(cpu / "name", "coretemp")
        write(cpu / "temp1_input", "81000")
        data = fanctl.snapshot()
        self.assertTrue(data["alert"])
        self.assertEqual(data["cpu"]["temp"], 81.0)
        # Host may already have /etc/mbpfan.conf; only assert the missing flag then.
        if not Path("/etc/mbpfan.conf").exists():
            self.assertTrue(data["config"]["system_missing"] or data["backend"]["config_missing"])

    def test_picks_up_super_io_fans_when_coretemp_has_none(self) -> None:
        cpu = self.hwmon / "hwmon0"
        write(cpu / "name", "coretemp")
        write(cpu / "temp1_input", "29000")
        sio = self.hwmon / "hwmon2"
        write(sio / "name", "nct6798")
        write(sio / "fan1_input", "980")
        write(sio / "fan1_label", "CPU")
        write(sio / "fan2_input", "0")
        write(sio / "fan2_label", "Chassis")
        os.environ["FANCTL_CONFIG_PATHS"] = str(self.root / "absent.conf")
        data = fanctl.snapshot()
        labels = [f["label"] for f in data["cpu"]["fans"]]
        rpms = [f["rpm"] for f in data["cpu"]["fans"]]
        self.assertIn("CPU", labels)
        self.assertIn(980, rpms)
        self.assertEqual(data["cpu"]["idle_fans"], 1)
        self.assertNotIn(0, rpms)
        self.assertIn("thermal_percent", data["cpu"])
        self.assertIn("usage", data["cpu"])

    def test_demo_snapshot_has_cpu_and_gpu(self) -> None:
        data = fanctl.snapshot(force_demo=True)
        self.assertTrue(data["demo"])
        self.assertIsNotNone(data["cpu"]["temp"])
        self.assertIsNotNone(data["gpu"]["temp"])
        self.assertTrue(data["cpu"]["fans"])
        self.assertTrue(data["gpu"]["fans"])
        self.assertIn("°", data["label"])

    def test_empty_hwmon_returns_monitor_state_not_demo(self) -> None:
        with mock.patch.object(fanctl, "nvidia_gpu", return_value=None), mock.patch.object(
            fanctl, "gpu_from_drm", return_value=None
        ):
            data = fanctl.snapshot()
        self.assertFalse(data["demo"])
        self.assertEqual(data["backend"]["name"], "monitor")
        self.assertIsNone(data["cpu"]["temp"])
        self.assertIsNone(data["gpu"]["temp"])
        self.assertFalse(data["cpu"]["fans"])
        self.assertIn("No temperature or fan sensors found", data["backend"]["notes"][0])


class CurveTests(unittest.TestCase):
    def test_pwm_percent_clamps_below_low(self) -> None:
        curve = {"low_temp": 63, "high_temp": 66, "max_temp": 86}
        self.assertEqual(fanctl.curve_pwm_percent(40, curve), 25)
        self.assertEqual(fanctl.curve_pwm_percent(90, curve), 100)

    def test_presets_have_distinct_duties(self) -> None:
        self.assertLess(fanctl.PRESET_DUTY["quiet"], fanctl.PRESET_DUTY["balanced"])
        self.assertLess(fanctl.PRESET_DUTY["balanced"], fanctl.PRESET_DUTY["cool"])

    def test_write_user_config_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mbpfan.conf"
            original = fanctl.DEFAULT_USER_CONFIG
            fanctl.DEFAULT_USER_CONFIG = path
            try:
                written = fanctl.write_user_config(
                    {"low_temp": 50, "high_temp": 60, "max_temp": 75, "polling_interval": 1}
                )
                text = written.read_text(encoding="utf-8")
                self.assertIn("low_temp = 50", text)
                parsed = fanctl.parse_mbpfan_conf(text)
                self.assertEqual(parsed["max_temp"], 75)
            finally:
                fanctl.DEFAULT_USER_CONFIG = original


class HwmonApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.hwmon = self.root / "hwmon"
        self.hwmon.mkdir()
        self._saved = {key: os.environ.get(key) for key in (
            "FANCTL_HWMON_ROOT", "FANCTL_DRM_ROOT", "FANCTL_CONFIG_PATHS", "FANCTL_DEMO"
        )}
        os.environ["FANCTL_HWMON_ROOT"] = str(self.hwmon)
        os.environ["FANCTL_DRM_ROOT"] = str(self.root / "missing-drm")
        os.environ.pop("FANCTL_DEMO", None)
        os.environ.pop("FANCTL_CONFIG_PATHS", None)

        def restore() -> None:
            for key, value in self._saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        self.addCleanup(self.tmpdir.cleanup)

    def test_backend_can_control_when_pwm_present(self) -> None:
        cpu = self.hwmon / "hwmon0"
        write(cpu / "name", "coretemp")
        write(cpu / "temp1_input", "40000")
        sio = self.hwmon / "hwmon1"
        write(sio / "name", "nct6687")
        write(sio / "fan1_input", "1200")
        write(sio / "pwm1", "100")
        os.environ["FANCTL_CONFIG_PATHS"] = str(self.root / "absent.conf")
        data = fanctl.snapshot()
        self.assertEqual(data["backend"]["name"], "hwmon")
        self.assertTrue(data["backend"]["can_control"])

    def test_apply_curve_writes_pwm_without_mbpfan(self) -> None:
        cpu = self.hwmon / "hwmon0"
        write(cpu / "name", "coretemp")
        write(cpu / "temp1_input", "40000")
        sio = self.hwmon / "hwmon1"
        write(sio / "name", "nct6687")
        write(sio / "fan1_input", "1200")
        pwm = sio / "pwm1"
        write(pwm, "100")
        # Not user-writable → must go through the single privileged apply-pwms call.
        os.chmod(pwm, 0o444)

        calls: list[tuple] = []

        def fake_privileged(args, stdin_text=None):
            calls.append((list(args), stdin_text))
            if args and args[0] == "apply-pwms":
                payload = json.loads(stdin_text or "{}")
                for item in payload.get("writes") or []:
                    target = Path(item["path"])
                    os.chmod(target, 0o644)
                    target.write_text(str(item["value"]), encoding="utf-8")
                return {"ok": True, "count": len(payload.get("writes") or []), "wrote": payload.get("writes")}
            return {"ok": False, "error": "unexpected"}

        original = fanctl.run_privileged
        original_user = fanctl.DEFAULT_USER_CONFIG
        fanctl.run_privileged = fake_privileged  # type: ignore[assignment]
        fanctl.DEFAULT_USER_CONFIG = self.root / "user-mbpfan.conf"
        try:
            result = fanctl.apply_curve({**fanctl.PRESETS["cool"], "preset": "cool"}, install_system=True)
        finally:
            fanctl.run_privileged = original  # type: ignore[assignment]
            fanctl.DEFAULT_USER_CONFIG = original_user

        self.assertTrue(result["ok"])
        self.assertTrue(result["pwm"]["attempted"])
        self.assertEqual(result["pwm"]["percent"], fanctl.PRESET_DUTY["cool"])
        self.assertEqual(
            int(pwm.read_text().strip()),
            int(round(255 * fanctl.PRESET_DUTY["cool"] / 100)),
        )
        self.assertTrue(any(c[0] and c[0][0] == "apply-pwms" for c in calls))
        # Desktop path must not insist on writing /etc/mbpfan.conf.
        self.assertFalse(any(c[0] and c[0][0] == "apply-config" for c in calls))


class FollowTests(unittest.TestCase):
    def test_follow_tick_hysteresis_skips_small_change(self) -> None:
        curve = {"low_temp": 63, "high_temp": 66, "max_temp": 86, "polling_interval": 1}
        with mock.patch.object(fanctl, "list_hwmon", return_value=[]), mock.patch.object(
            fanctl, "group_cpu", return_value={"temp": 64.0}
        ), mock.patch.object(fanctl, "apply_hwmon_percent") as apply_mock:
            result, last = fanctl.follow_tick(45, min_delta=5, curve=curve)
        apply_mock.assert_not_called()
        self.assertTrue(result["skipped"])
        self.assertEqual(last, 45)
        self.assertEqual(result["percent"], 41)

    def test_follow_tick_applies_when_delta_large_enough(self) -> None:
        curve = {"low_temp": 50, "high_temp": 60, "max_temp": 75, "polling_interval": 1}
        with mock.patch.object(fanctl, "list_hwmon", return_value=[]), mock.patch.object(
            fanctl, "group_cpu", return_value={"temp": 80.0}
        ), mock.patch.object(
            fanctl,
            "apply_hwmon_percent",
            return_value={"ok": True, "attempted": True, "percent": 100},
        ) as apply_mock:
            result, last = fanctl.follow_tick(45, min_delta=5, curve=curve)
        apply_mock.assert_called_once_with(100, require_spinning=False, allow_pkexec=True)
        self.assertFalse(result["skipped"])
        self.assertEqual(last, 100)

    def test_apply_percent_skips_pkexec_when_write_does_not_stick(self) -> None:
        fans = [
            {
                "pwm_path": "/sys/class/hwmon/hwmon7/pwm1",
                "pwm_enable_path": "/sys/class/hwmon/hwmon7/pwm1_enable",
                "pwm_max": 255,
            }
        ]
        with mock.patch.object(fanctl, "hwmon_pwm_fans", return_value=fans), mock.patch.object(
            fanctl, "release_skipped_pwms", return_value=[]
        ), mock.patch.object(
            fanctl, "_write_pwms_unprivileged", return_value=(True, "did not stick", "nostick")
        ), mock.patch.object(fanctl, "run_privileged") as priv:
            result = fanctl.apply_hwmon_percent(40)
        priv.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "direct")
        self.assertIn("did not stick", result.get("warning") or "")

    def test_apply_percent_pkexec_only_on_permission(self) -> None:
        fans = [
            {
                "pwm_path": "/sys/class/hwmon/hwmon7/pwm1",
                "pwm_enable_path": "/sys/class/hwmon/hwmon7/pwm1_enable",
                "pwm_max": 255,
            }
        ]
        with mock.patch.object(fanctl, "hwmon_pwm_fans", return_value=fans), mock.patch.object(
            fanctl, "release_skipped_pwms", return_value=[]
        ), mock.patch.object(
            fanctl, "_write_pwms_unprivileged", return_value=(False, "Permission denied", "permission")
        ), mock.patch.object(
            fanctl,
            "run_privileged",
            return_value={"ok": True, "count": 1, "wrote": fans},
        ) as priv:
            result = fanctl.apply_hwmon_percent(40)
        priv.assert_called_once()
        self.assertEqual(priv.call_args[0][0], ["apply-pwms"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "pkexec")

    def test_follow_tick_pkexecs_installed_helper_on_permission(self) -> None:
        curve = {"low_temp": 50, "high_temp": 60, "max_temp": 75, "polling_interval": 1}
        with mock.patch.object(fanctl, "list_hwmon", return_value=[]), mock.patch.object(
            fanctl, "group_cpu", return_value={"temp": 80.0}
        ), mock.patch.object(
            fanctl, "_write_pwms_unprivileged", return_value=(False, "Permission denied", "permission")
        ), mock.patch.object(fanctl, "hwmon_pwm_fans", return_value=[
            {"pwm_path": "/tmp/pwm1", "pwm_enable_path": "/tmp/pwm1_enable", "pwm_max": 255}
        ]), mock.patch.object(fanctl, "release_skipped_pwms", return_value=[]), mock.patch.object(
            fanctl,
            "run_privileged",
            return_value={"ok": True, "count": 1, "wrote": []},
        ) as priv:
            result, last = fanctl.follow_tick(None, min_delta=5, curve=curve)
        priv.assert_called_once()
        self.assertEqual(priv.call_args[0][0], ["apply-pwms"])
        self.assertTrue(result["ok"])
        self.assertEqual(last, 100)

    def test_follow_status_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["FANCTL_FOLLOW_STATE_DIR"] = tmp
            with mock.patch.dict(os.environ, env, clear=False):
                status = fanctl.follow_status()
        self.assertFalse(status["running"])
        self.assertTrue(status["ok"])

    def test_wait_for_follow_running_detects_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = os.environ.copy()
            env["FANCTL_FOLLOW_STATE_DIR"] = str(root)
            with mock.patch.dict(os.environ, env, clear=False):
                fanctl.follow_pid_file().write_text(str(os.getpid()), encoding="utf-8")
                fanctl.follow_state_file().write_text(
                    json.dumps({"percent": 25, "temp": 30.0}),
                    encoding="utf-8",
                )
                status = fanctl.wait_for_follow_running(timeout=0.5)
        self.assertTrue(status["ok"])
        self.assertTrue(status["running"])

    def test_follow_cli_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["FANCTL_FOLLOW_STATE_DIR"] = tmp
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "fanctl.py"), "follow", "status"],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        data = json.loads(proc.stdout)
        self.assertTrue(data["ok"])
        self.assertFalse(data["running"])


class PrivilegeBoundaryTests(unittest.TestCase):
    def test_polkit_binds_helper_not_python3(self) -> None:
        text = (ROOT / "polkit" / "io.github.anesturi.fan-control.policy").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("/usr/bin/python3", text)
        self.assertNotIn("auth_admin_keep", text)
        self.assertIn("/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py", text)
        self.assertIn("org.freedesktop.policykit.exec.argv1", text)
        self.assertIn(">yes<", text)
        self.assertIn("apply-pwms", text)

    def test_embedded_polkit_matches_repo_file(self) -> None:
        repo = (ROOT / "polkit" / "io.github.anesturi.fan-control.policy").read_text(
            encoding="utf-8"
        )
        self.assertEqual(privileged.POLKIT_POLICY_TEXT.strip(), repo.strip())

    def test_udev_does_not_chmod_world_writable(self) -> None:
        rules = ROOT / "udev" / "99-io.github.anesturi.fan-control.rules"
        text = rules.read_text(encoding="utf-8")
        self.assertNotIn("0666", text)
        self.assertNotRegex(text, r'(?i)MODE\s*=\s*"0?666"')
        self.assertNotRegex(text, r"(?m)^[^#]*chmod")
        self.assertFalse((ROOT / "scripts" / "pwm-unlock.sh").exists())
        self.assertFalse((ROOT / "scripts" / "fan-control-pwm-unlock.service").exists())

    def test_restore_pwm_uses_0644(self) -> None:
        source = (ROOT / "scripts" / "fanctl-privileged.py").read_text(encoding="utf-8")
        self.assertIn("os.chmod(path, 0o644)", source)
        self.assertNotIn("0o666", source)
        self.assertNotIn("chmod 666", source)

    def test_allowed_sysfs_rejects_arbitrary_paths(self) -> None:
        self.assertTrue(privileged.allowed_sysfs("/sys/class/hwmon/hwmon0/pwm1"))
        self.assertTrue(privileged.allowed_sysfs("/sys/class/hwmon/hwmon0/pwm1_enable"))
        self.assertTrue(
            privileged.allowed_sysfs("/sys/devices/platform/applesmc.768/fan1_output")
        )
        self.assertFalse(privileged.allowed_sysfs("/tmp/pwm1"))
        self.assertFalse(privileged.allowed_sysfs("/etc/shadow"))
        self.assertFalse(privileged.allowed_sysfs("/usr/bin/python3"))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "evil"
            target.write_text("x", encoding="utf-8")
            link = Path(tmp) / "pwm1"
            link.symlink_to(target)
            self.assertFalse(privileged.allowed_sysfs(str(link)))

    def test_trusted_helper_rejects_wrong_uid_and_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "fanctl-privileged.py"
            helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            helper.chmod(0o755)
            env_ok = {
                "FANCTL_INSTALLED_HELPER": str(helper),
                "FANCTL_HELPER_UID": str(os.getuid()),
            }
            with mock.patch.dict(os.environ, env_ok, clear=False):
                self.assertEqual(fanctl.trusted_installed_helper(), helper)
            with mock.patch.dict(
                os.environ,
                {
                    "FANCTL_INSTALLED_HELPER": str(helper),
                    "FANCTL_HELPER_UID": "0",
                },
                clear=False,
            ):
                if os.getuid() != 0:
                    self.assertIsNone(fanctl.trusted_installed_helper())
            helper.chmod(0o777)
            with mock.patch.dict(os.environ, env_ok, clear=False):
                self.assertIsNone(fanctl.trusted_installed_helper())
            helper.chmod(0o775)
            with mock.patch.dict(os.environ, env_ok, clear=False):
                self.assertIsNone(fanctl.trusted_installed_helper())

    def test_apply_pwms_without_trusted_helper_does_not_exec_python3(self) -> None:
        with mock.patch.object(fanctl, "trusted_installed_helper", return_value=None), mock.patch.object(
            fanctl, "which", return_value="/usr/bin/pkexec"
        ), mock.patch.object(fanctl.subprocess, "run") as run:
            result = fanctl.run_privileged(["apply-pwms"], '{"writes":[]}')
        run.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("helper", (result.get("error") or "").lower())

    def test_grant_bootstrap_uses_checkout_helper_not_keep_policy(self) -> None:
        with mock.patch.object(fanctl, "trusted_installed_helper", return_value=None), mock.patch.object(
            fanctl, "which", return_value="/usr/bin/pkexec"
        ), mock.patch.object(fanctl.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
            result = fanctl.run_privileged(["grant-access"])
        self.assertTrue(result["ok"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "/usr/bin/pkexec")
        self.assertEqual(argv[1], sys.executable)
        self.assertEqual(Path(argv[2]).name, "fanctl-privileged.py")
        self.assertEqual(argv[3], "grant-access")

    def test_apply_pwms_uses_installed_helper_without_python3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "fanctl-privileged.py"
            helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            helper.chmod(0o755)
            with mock.patch.dict(
                os.environ,
                {
                    "FANCTL_INSTALLED_HELPER": str(helper),
                    "FANCTL_HELPER_UID": str(os.getuid()),
                },
                clear=False,
            ), mock.patch.object(
                fanctl, "which", return_value="/usr/bin/pkexec"
            ), mock.patch.object(fanctl.subprocess, "run") as run:
                run.return_value = mock.Mock(
                    returncode=0, stdout='{"ok": true, "count": 0}', stderr=""
                )
                result = fanctl.run_privileged(["apply-pwms"], '{"writes":[]}')
            argv = run.call_args[0][0]
            self.assertTrue(result["ok"])
            self.assertEqual(argv[0], "/usr/bin/pkexec")
            self.assertEqual(argv[1], str(helper))
            self.assertEqual(argv[2], "apply-pwms")
            self.assertNotIn(sys.executable, argv)
            self.assertNotIn("/usr/bin/python3", argv)

    def test_modified_checkout_helper_is_not_used_once_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "fanctl-privileged.py"
            helper.write_text("#!/usr/bin/env python3\nprint('trusted')\n", encoding="utf-8")
            helper.chmod(0o755)
            checkout = fanctl.privileged_helper()
            with mock.patch.dict(
                os.environ,
                {
                    "FANCTL_INSTALLED_HELPER": str(helper),
                    "FANCTL_HELPER_UID": str(os.getuid()),
                },
                clear=False,
            ), mock.patch.object(
                fanctl, "which", return_value="/usr/bin/pkexec"
            ), mock.patch.object(fanctl.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
                fanctl.run_privileged(["apply-pwms"], '{"writes":[]}')
                fanctl.run_privileged(["grant-access"])
            for call in run.call_args_list:
                argv = call[0][0]
                self.assertNotIn(str(checkout), argv)
                self.assertNotIn(sys.executable, argv[1:])


class CliTests(unittest.TestCase):
    def test_snapshot_cli_demo(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "fanctl.py"), "snapshot", "--demo"],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(proc.stdout)
        self.assertTrue(data["ok"])
        self.assertTrue(data["demo"])


if __name__ == "__main__":
    unittest.main()
