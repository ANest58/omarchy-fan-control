# Tornaider

CPU and GPU temperatures, fan RPM, Quiet / Balanced / Cool presets, and
**Follow curve** continuous PWM for the Omarchy bar.

Made with [Cursor](https://cursor.com).

Plugin id (unchanged for upgrades): `io.github.anesturi.fan-control`

![Tornaider panel with CPU and GPU temps, fan RPM, and Quiet / Balanced / Cool presets](docs/screenshot.png)

## Install

```sh
omarchy plugin add https://github.com/ANest58/omarchy-fan-control.git --enable
omarchy bar move io.github.anesturi.fan-control --section right
omarchy restart shell
```

On first install, open the panel and click **Allow passwordless control** if
motherboard PWM nodes are root-only (desktop PCs). Apple hardware may also need
`/etc/mbpfan.conf` — use **Install /etc/mbpfan.conf** in the panel or key `i`.

If you granted access on an older plugin copy (udev `chmod 666` / python3
polkit), click **Allow passwordless control** again after pulling `master`.
That replaces the old policy, restores PWM nodes to `0644`, and installs the
root-owned helper.

## Usage

| Action | How |
|--------|-----|
| Open / close panel | Click the bar pill |
| Force sensor refresh | Middle-click the pill |
| Quiet / Balanced / Cool | Keys `1` / `2` / `3` or preset buttons |
| Install system mbpfan config | Key `i` |
| Toggle follow curve | Key `f` or **Follow curve** button |
| Close panel | **Esc** |

The bar pill shows live CPU/GPU temps (e.g. `54° 61°`). The panel adds usage
bars, a 60-second temperature dot chart (sampled every **3 seconds**), fan RPM,
and curve controls.

### Presets vs Follow curve

| Mode | What it does | Best for |
|------|----------------|----------|
| **Presets** (`1`/`2`/`3`) | Saves a temp curve *and* sets a **fixed PWM duty** immediately | Quick “make it quiet / loud now” |
| **Follow curve** (`f`) | Background loop adjusts PWM **every 3 s** from the saved curve as CPU temp changes | Day-to-day desktop cooling |

Presets and follow are mutually exclusive: applying a preset **stops** follow mode.

### Follow curve — step by step

1. **Unlock PWM once** (desktop): panel → **Allow passwordless control**  
   Or: `python3 scripts/fanctl.py grant-access`

2. **Pick a starting curve** (optional): click **Balanced** or edit thresholds in  
   `~/.config/mbpfan/mbpfan.conf` (`low_temp`, `high_temp`, `max_temp`).

3. **Start follow**  
   - Panel: **Follow curve** (or key `f`)  
   - CLI:
     ```sh
     PLUGIN="$HOME/.config/omarchy/plugins/io.github.anesturi.fan-control"
     python3 "$PLUGIN/scripts/fanctl.py" follow start
     ```

4. **Confirm it is running**
   ```sh
   python3 "$PLUGIN/scripts/fanctl.py" follow status
   # or
   cat ~/.local/state/omarchy/fan-control/follow-state.json
   ```
   Example: `{"running": true, "temp": 54, "percent": 41, "skipped": false, ...}`  
   - `percent` — target fan duty from the curve  
   - `skipped: true` — hysteresis held the last duty (change &lt; 5%)

5. **Stop follow** (returns case fans to firmware auto mode)  
   - Panel: **Stop follow curve**  
   - CLI: `python3 "$PLUGIN/scripts/fanctl.py" follow stop`  
   - Systemd: `systemctl --user disable --now fan-control-follow.service`

`follow start` enables a user systemd unit when available
(`~/.config/systemd/user/fan-control-follow.service`); otherwise it spawns a
detached process. PWM index **2** (AIO pump on many MSI boards) is never driven.

Follow writes PWM through the installed helper when sysfs is root-only. It
does not chmod fan nodes for your user, and it does not prompt after grant
(`apply-pwms` is `allow_active=yes` for the active session).

### Curve reference (Balanced preset)

| Threshold | Fan duty |
|-----------|----------|
| ≤ `low_temp` (63°C) | 25% |
| `low_temp` → `high_temp` | 25% → 75% |
| `high_temp` → `max_temp` | 75% → 100% |
| ≥ `max_temp` (86°C) | 100% |

## Configure

```sh
omarchy bar move io.github.anesturi.fan-control --section right
```

Bar widget setting **`refreshSeconds`** (1–10, default 2) controls how often
the panel refreshes sensor JSON. Follow curve and the temp chart both use a
**3 second** interval independently.

Edit the saved curve:

```sh
${EDITOR:-nano} ~/.config/mbpfan/mbpfan.conf
```

### CLI

From the plugin directory (`$PLUGIN` as above):

| Command | What it does |
|---------|----------------|
| `python3 scripts/fanctl.py snapshot` | One JSON sensor snapshot (`--demo` for fake data) |
| `python3 scripts/fanctl.py apply --preset balanced --user-only` | Save curve and set a fixed PWM duty |
| `python3 scripts/fanctl.py follow start` / `stop` / `status` | Follow curve daemon |
| `python3 scripts/fanctl.py grant-access` | Install root-owned helper + polkit (password once) |
| `python3 scripts/fanctl.py revoke-access` | Remove helper, polkit, leftover udev/hooks |
| `python3 scripts/fanctl.py install-config` | Write a validated curve to `/etc/mbpfan.conf` |

## Dependencies and privileges

| Component | Purpose |
|-----------|---------|
| `python3` (stdlib) | Sensor collector and PWM helper (`scripts/fanctl.py`) |
| `lm_sensors` / hwmon | CPU temp and motherboard fan RPM on desktop PCs |
| `nvidia-smi` (optional) | NVIDIA GPU temperature and fan percent |
| `nvidia-settings` (optional) | NVIDIA fan RPM when Coolbits is enabled |
| `mbpfan` (optional) | Apple SMC fan daemon on Intel MacBooks |
| `t2fanrd` (optional) | T2 Mac fan daemon |
| `pkexec` / polkit | One-time install of the root-owned helper and `/etc/mbpfan.conf` |

**Allow passwordless control** installs a root-owned helper at
`/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py` and a PolicyKit
policy bound to that exact file. PWM sysfs stays `0644` (root-only). Fan duty
is applied only by that helper — not by chmodding nodes world-writable, and
not by authorizing `/usr/bin/python3`. Follow curve uses the same helper
(`allow_active=yes` for `apply-pwms`, so it does not prompt again).

The first grant may ask PolicyKit to run the checkout helper through
`python3` once (default python3 policy, password every time). After that,
only the installed helper is used. Re-run grant after upgrading from an older
plugin copy so leftover `chmod 666` udev rules and `auth_admin_keep` python3
policies are removed.

Bundled files used on request:

- `polkit/io.github.anesturi.fan-control.policy` — actions bound to the installed helper
- `etc/mbpfan.conf` — default temperature curve for `mbpfan`
- `udev/99-io.github.anesturi.fan-control.rules` — kept in the repo as a comment-only
  leftover; **not installed**. Older copies that `chmod 666` PWM nodes are
  deleted by grant/revoke.
- `scripts/pwm-unlock.sh` and `scripts/fan-control-pwm-unlock.service` — **removed**.
  Grant/revoke still delete any copies that an older plugin version installed.

### What grant-access installs

| Path | Mode | Role |
|------|------|------|
| `/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py` | `0755`, uid 0 | Only executable PolicyKit will run |
| `/usr/share/polkit-1/actions/io.github.anesturi.fan-control.policy` | `0644`, uid 0 | Bound to that helper + `argv1` |

PWM sysfs stays `0644`. A second local account cannot write PWM nodes; only
the helper (active session, `apply-pwms`) can.

Verify after grant:

```sh
ls -l /usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py
stat -c '%a %U' /sys/class/hwmon/hwmon*/pwm[0-9]
# expect 644 root — not 666
test ! -e /etc/udev/rules.d/99-io.github.anesturi.fan-control.rules && echo "no udev 666 rule"
```

## Desktop motherboard fans (NCT6687)

`coretemp` reports CPU temperature only. Case and CPU header RPM come from a
Super I/O chip (for example NCT6687). Base setup:

```sh
sudo pacman -S lm_sensors
sudo sensors-detect
sudo systemctl enable --now lm_sensors.service
```

On MSI boards where stock `nct6683` ignores PWM writes, install
`nct6687d-dkms-git` from the AUR and blacklist `nct6683`.

| Preset   | Fixed duty | Temp curve (low / high / max °C) |
|----------|------------|-----------------------------------|
| Quiet    | 18%        | 70 / 78 / 90                      |
| Balanced | 45%        | 63 / 66 / 86                      |
| Cool     | 100%       | 50 / 60 / 75                      |

## MacBook / mbpfan

```sh
yay -S mbpfan-git
sudo cp etc/mbpfan.conf /etc/mbpfan.conf
sudo systemctl enable --now mbpfan
```

T2 Macs: use `t2fanrd` instead (`sudo systemctl enable --now t2fanrd`).

## Development

See **[DEV.md](DEV.md)** for a guided tour of the codebase.

Quick checks:

```sh
PLUGIN_DIR="$HOME/.config/omarchy/plugins/io.github.anesturi.fan-control"
omarchy plugin validate "$PLUGIN_DIR"
cd "$PLUGIN_DIR" && python3 tests/fanctl_test.py && node --test tests/model.test.js
omarchy-shell shell rescanPlugins   # after QML/Python edits
```

## Remove

```sh
omarchy plugin remove io.github.anesturi.fan-control --yes
```

Optional cleanup:

| What | How to remove |
|------|----------------|
| Privileged helper + polkit + leftover udev | `python3 scripts/fanctl.py revoke-access` (password once) |
| Follow service | `systemctl --user disable --now fan-control-follow.service` |
| Follow state | `rm -rf ~/.local/state/omarchy/fan-control/` |
| User curve copy | `rm -rf ~/.config/mbpfan/` |
| System mbpfan config | `sudo rm /etc/mbpfan.conf` |
| PWM udev rule (legacy) | `sudo rm /etc/udev/rules.d/99-io.github.anesturi.fan-control.rules` |
| PWM boot/resume unlock (legacy) | `sudo systemctl disable --now io.github.anesturi.fan-control-pwm.service; sudo rm -f /etc/systemd/system/io.github.anesturi.fan-control-pwm.service /usr/lib/systemd/system-sleep/io.github.anesturi.fan-control /usr/lib/io.github.anesturi.fan-control/pwm-unlock.sh` |
| Polkit policy (legacy / extra) | `sudo rm /usr/share/polkit-1/actions/io.github.anesturi.fan-control.policy` |

`revoke-access` is the preferred cleanup: it removes
`/usr/lib/io.github.anesturi.fan-control/`, the polkit policy, any older
chmod-666 udev rule, the boot/resume unlock unit and sleep hook, and restores
PWM nodes to `0644`. The extra `sudo rm` rows above are only needed if revoke
did not run or an older install left files behind.

## Credits

Tornaider was built with [Cursor](https://cursor.com).

| Project | Credit |
|---------|--------|
| [Cursor](https://cursor.com) | AI-assisted development of this plugin |
| [Omarchy](https://omarchy.org) | Linux desktop and bar this widget runs on ([source](https://github.com/basecamp/omarchy)) |
| [Quickshell](https://quickshell.org) | QML shell runtime used by Omarchy |
| [linux-on-mac/mbpfan](https://github.com/linux-on-mac/mbpfan) | Bundled `etc/mbpfan.conf` temperature defaults |
| Linux hwmon / [lm-sensors](https://github.com/lm-sensors/lm-sensors) | CPU temps and motherboard PWM sysfs |
| [nct6687d](https://github.com/Fred78290/nct6687d) | PWM writes on many MSI NCT6687 boards |
| [T2FanRD](https://github.com/GnomedDev/T2FanRD) | Optional T2 Mac fan daemon |
| NVIDIA `nvidia-smi` | Optional GPU temperature and fan percent |

## License

MIT. See [LICENSE](LICENSE). Third-party projects listed above keep their own licenses;
this plugin does not relicense them.
