# Dell Fan Control Daemon

A local Python daemon that controls Dell server fan speeds based on real-time temperature readings from Linux hwmon sysfs sensors. Runs directly on the target machine — no SSH, no remote dependencies.

## Overview

The daemon reads temperature sensors from `/sys/devices/platform/dell_smm_hwmon/hwmon/hwmon4/` and writes PWM values to control fan speeds. It uses a configurable fan curve that maps temperature ranges to PWM duty cycles (0–255), providing smooth, responsive cooling without relying on the BIOS fan profile.

### Why this exists

Dell server BIOS fan curves are often too conservative (fans spin up late, then aggressively) or too aggressive (loud at idle). This daemon gives you precise control over how fans respond to actual temperatures, running as a proper systemd service with graceful shutdown that restores BIOS control.

## Features

- **Local execution** — reads/writes hwmon sysfs directly (no SSH overhead)
- **Configurable fan curve** — three-point linear ramp: idle → target → critical
- **Multi-sensor blending** — weight CPU, ambient, and NVMe drive temperatures together
- **Systemd integration** — installs as a service with auto-restart and journal logging
- **Graceful shutdown** — on SIGTERM/SIGINT, restores fans to BIOS auto mode (`pwm*_enable=2`)
- **PID file** — prevents duplicate instances
- **YAML config** — optional config file for per-server tuning
- **Dry-run mode** — test without writing anything

## Requirements

- Linux server with Dell SMM hwmon support (`dell-smm-hwmon` kernel module)
- Python 3.9+ (stdlib only — no pip installs required for basic operation)
- `pyyaml` (optional, for YAML config file support: `pip install pyyaml`)
- **root privileges** — writing to hwmon PWM files requires `sudo`

## Quick Start

```bash
# Test without writing anything
sudo python3 dell_fan_control.py --discover
sudo python3 dell_fan_control.py --once --dry-run

# Run one cycle with actual writes
sudo python3 dell_fan_control.py --once

# Start as daemon
sudo python3 dell_fan_control.py --daemon
```

## Installation (systemd)

```bash
# 1. Copy the script and service file
sudo cp dell_fan_control.py /opt/
sudo cp dell_fan_control.service /etc/systemd/system/

# 2. (Optional) copy YAML config
sudo cp dell_fan_control.yaml /etc/dell-fan-control.yaml

# 3. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now dell_fan_control

# 4. Monitor
sudo journalctl -u dell_fan_control -f
```

### systemd commands

```bash
sudo systemctl status dell_fan_control    # check status
sudo systemctl restart dell_fan_control   # apply config changes
sudo systemctl stop dell_fan_control      # stops and restores BIOS fan control
sudo journalctl -u dell_fan_control -f    # follow logs
```

## Command-Line Reference

```
positional arguments:
  --temp-sensor, -t     Temperature sensor to monitor. Repeat for multiple.
                        Built-in: temp1 (Ambient), temp2 (CPU), nvme0, nvme1
                        Or any /sys/.../temp*_input path. Default: temp2
  --fans, -f            Fans to control. Default: fan1 fan2
  --disable-fan         Fan(s) to leave untouched

fan curve:
  --idle-temp           °C below which idle PWM applies (default: 30)
  --target-temp         °C at which target PWM is reached (default: 60)
  --critical-temp       °C above which fans go to max (default: 85)
  --pwm-idle            PWM at idle, 0-255 (default: 64, ~25%)
  --pwm-target          PWM at target, 0-255 (default: 192, ~75%)

operation:
  --interval, -i        Polling interval in seconds (default: 10)
  --dry-run             Show what would happen without writing
  --once                Run one cycle and exit
  --daemon              Run as continuous daemon
  --discover, -d        List available sensors and exit
  --config              Path to YAML config file
  --log-file            Log file path (default: /var/log/dell_fan_control.log)
  --verbose, -v         Enable debug logging
```

## Fan Curve

The fan curve is a piecewise linear mapping from temperature to PWM duty cycle:

```
PWM
255 |                                    ██████████ critical
    |                                ███
 192 |                          ███  target (~75%)
    |                      ███
    |                  ███
  64 |            ███  idle (~25%)
    |        ███
      +----|-----|---------|----------------→ °C
         idle  target   critical
          30°     60°       85°
```

- **Below idle (30°C)** → PWM stays at `pwm_idle` (64 / ~25%)
- **Idle to target (30–60°C)** → linear ramp from `pwm_idle` to `pwm_target`
- **Target to critical (60–85°C)** → linear ramp from `pwm_target` to 255 (100%)
- **Above critical (85°C)** → full speed

## Sensors

| Name | Path | Description |
|------|------|-------------|
| `temp1` | `hwmon4/temp1_input` | Ambient temperature |
| `temp2` | `hwmon4/temp2_input` | CPU temperature |
| `nvme0` | `hwmon1/temp1_input` | NVMe Drive 0 |
| `nvme1` | `hwmon2/temp1_input` | NVMe Drive 1 |

You can also specify any `/sys/class/hwmon/.../temp*_input` path directly. When multiple sensors are configured, the daemon computes a weighted average.

### Example: CPU + NVMe blend

```bash
sudo python3 dell_fan_control.py --once \
    --temp-sensor temp2 --temp-sensor nvme0
```

## YAML Configuration

Copy `dell_fan_control.yaml` to `/etc/dell-fan-control.yaml` and run with `--config /etc/dell-fan-control.yaml`. CLI flags override config file values.

```yaml
curve:
  idle_temp: 30
  target_temp: 60
  critical_temp: 85
  pwm_idle: 64
  pwm_target: 192

interval: 10
log_file: /var/log/dell_fan_control.log
```

## Safety

- **Root required** — the script checks `os.geteuid() != 0` and refuses to run without privileges (unless `--dry-run`).
- **PWM clamped** — all PWM values are clamped to 0–255.
- **Graceful restore** — on shutdown, fans return to BIOS auto mode (`pwm*_enable=2`). If the daemon crashes without sending SIGTERM, fans remain at their last manual setting until the next boot or manual restore.
- **Critical threshold** — above 85°C (configurable), all fans go to 100% regardless of curve shape.

## File Layout

```
dell_fan_control.py      # Main daemon script
dell_fan_control.service # systemd unit file
dell_fan_control.yaml    # Sample YAML configuration
README.md                # This file
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Permission denied` on hwmon writes | Run with `sudo` |
| Sensor shows `NOT FOUND` | Run `--discover` to find correct paths; hwmon indices may differ per system |
| Fans not responding | Check `pwm*_enable` is `1` (manual). The daemon sets this on first cycle. |
| Daemon won't start via systemd | Check `journalctl -u dell_fan_control` and verify the script path in the service file |
| Want to restore BIOS control | `sudo systemctl stop dell_fan_control` or manually write `echo 2 > /sys/devices/platform/dell_smm_hwmon/hwmon/hwmon4/pwm1_enable` |

## License

MIT