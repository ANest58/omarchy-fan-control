# Tornaider — developer guide

High-level map of the plugin for learning and hacking. User-facing docs live in
[README.md](README.md).

**Plugin id:** `io.github.anesturi.fan-control` (internal folder name; display
name is **Tornaider**)

---

## Architecture in one diagram

```
Omarchy bar
    │
    ▼
BarWidget.qml ──loads──▶ Panel.qml
                              │
         ┌────────────────────┼────────────────────┐
         │                    │                    │
         ▼                    ▼                    ▼
    Model.js            fanctl.py            fanctl-privileged.py
  (pure JS logic)    (sensors + PWM)         (pkexec root writes)
         │                    │
         │              snapshot stream
         │              (JSON lines)
         └──────────▶ Panel displays temps, chart, presets, follow
```

The bar never talks to Python directly except through `Panel.qml`, which runs
`fanctl.py` as child processes.

---

## File roles

| File | Role |
|------|------|
| `manifest.json` | Omarchy plugin metadata (id, display name, bar settings schema) |
| `BarWidget.qml` | Bar pill button; loads `Panel.qml` in a hidden `Loader` |
| `Panel.qml` | Full UI: processes, keyboard shortcuts, device blocks, chart |
| `Model.js` | Pure functions shared by QML and Node tests (no Qt imports) |
| `scripts/fanctl.py` | Main backend: hwmon, snapshots, curves, PWM, follow daemon |
| `scripts/fanctl-privileged.py` | pkexec helper; grant copies it to `/usr/lib/io.github.anesturi.fan-control/` |
| `scripts/pwm-unlock.sh` | **Removed.** Older installs may still have a copy; grant/revoke delete it |
| `scripts/fan-control-pwm-unlock.service` | **Removed.** Same cleanup as the unlock script |
| `etc/mbpfan.conf` | Bundled default curve written via validated JSON, not a file copy |
| `udev/*.rules` | Comment-only leftover; grant/revoke delete any older chmod-666 copy |
| `polkit/*.policy` | Bound to the installed helper path + argv1 (no `/usr/bin/python3`) |
| `tests/fanctl_test.py` | Python unit tests with fake hwmon trees |
| `tests/model.test.js` | Node tests for `Model.js` |
| `docs/screenshot.png` | README panel screenshot |

---

## `BarWidget.qml` — bar entry point

- Sets `moduleName` to the plugin id (required by Omarchy).
- `Loader` loads `Panel.qml` once and wires `bar`, `settings`, `anchorItem`.
- `WidgetButton` shows `panel.label` (from latest snapshot) and forwards
  click → toggle panel, middle-click → refresh.

**Lifecycle:** `open` / `close` / `toggle` delegate to the loaded panel.

---

## `Panel.qml` — UI shell

### Processes

| Process | Command | Purpose |
|---------|---------|---------|
| `stream` | `fanctl.py snapshot --interval N` | Long-lived JSON line stream |
| `oneshot` | `fanctl.py snapshot` | Single refresh after apply actions |
| `actionProc` | `fanctl.py apply …` / `follow start` / etc. | Mutating commands |

Each stdout line is passed to `Model.parseSnapshot()` → updates `snapshot`.

### Timers

- **`tempSampler`** — every 3 s, appends CPU/GPU temp to `cpuTempHistory` /
  `gpuTempHistory` for the dot chart (matches follow interval).

### Key UI components (defined inline)

- **`DeviceBlock`** — device title, usage `MetricBar`, `TempDotBar`, fan rows.
- **`MetricBar`** — horizontal CPU/GPU usage bar.
- **`TempDotBar`** — 60 s window, 3 s columns, vertical grid ticks, stacked dots.

### Keyboard (`PanelKeyCatcher`)

`1`/`2`/`3` presets, `i` install config, `f` follow toggle, Esc close.

---

## `Model.js` — data layer

Pure JavaScript so the same file loads in Quickshell and `node --test`.

