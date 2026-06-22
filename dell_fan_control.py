#!/usr/bin/env python3
"""
dell_fan_control.py — Local daemon that controls Dell server fan speeds
based on hwmon temperature sensors. Runs directly on the target machine.

Reads temperature sensors from /sys/devices/platform/dell_smm_hwmon/hwmon/hwmon4/
and sets PWM fan speeds based on a configurable curve.

Fan PWM values range 0-255. Temperature values from hwmon are in millidegrees
Celsius (e.g. 30000 = 30.0 °C).

Usage:
    # Run as foreground daemon (use with systemd)
    sudo python3 dell_fan_control.py --daemon

    # Quick test — one cycle, then exit
    sudo python3 dell_fan_control.py --once

    # Dry-run: show what would happen without writing
    sudo python3 dell_fan_control.py --once --dry-run

    # Custom curve and interval
    sudo python3 dell_fan_control.py --daemon \\
        --idle-temp 25 --target-temp 55 --critical-temp 80 \\
        --pwm-idle 48 --pwm-target 160 --interval 10

    # Use config file
    sudo python3 dell_fan_control.py --config /etc/dell-fan-control.yaml

Systemd installation:
    cp dell_fan_control.py /opt/
    cp dell_fan_control.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now dell_fan_control
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HWMON4 = "/sys/devices/platform/dell_smm_hwmon/hwmon/hwmon4"

PWM_MIN = 0
PWM_MAX = 255

SAFETY_CRITICAL_TEMP_C = 85.0   # Force max fans above this temperature
SAFETY_IDLE_TEMP_C = 30.0       # Minimum fan speed below this temperature

LOG_FILE = "/var/log/dell_fan_control.log"
PID_FILE = "/var/run/dell_fan_control.pid"

DEFAULT_PWM_PATHS = {
    "fan1": Path(HWMON4) / "pwm1",        # Processor Fan
    "fan2": Path(HWMON4) / "pwm2",        # Motherboard Fan
}

DEFAULT_ENABLE_PATHS = {
    "fan1": Path(HWMON4) / "pwm1_enable",
    "fan2": Path(HWMON4) / "pwm2_enable",
}

DEFAULT_TEMP_PATHS = {
    "temp1": (Path(HWMON4) / "temp1_input", "Ambient"),
    "temp2": (Path(HWMON4) / "temp2_input", "CPU"),
}

# NVMe drive temperature sensors
NVME_TEMP_PATHS: dict[str, tuple[Path, str]] = {
    "nvme0": (Path("/sys/class/hwmon/hwmon1/temp1_input"), "NVMe0 SSD"),
    "nvme1": (Path("/sys/class/hwmon/hwmon2/temp1_input"), "NVMe1 SSD"),
}

ALL_TEMP_PATHS: dict[str, tuple[Path, str]] = {
    **DEFAULT_TEMP_PATHS,
    **NVME_TEMP_PATHS,
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FanCurve:
    """Linear fan speed curve defined by temperature breakpoints.

    Below idle_temp   → pwm_idle
    idle → target     → linear ramp from pwm_idle to pwm_target
    target → critical → linear ramp from pwm_target to PWM_MAX (255)
    Above critical    → PWM_MAX (255)

    All temperatures in degrees Celsius. PWM values 0-255.
    """

    idle_temp: float = SAFETY_IDLE_TEMP_C
    target_temp: float = 60.0
    critical_temp: float = SAFETY_CRITICAL_TEMP_C
    pwm_idle: int = 64                      # ~25% at idle
    pwm_target: int = 192                   # ~75% at target temp

    def pwm_for_temperature(self, temp_c: float) -> int:
        """Return the PWM value (0-255) for a given temperature in °C."""
        if temp_c <= self.idle_temp:
            return self.pwm_idle
        elif temp_c < self.target_temp:
            ratio = (temp_c - self.idle_temp) / (self.target_temp - self.idle_temp)
            return int(self.pwm_idle + ratio * (self.pwm_target - self.pwm_idle))
        elif temp_c < self.critical_temp:
            ratio = (temp_c - self.target_temp) / (self.critical_temp - self.target_temp)
            return int(self.pwm_target + ratio * (PWM_MAX - self.pwm_target))
        else:
            return PWM_MAX


@dataclass
class FanConfig:
    """Configuration for controlling a single fan."""

    name: str
    pwm_path: Path = None
    enable_path: Path = None
    curve: FanCurve = field(default_factory=FanCurve)
    enabled: bool = True

    def __post_init__(self):
        if self.pwm_path is None:
            self.pwm_path = DEFAULT_PWM_PATHS.get(
                self.name, Path(HWMON4) / self.name
            )
        if self.enable_path is None:
            self.enable_path = DEFAULT_ENABLE_PATHS.get(
                self.name, Path(HWMON4) / f"{self.name}_enable"
            )


@dataclass
class SensorConfig:
    """Configuration for a temperature sensor source."""

    name: str
    hwmon_path: Path
    label: str = ""
    weight: float = 1.0


# ---------------------------------------------------------------------------
# Local file I/O (no SSH — runs on the target machine)
# ---------------------------------------------------------------------------

class DellFanController:
    """Controls Dell server fans by reading/writing local hwmon sysfs files."""

    def __init__(self):
        self._manual_mode_set = False

    # -- file helpers ----------------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        """Read a sysfs file and return its contents (stripped)."""
        with open(path, "r") as f:
            return f.read().strip()

    @staticmethod
    def write_file(path: Path, value: str):
        """Write a value to a sysfs file."""
        with open(path, "w") as f:
            f.write(value)

    # -- fan control -----------------------------------------------------------

    def ensure_manual_mode(self, fan_configs: list[FanConfig]):
        """Set PWM enable mode to manual (1) for each fan. Only done once."""
        for fc in fan_configs:
            if not fc.enabled:
                continue
            try:
                current = self.read_file(fc.enable_path)
                if current != "1":
                    logging.warning(
                        "Setting %s to manual mode (was: %s)",
                        fc.enable_path.name, current,
                    )
                    self.write_file(fc.enable_path, "1")
            except FileNotFoundError:
                logging.warning(
                        "Enable path %s not found — fan may already be in manual mode or path differs",
                        fc.enable_path,
                )

    def restore_auto_mode(self, fan_configs: list[FanConfig]):
        """Restore PWM enable mode to auto (2) for each fan on shutdown."""
        for fc in fan_configs:
            if not fc.enabled:
                continue
            try:
                self.write_file(fc.enable_path, "2")
                logging.info("Restored %s to auto mode", fc.enable_path.name)
            except FileNotFoundError:
                logging.warning(
                    "Could not restore %s — enable path not found",
                    fc.enable_path,
                )
            except Exception as e:
                logging.error("Could not restore %s: %s", fc.enable_path.name, e)

    def read_temp(self, sensor: SensorConfig) -> float:
        """Read a temperature sensor and return degrees Celsius."""
        raw = self.read_file(sensor.hwmon_path)
        millidegrees = int(raw)
        return millidegrees / 1000.0

    def read_fan_rpm(self, fan_index: int) -> int:
        """Read current fan RPM from hwmon."""
        path = Path(HWMON4) / f"fan{fan_index}_input"
        raw = self.read_file(path)
        return int(raw)

    def set_fan_pwm(self, fan_config: FanConfig, pwm_value: int):
        """Set PWM value for a fan."""
        clamped = max(PWM_MIN, min(PWM_MAX, pwm_value))
        self.write_file(fan_config.pwm_path, str(clamped))
        return clamped

    def get_combined_temp(self, sensors: list[SensorConfig]) -> float:
        """Read all configured sensors and return weighted average temperature."""
        temps = []
        weights = []
        for s in sensors:
            try:
                temp_c = self.read_temp(s)
                temps.append(temp_c)
                weights.append(s.weight)
                logging.debug("  %s (%s): %.1f °C", s.name, s.label or "unknown", temp_c)
            except FileNotFoundError:
                logging.warning(
                    "Sensor %s not found at %s — skipping", s.name, s.hwmon_path
                )
            except Exception as e:
                logging.warning("Could not read sensor %s (%s): %s", s.name, s.hwmon_path, e)

        if not temps:
            raise RuntimeError("No valid temperature readings available")

        total_weight = sum(weights)
        return sum(t * w for t, w in zip(temps, weights)) / total_weight


# ---------------------------------------------------------------------------
# Sensor discovery
# ---------------------------------------------------------------------------

def discover_sensors():
    """List all available hwmon sensors on the local machine."""
    print(f"\n{'='*60}")
    print(f"  Available Sensors (hwmon4 — {HWMON4})")
    print(f"{'='*60}\n")

    ctrl = DellFanController()

    # Temperature sensors
    print("System Temperature Sensors:")
    for name, (path, label) in DEFAULT_TEMP_PATHS.items():
        try:
            raw = ctrl.read_file(path)
            temp_c = int(raw) / 1000.0
            label_path = path.parent / f"{name}_label"
            try:
                label_val = ctrl.read_file(label_path) or label
            except Exception:
                label_val = label
            print(f"  {name}: {label_val:<12} → {temp_c:.1f} °C  ({path})")
        except FileNotFoundError:
            print(f"  {name}: {label:<12} → NOT FOUND")
        except Exception as e:
            print(f"  {name}: {label:<12} → ERROR: {e}")

    # NVMe drive temperatures
    print("\nNVMe Drive Temperatures:")
    for name, (path, label) in NVME_TEMP_PATHS.items():
        try:
            raw = ctrl.read_file(path)
            temp_c = int(raw) / 1000.0
            crit_path = path.parent / "temp1_crit"
            max_path = path.parent / "temp1_max"
            thresholds = ""
            try:
                crit_c = int(ctrl.read_file(crit_path)) / 1000.0
                max_c = int(ctrl.read_file(max_path)) / 1000.0
                thresholds = f" (warn={max_c:.0f}°C, crit={crit_c:.0f}°C)"
            except Exception:
                pass
            print(f"  {name}: {label:<12} → {temp_c:.1f} °C{thresholds}")
        except FileNotFoundError:
            print(f"  {name}: {label:<12} → NOT FOUND")
        except Exception as e:
            print(f"  {name}: {label:<12} → ERROR: {e}")

    # Fan info
    print("\nFans:")
    for fan_idx in (1, 2):
        try:
            label = ctrl.read_file(Path(HWMON4) / f"fan{fan_idx}_label") or f"Fan {fan_idx}"
            rpm = ctrl.read_file(Path(HWMON4) / f"fan{fan_idx}_input")
            pwm = ctrl.read_file(Path(HWMON4) / f"pwm{fan_idx}")
            mode = ctrl.read_file(Path(HWMON4) / f"pwm{fan_idx}_enable")
            print(f"  pwm{fan_idx}: {label:<16} RPM={rpm}  PWM={pwm}/255  mode={mode}")
        except FileNotFoundError:
            print(f"  pwm{fan_idx}: NOT FOUND")
        except Exception as e:
            print(f"  pwm{fan_idx}: ERROR: {e}")

    controller.close() if hasattr(controller, 'close') else None


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def build_temp_sensor(name: str) -> SensorConfig:
    """Create a SensorConfig from a sensor name or explicit path."""
    if name in ALL_TEMP_PATHS:
        path_obj, label = ALL_TEMP_PATHS[name]
        return SensorConfig(name=name, hwmon_path=path_obj, label=label)

    if name.startswith("/"):
        return SensorConfig(
            name=Path(name).name,
            hwmon_path=Path(name),
        )

    # Fallback: guess path under HWMON4
    guessed = Path(HWMON4) / (name if name.startswith("temp") else f"{name}_input")
    return SensorConfig(name=name, hwmon_path=guessed)


def load_yaml_config(path: str) -> Optional[dict]:
    """Load configuration from a YAML file (pyyaml optional)."""
    try:
        import yaml  # type: ignore
        with open(path, "r") as f:
            return yaml.safe_load(f)
    except ImportError:
        logging.warning(
            "PyYAML not installed. Cannot load config from %s. "
            "Install with: pip install pyyaml", path
        )
        return None
    except FileNotFoundError:
        logging.error("Config file not found: %s", path)
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dell Fan Control Daemon — runs locally on the target machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Sensor names:  temp1 (Ambient), temp2 (CPU), nvme0, nvme1, or any /sys/... path
Fan names:     fan1 (Processor), fan2 (Motherboard)

Examples:
  # Run once to test (requires sudo for write access to hwmon)
  sudo python3 dell_fan_control.py --once

  # Dry-run — see what would happen without writing
  sudo python3 dell_fan_control.py --once --dry-run

  # Start as daemon with default settings
  sudo python3 dell_fan_control.py --daemon

  # Custom curve, specific sensors, 10s interval
  sudo python3 dell_fan_control.py --daemon \\
      --temp-sensor temp2 \\
      --idle-temp 25 --target-temp 55 --critical-temp 80 \\
      --pwm-idle 48 --pwm-target 160 --interval 10

  # List available sensors
  sudo python3 dell_fan_control.py --discover
""",
    )

    # Sensors
    parser.add_argument(
        "--temp-sensor", "-t", action="append", dest="sensors", default=None,
        help="Temperature sensor(s) to monitor. Repeat for multiple sensors. "
             "Default: temp2 (CPU).",
    )

    # Fans
    parser.add_argument(
        "--fans", "-f", nargs="+", default=["fan1", "fan2"],
        help="Fans to control. Default: fan1 fan2.",
    )
    parser.add_argument(
        "--disable-fan", action="append", default=[],
        help="Fan(s) to leave untouched.",
    )

    # Fan curve
    curve = parser.add_argument_group("Fan Curve")
    curve.add_argument("--idle-temp", type=float, default=FanCurve.idle_temp,
                       help="°C below which idle PWM is used (default: 30)")
    curve.add_argument("--target-temp", type=float, default=FanCurve.target_temp,
                       help="°C at which target PWM is reached (default: 60)")
    curve.add_argument("--critical-temp", type=float, default=FanCurve.critical_temp,
                       help="°C above which fans go to max (default: 85)")
    curve.add_argument("--pwm-idle", type=int, default=FanCurve.pwm_idle,
                       help="PWM value at idle temp 0-255 (default: 64)")
    curve.add_argument("--pwm-target", type=int, default=FanCurve.pwm_target,
                       help="PWM value at target temp 0-255 (default: 192)")

    # Operation
    parser.add_argument("--interval", "-i", type=float, default=10.0,
                        help="Polling interval in seconds (default: 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without writing")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle and exit — useful for testing")
    parser.add_argument("--daemon", action="store_true",
                        help="Run as a continuous daemon (default if neither --once nor --discover)")
    parser.add_argument("--discover", "-d", action="store_true",
                        help="List available sensors and exit")
    parser.add_argument("--config", default=None,
                        help="Path to YAML config file")
    parser.add_argument("--log-file", default=LOG_FILE,
                        help=f"Log file path (default: {LOG_FILE})")
    parser.add_argument("--pid-file", default=PID_FILE,
                        help=f"PID file path (default: {PID_FILE})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose/debug logging")

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Control loop
# ---------------------------------------------------------------------------

