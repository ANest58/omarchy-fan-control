#!/bin/sh
# Re-chmod motherboard PWM sysfs nodes so Follow curve never needs pkexec.
# Installed by grant-access as a boot oneshot and a systemd sleep hook.
# Sleep hook args: pre|post [suspend|hibernate|hybrid-sleep].
if [ "${1:-}" = pre ]; then
  exit 0
fi
for f in /sys/class/hwmon/hwmon*/pwm[0-9] /sys/class/hwmon/hwmon*/pwm[0-9]_enable; do
  if [ -e "$f" ]; then
    chmod a+rw "$f" 2>/dev/null || true
  fi
done