| Function | Parses / computes |
|----------|-------------------|
| `parseSnapshot(raw)` | JSON string → snapshot object (or `null`) |
| `pillText` / `tooltipText` | Bar pill and hover text |
| `tempText` / `rpmText` / `percentText` | Formatted display strings |
| `appendTempSample` | Rolling 60 s temp history buffer |
| `tempPlotRange` | Min/max for dot chart scaling |
| `tempPlotTicks` | X-axis grid every 3 s (major labels every 15 s: -60s … now) |
| `dotBarDots` | Bucket into 3 s slots, forward-fill gaps, stacked dot columns |
| `tempLevel` | cool / normal / warm / hot color bucket |
| `fanRows` / `fanStatus` | Per-fan RPM + PWM display |
| `applyCurveToSnapshot` | Merge apply response into live snapshot |
| `applyFollowToSnapshot` | Merge follow start/stop into snapshot |
| `followStatusText` | Panel status line while follow is active |

Constants: `TEMP_HISTORY_WINDOW_MS = 60000`, `TEMP_SAMPLE_INTERVAL_MS = 3000`.

---

## `fanctl.py` — backend (chunked)

### Config parsing

| Function | Purpose |
|----------|---------|
| `parse_mbpfan_conf(text)` | Parse `low_temp`, `high_temp`, `max_temp`, `polling_interval` |
| `load_config()` | Resolve system `/etc/mbpfan.conf` (+ bundled fallback) |
| `load_display_config()` | Prefer user `~/.config/mbpfan/mbpfan.conf` for UI + follow |
| `render_mbpfan_conf` / `write_user_config` | Write user curve file |

### Sensor discovery

| Function | Purpose |
|----------|---------|
| `list_hwmon()` | Walk `/sys/class/hwmon/*` (or `FANCTL_HWMON_ROOT` in tests) |
| `temps_for` / `fans_for` | Read temp* and fan* sysfs nodes |
| `collect_case_fans` | Motherboard PWM fans (NCT6687, etc.) |
| `group_cpu` / `group_gpu` | Merge chips into CPU/GPU device dicts |
| `nvidia_gpu` / `gpu_from_drm` | NVIDIA and DRM GPU fallbacks |
| `cpu_usage_percent` / `gpu_usage_percent` | `/proc/stat` and vendor usage |
| `detect_backend` | Classify: hwmon, mbpfan, applesmc, monitor, demo |

### Snapshot assembly

| Function | Purpose |
|----------|---------|
| `snapshot()` | Main entry: temps, fans, backend, presets, follow status |
| `demo_snapshot()` / `empty_snapshot()` | Demo and no-sensor states |
| `format_pill()` / `tooltip_text()` | Bar strings |
| `attach_follow()` | Adds `follow` key from `follow_status()` |
| `presets_payload()` | Quiet/Balanced/Cool buttons with `active` flag |

### PWM control (desktop)

| Function | Purpose |
|----------|---------|
| `curve_pwm_percent(temp, curve)` | Map CPU °C → duty 25–100% |
| `hwmon_pwm_fans()` | List controllable PWM channels (optional: include idle fans) |
| `apply_hwmon_percent(percent)` | Direct write if possible; else pkexec the installed helper (`apply-pwms`) |
| `trusted_installed_helper()` | Root-owned, non-group/world-writable helper, or None |
| `run_privileged()` | pkexec installed helper; checkout `python3` only for grant/revoke/config bootstrap |
| `grant_access()` / `revoke_access()` | Install or remove helper, polkit, leftover udev/hooks |
| `_write_pwms_unprivileged()` | Enable manual mode + write value; verify stick |
| `release_skipped_pwms()` | Return pump headers (PWM index 2) to EC auto |
| `release_controlled_pwms()` | Return all case fans to auto on follow stop |
| `apply_curve()` | Save curve, optional `/etc` install, apply preset duty or curve PWM |
| `apply_hwmon_pwms()` | Preset fixed duty or curve-derived duty |