def run_once(
    controller: DellFanController,
    sensors: list[SensorConfig],
    fan_configs: list[FanConfig],
    curve: FanCurve,
    dry_run: bool = False,
    set_manual: bool = True,
) -> Optional[dict]:
    """Execute a single read-compute-write cycle. Returns status dict."""

    if set_manual:
        controller.ensure_manual_mode(fan_configs)

    # Read combined temperature
    try:
        combined_temp = controller.get_combined_temp(sensors)
    except RuntimeError as e:
        logging.error(str(e))
        return None

    result = {"temperature": combined_temp, "fans": {}}

    logging.info("Combined temperature: %.1f °C", combined_temp)

    for fc in fan_configs:
        if not fc.enabled:
            try:
                current_pwm = controller.read_file(fc.pwm_path)
                result["fans"][fc.name] = {"pwm": f"unchanged ({current_pwm})", "skipped": True}
            except Exception:
                result["fans"][fc.name] = {"pwm": "disabled", "skipped": True}
            continue

        desired_pwm = curve.pwm_for_temperature(combined_temp)

        # Read current PWM for comparison
        try:
            current_pwm = int(controller.read_file(fc.pwm_path))
        except (ValueError, Exception):
            current_pwm = -1

        if current_pwm == desired_pwm:
            logging.debug("  %s: already at PWM %d — no change", fc.name, desired_pwm)
        else:
            if dry_run:
                logging.info(
                    "  %s: would set PWM %d → %d", fc.name, current_pwm, desired_pwm,
                )
            else:
                actual_pwm = controller.set_fan_pwm(fc, desired_pwm)
                logging.info("  %s: set PWM %d → %d", fc.name, current_pwm, actual_pwm)

        result["fans"][fc.name] = {
            "previous_pwm": current_pwm,
            "new_pwm": desired_pwm,
            "skipped": False,
        }

    return result


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

