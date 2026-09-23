# tornaider-helper

Trusted bootstrap for Tornaider PWM control. Pacman installs a **root-owned**
helper and PolicyKit policy; the Omarchy plugin checkout never chooses root
code, digests, executables, or install destinations.

## Install

From this directory:

```sh
makepkg -si
```

That places:

| Path | Role |
|------|------|
| `/usr/lib/io.github.anesturi.fan-control/fanctl-privileged.py` | Only executable PolicyKit will run |
| `/usr/share/polkit-1/actions/io.github.anesturi.fan-control.policy` | Bound to that helper + fixed `argv1` |

## After install

In the Tornaider panel, click **Allow passwordless control** once to clean up
any legacy world-writable udev/hooks from older plugin versions. PWM writes
then go through the package helper only.

## Remove

```sh
sudo pacman -R tornaider-helper
```