Constants: `PRESET_DUTY` (18/45/100%), `SKIP_PWM_INDEXES = {"2"}` (AIO pump).

### Follow curve daemon

| Function | Purpose |
|----------|---------|
| `follow_tick()` | Read CPU temp → curve duty; hysteresis if Δ &lt; 5% |
| `follow_loop()` | 3 s loop under file lock; SIGTERM cleanup |
| `follow_start()` / `follow_stop()` | systemd user unit or detached process |
| `wait_for_follow_running()` | Poll until PID file appears |
| `follow_status()` | JSON for CLI and snapshot |
| `ensure_follow_service()` | Write `~/.config/systemd/user/fan-control-follow.service` |

State files: `~/.local/state/omarchy/fan-control/` (`follow.pid`, `follow-state.json`).

### CLI (`main()`)

Subcommands: `snapshot`, `apply`, `follow run|start|stop|status|once`,
`grant-access`, `revoke-access`, `install-config`.

---

## `fanctl-privileged.py` — privileged helper

Runs under `pkexec`. Must be root-owned at
`/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py`. Actions:

- `grant-access` — install this helper + polkit; restore PWM to 0644; remove legacy udev/hooks
- `revoke-access` — reverse grant (helper, polkit, leftover udev/unit/hook, PWM 0644)
- `apply-config` / `install-config` — write `/etc/mbpfan.conf` from a validated curve (no user-path copy)
- `apply-pwms` — root write of validated hwmon/applesmc PWM paths only

Polkit actions (repo file must match `POLKIT_POLICY_TEXT` in the helper):

| Action id | argv1 | Active session |
|-----------|-------|----------------|
| `io.github.anesturi.fan-control.apply-pwms` | `apply-pwms` | `allow_active=yes` (no prompt) |
| `io.github.anesturi.fan-control.grant` | `grant-access` | `auth_admin` |
| `io.github.anesturi.fan-control.revoke` | `revoke-access` | `auth_admin` |
| `io.github.anesturi.fan-control.install-config` | `install-config` | `auth_admin` |
| `io.github.anesturi.fan-control.apply-config` | `apply-config` | `auth_admin` |

Do not add `/usr/bin/python3` or `auth_admin_keep`. Do not chmod PWM `0666`.

`allowed_sysfs()` resolves the path first, then allows only `hwmonN/pwm*` under
`/sys/class/hwmon` or `/sys/devices`, plus Apple SMC `fanN_output` /
`fanN_manual`. Symlinks out of sysfs are rejected.

---

## Privilege boundary

```
Panel / follow daemon
        │
        ▼
   fanctl.py
        │
        ├── PWM 0644 readable? ──write sysfs──► done (rare after grant)
        │
        └── run_privileged(argv)
                 │
                 ├── trusted helper exists (uid 0, not group/world-writable)
                 │         pkexec /usr/lib/.../fanctl-privileged.py argv1
                 │
                 └── else only grant/revoke/install-config/apply-config
                           pkexec python3 <checkout helper> argv1
                           (default python3 policy; password every time)
```

`apply-pwms` never falls back to checkout `python3`. If the helper is missing
or world-writable, PWM apply returns an error and asks the user to grant.

First grant copies `__file__` (the running helper) into the root path, writes
the embedded polkit XML, verifies owner/mode/content, restores PWM to `0644`,
and deletes:

- `/etc/udev/rules.d/99-io.github.anesturi.fan-control.rules`
- `/usr/lib/systemd/system-sleep/io.github.anesturi.fan-control`
- `/etc/systemd/system/io.github.anesturi.fan-control-pwm.service`
- `/usr/lib/io.github.anesturi.fan-control/pwm-unlock.sh`

After that, only the installed helper is used.

---

## Data flow examples

### Open panel → see temps

1. `Panel` starts `fanctl.py snapshot --interval 2`.
2. Each line → `Model.parseSnapshot` → `snapshot` property updates.
3. QML bindings refresh hero temp, usage bars, fan rows, follow status.