def write_pid_file():
    """Write current PID to file."""
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logging.warning("Could not write PID file: %s", e)


def remove_pid_file():
    """Remove PID file on exit."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning("Could not remove PID file: %s", e)


_shutting_down = False

def shutdown_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _shutting_down
    sig_name = signal.Signals(signum).name
    logging.info("Received %s — shutting down...", sig_name)
    _shutting_down = True


def daemon_loop(
    controller: DellFanController,
    sensors: list[SensorConfig],
    fan_configs: list[FanConfig],
    curve: FanCurve,
    interval: float,
    dry_run: bool,
):
    """Run the continuous control loop."""

    # Install signal handlers
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    write_pid_file()
    logging.info("Daemon started (PID %d, interval %.1fs)", os.getpid(), interval)

    try:
        cycle = 0
        set_manual_first = True
        while not _shutting_down:
            cycle += 1
            logging.debug("--- Cycle %d ---", cycle)

            result = run_once(
                controller, sensors, fan_configs, curve, dry_run,
                set_manual=set_manual_first,
            )
            set_manual_first = False

            if result is not None:
                temp = result["temperature"]
                fan_summary = ", ".join(
                    f"{fname}={fdata.get('new_pwm', '?')}"
                    for fname, fdata in result["fans"].items()
                    if not fdata.get("skipped")
                )
                logging.info("Cycle %d: temp=%.1f°C  fans=[%s]", cycle, temp, fan_summary)

            # Sleep in small increments so we respond to signals promptly
            sleep_steps = max(1, int(interval * 4))
            step = interval / sleep_steps
            for _ in range(sleep_steps):
                if _shutting_down:
                    break
                time.sleep(step)

    finally:
        logging.info("Restoring fans to auto mode...")
        controller.restore_auto_mode(fan_configs)
        remove_pid_file()
        logging.info("Daemon stopped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    # Load YAML config if provided (overrides defaults, CLI flags override both)
    if args.config:
        cfg = load_yaml_config(args.config)
        if cfg:
            # Apply YAML values as defaults where CLI has not been explicit
            curve_cfg = cfg.get("curve", {})
            if "idle_temp" in curve_cfg and not args.idle_temp:
                args.idle_temp = curve_cfg["idle_temp"]
            if "target_temp" in curve_cfg and not args.target_temp:
                args.target_temp = curve_cfg["target_temp"]
            if "critical_temp" in curve_cfg and not args.critical_temp:
                args.critical_temp = curve_cfg["critical_temp"]
            if "pwm_idle" in curve_cfg and not args.pwm_idle:
                args.pwm_idle = curve_cfg["pwm_idle"]
            if "pwm_target" in curve_cfg and not args.pwm_target:
                args.pwm_target = curve_cfg["pwm_target"]

    # Logging setup
    log_level = logging.DEBUG if args.verbose else logging.INFO

    try:
        file_handler = logging.FileHandler(args.log_file)
        file_handler.setLevel(log_level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
    except PermissionError:
        logging.warning("Cannot write to %s — logging to stderr only", args.log_file)
        file_handler = None

    root = logging.getLogger()
    root.setLevel(log_level)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    root.addHandler(console)

    if file_handler:
        root.addHandler(file_handler)

    # Discover mode
    if args.discover:
        discover_sensors()
        return

    # Check we're running as root (needed to write hwmon)
    if not args.dry_run and os.geteuid() != 0:
        logging.error("This script requires root privileges to write to hwmon. Use sudo.")
        sys.exit(1)

    # Build curve
    curve = FanCurve(
        idle_temp=args.idle_temp,
        target_temp=args.target_temp,
        critical_temp=args.critical_temp,
        pwm_idle=args.pwm_idle,
        pwm_target=args.pwm_target,
    )
    logging.info("Fan curve: idle=%.0f°C/%d  target=%.0f°C/%d  critical=%.0f°C/255",
                 curve.idle_temp, curve.pwm_idle,
                 curve.target_temp, curve.pwm_target,
                 curve.critical_temp)

    # Build sensor configs
    sensor_names = args.sensors if args.sensors else ["temp2"]
    sensors = [build_temp_sensor(s) for s in sensor_names]
    logging.info("Sensors: %s", ", ".join(f"{s.name}({s.label})" for s in sensors))

    # Build fan configs
    fan_configs = []
    for fan_name in args.fans:
        enabled = fan_name not in args.disable_fan
        fc = FanConfig(name=fan_name, curve=curve, enabled=enabled)
        fan_configs.append(fc)
    logging.info(
        "Fans: %s",
        ", ".join(f"{fc.name}{' (disabled)' if not fc.enabled else ''}" for fc in fan_configs),
    )

    # Controller
    controller = DellFanController()

    # Run
    if args.once or not args.daemon:
        run_once(controller, sensors, fan_configs, curve, args.dry_run)
    else:
        daemon_loop(controller, sensors, fan_configs, curve, args.interval, args.dry_run)


if __name__ == "__main__":
    main()