### Press `2` (Balanced)

1. `actionProc` runs `fanctl.py apply --preset balanced --user-only`.
2. `apply_curve` stops follow if running, writes `~/.config/mbpfan/mbpfan.conf`,
   sets PWM to 45%.
3. JSON response → `applyCurveToSnapshot` updates panel thresholds immediately.

### Press `f` (Follow curve)

1. `actionProc` runs `follow start`.
2. systemd (or detached) runs `follow run` → `follow_loop` every 3 s.
3. `follow_tick` reads CPU temp, computes duty, writes PWM when change ≥ 5%.
4. Next `snapshot` line includes `follow.running: true` and live `percent`.

### Click **Allow passwordless control**

1. `actionProc` runs `fanctl.py grant-access`.
2. `run_privileged(["grant-access"])` pkexecs checkout `python3` the first time,
   then the installed helper on later grants.
3. Helper installs itself + polkit, restores PWM `0644`, removes legacy udev.
4. JSON includes `helper` and `polkit` → panel shows “Passwordless fan control enabled”.
5. Later preset/follow PWM writes: `pkexec <installed helper> apply-pwms`.

---

## Testing

### Python (`tests/fanctl_test.py`)

Uses `FANCTL_HWMON_ROOT` to build fake sysfs trees under `/tmp`. Covers config
parsing, snapshots, PWM apply, follow hysteresis, CLI smoke tests, and
`PrivilegeBoundaryTests`:

- polkit has no `/usr/bin/python3` and no `auth_admin_keep`
- embedded `POLKIT_POLICY_TEXT` matches `polkit/*.policy`
- udev file has no `MODE=0666`; unlock script/unit are gone
- `trusted_installed_helper()` rejects wrong uid and group/world-writable files
- `apply-pwms` without a trusted helper does not exec python3
- once a helper is installed, checkout `fanctl-privileged.py` is not on the argv

### Node (`tests/model.test.js`)

Tests `Model.js` without Qt: parsing, chart dots, curve merge, follow text.

Run both before publishing:

```sh
python3 tests/fanctl_test.py
node --test tests/model.test.js
```

---

## Environment variables (dev / test)

| Variable | Effect |
|----------|--------|
| `FANCTL_HWMON_ROOT` | Replace `/sys/class/hwmon` root |
| `FANCTL_DRM_ROOT` | Replace DRM sysfs root |
| `FANCTL_CONFIG_PATHS` | Colon-separated mbpfan.conf search paths |
| `FANCTL_DEMO=1` | Fake sensor snapshot |
| `FANCTL_FOLLOW_STATE_DIR` | Isolated follow PID/state directory |
| `FANCTL_INSTALLED_HELPER` | Override helper path (tests) |
| `FANCTL_HELPER_UID` | Expected helper owner uid (tests; default 0) |

---

## Common change recipes

| Goal | Where to edit |
|------|----------------|
| Change preset duties | `PRESET_DUTY` in `fanctl.py` |
| Change curve math | `curve_pwm_percent()` |
| Change chart window / sample rate | `Model.js` constants + `Panel` timer |
| Add a new CLI command | `main()` in `fanctl.py` + `Panel.runAction` |
| New bar setting | `manifest.json` `schema` + read via `setting()` in `Panel` |
| Skip another pump PWM | `SKIP_PWM_INDEXES` |
| Change polkit actions | `polkit/*.policy` **and** `POLKIT_POLICY_TEXT` in the helper (keep them identical) |
| Change allowed PWM paths | `allowed_sysfs()` in `fanctl-privileged.py` |

---

## Validate and reload

```sh
omarchy plugin validate "$PLUGIN_DIR"
/usr/lib/qt6/bin/qmllint -I "$OMARCHY_PATH/shell" BarWidget.qml Panel.qml
omarchy-shell shell summon io.github.anesturi.fan-control '{}'
omarchy-shell shell hide io.github.anesturi.fan-control
omarchy restart shell
```
