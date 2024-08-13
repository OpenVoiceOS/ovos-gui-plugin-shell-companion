from enum import Enum
import datetime
import shutil
import subprocess
from datetime import timedelta
from typing import Optional
from astral import LocationInfo
from astral.sun import sun
from json_database import JsonStorage
from ovos_bus_client import Message
from ovos_config import Configuration
from ovos_utils.events import EventSchedulerInterface
from ovos_utils.log import LOG
from ovos_utils.time import now_local


class BrightnessConstants(Enum):
    """Constants for brightness control, used with the BrightnessManager class."""

    MIN_BRIGHTNESS = 0
    MAX_BRIGHTNESS_DSI = 255
    MAX_BRIGHTNESS_HDMI = 100
    AUTO_DIM_BRIGHTNESS = 20
    AUTO_DIM_CHECK_INTERVAL = 1  # minute
    AUTO_NIGHT_MODE_CHECK_INTERVAL = 1  # hour
    SUNSET_CHECK_INTERVAL = 24  # hours


class BrightnessManager:
    def __init__(self, bus, config: Optional[JsonStorage] = None, *args, **kwargs):
        """
        Initialize the BrightnessManager.

        Args:
            bus: The message bus.
            config: JsonStorage configuration object.
            *args: Variable length argument list.
            *kwargs: Arbitrary keyword arguments.
        """
        LOG.debug(f"Received unexpected arguments, ignoring: {args} {kwargs}")
        self.bus = bus
        self.config = config
        if self.config is None:
            LOG.warning(
                "No config provided, using default config but it will not be saved"
            )
            self.config = {"auto_dim": False, "auto_nightmode": True}
        self.event_scheduler = EventSchedulerInterface()
        self.event_scheduler.set_id("ovos-shell")
        self.event_scheduler.set_bus(self.bus)
        self.device_interface = "DSI"
        self.vcgencmd = shutil.which("vcgencmd")
        self.ddcutil = shutil.which("ddcutil")
        self.ddcutil_detected_bus = None
        self.ddcutil_brightness_code = None

        LOG.info(f"vcgencmd found: {self.vcgencmd is not None}")
        LOG.info(f"ddcutil found: {self.ddcutil is not None}")

        self.sunset_time = None
        self.sunrise_time = None
        self.get_sunset_time()

        self.discover()

        self.bus.on("phal.brightness.control.get", self.query_current_brightness)
        self.bus.on("phal.brightness.control.set", self.set_brightness_from_bus)
        self.bus.on(
            "speaker.extension.display.auto.dim.changed", self.is_auto_dim_enabled
        )
        self.bus.on(
            "speaker.extension.display.auto.nightmode.changed",
            self.is_auto_night_mode_enabled,
        )
        self.bus.on("gui.page_interaction", self.undim_display)
        self.bus.on("gui.page_gained_focus", self.undim_display)
        self.bus.on("recognizer_loop:wakeword", self.undim_display)
        self.bus.on("recognizer_loop:record_begin", self.undim_display)

        self.evaluate_initial_settings()

    @property
    def auto_dim(self):
        """
        Get the auto dim setting from the config.

        Returns:
            bool: The auto dim setting.
        """
        return self.config.get("auto_dim", False)

    @auto_dim.setter
    def auto_dim(self, value):
        """
        Set the auto dim setting in the config.

        Args:
            value (bool): The auto dim setting to set.
        """
        if self.config.get("auto_dim") != value:
            self.config["auto_dim"] = value
            self.config.store()
        self.start_auto_dim()

    @property
    def auto_nightmode(self):
        """
        Get the auto night mode setting from the config.

        Returns:
            bool: The auto night mode setting.
        """
        return self.config.get("auto_nightmode", True)

    @auto_nightmode.setter
    def auto_nightmode(self, value):
        """
        Set the auto night mode setting in the config.

        Args:
            value (bool): The auto night mode setting to set.
        """
        if self.config.get("auto_nightmode") != value:
            self.config["auto_nightmode"] = value
            self.config.store()
            self.start_auto_night_mode()

    def discover(self):
        """
        Discover the brightness control device interface (HDMI / DSI) on the Raspberry PI.
        """
        try:
            LOG.info("Discovering brightness control device interface")
            if self.vcgencmd:
                proc = subprocess.Popen(
                    [self.vcgencmd, "get_config", "display_default_lcd"],
                    stdout=subprocess.PIPE,
                )
                if proc.stdout.read().decode("utf-8").strip() in (
                    "1",
                    "display_default_lcd=1",
                ):
                    self.device_interface = "DSI"
                    self.max_brightness = BrightnessConstants.MAX_BRIGHTNESS_DSI.value
                else:
                    self.device_interface = "HDMI"
                    self.max_brightness = BrightnessConstants.MAX_BRIGHTNESS_HDMI.value
                LOG.info(
                    f"Brightness control device interface is {self.device_interface}"
                )
            else:
                LOG.warning("vcgencmd not found, defaulting to DSI interface")
                self.device_interface = "DSI"
                self.max_brightness = BrightnessConstants.MAX_BRIGHTNESS_DSI.value

            if self.device_interface == "HDMI" and self.ddcutil:
                proc_detect = subprocess.Popen(
                    [self.ddcutil, "detect"], stdout=subprocess.PIPE
                )

                ddcutil_detected_output = proc_detect.stdout.read().decode("utf-8")
                if "I2C bus:" in ddcutil_detected_output:
                    bus_code = (
                        ddcutil_detected_output.split("I2C bus: ")[1]
                        .strip()
                        .split("\n")[0]
                    )
                    self.ddcutil_detected_bus = bus_code.split("-")[1].strip()
                else:
                    self.ddcutil_detected_bus = None
                    LOG.error("Display is not detected by DDCUTIL")

                if self.ddcutil_detected_bus:
                    proc_fetch_vcp = subprocess.Popen(
                        [
                            self.ddcutil,
                            "getvcp",
                            "known",
                            "--bus",
                            self.ddcutil_detected_bus,
                        ],
                        stdout=subprocess.PIPE,
                    )
                    # check the vcp output for the Brightness string and get its VCP code
                    for line in proc_fetch_vcp.stdout:
                        if "Brightness" in line.decode("utf-8"):
                            self.ddcutil_brightness_code = (
                                line.decode("utf-8").split(" ")[2].strip()
                            )
            elif self.device_interface == "HDMI":
                LOG.warning("ddcutil not found, HDMI brightness control may not work")
        except Exception as e:
            LOG.exception(f"Error in discover method: {e}")
            LOG.info("Falling back to DSI interface")
            self.device_interface = "DSI"
            self.max_brightness = BrightnessConstants.MAX_BRIGHTNESS_DSI.value

    def evaluate_initial_settings(self):
        if self.auto_dim:
            self.start_auto_dim()
        if self.auto_nightmode:
            self.start_auto_night_mode()

    def get_brightness(self):
        """
        Get the current brightness level.

        Returns:
            int: The current brightness level, or None if there was an error.
        """
        LOG.info("Getting current brightness level")
        try:
            if self.device_interface == "HDMI":
                proc_fetch_vcp = subprocess.Popen(
                    [
                        self.ddcutil,
                        "getvcp",
                        self.ddcutil_brightness_code,
                        "--bus",
                        self.ddcutil_detected_bus,
                    ],
                    stdout=subprocess.PIPE,
                )
                for line in proc_fetch_vcp.stdout:
                    if "current value" in line.decode("utf-8"):
                        brightness_level = (
                            line.decode("utf-8")
                            .split("current value = ")[1]
                            .split(",")[0]
                            .strip()
                        )
                        return int(brightness_level)

            if self.device_interface == "DSI":
                with open(
                    "/sys/class/backlight/rpi_backlight/actual_brightness", "r"
                ) as f:
                    brightness_level = f.read().strip()
                return int(brightness_level)
        except Exception as e:
            LOG.exception(f"Error getting brightness: {e}")
            return None

    def query_current_brightness(self, message: Message):
        """
        Query and emit the current brightness level.

        Args:
            message: The incoming message (unused).
        """
        current_brightness = self.get_brightness()
        if current_brightness is not None:
            if self.device_interface == "HDMI":
                self.bus.emit(
                    message.forward(
                        Message(
                            "phal.brightness.control.get.response",
                            {"brightness": current_brightness},
                        )
                    )
                )
            elif self.device_interface == "DSI":
                brightness_percentage = int(
                    (current_brightness / BrightnessConstants.MAX_BRIGHTNESS_DSI.value)
                    * 100
                )
                self.bus.emit(
                    message.forward(
                        Message(
                            "phal.brightness.control.get.response",
                            {"brightness": brightness_percentage},
                        )
                    )
                )
        else:
            self.bus.emit(
                message.forward(
                    Message(
                        "phal.brightness.control.get.error",
                        {"error": "Failed to get brightness"},
                    )
                )
            )

    def set_brightness(self, level):
        """
        Set the brightness level.

        Args:
            level (int): The brightness level to set.
        """
        LOG.debug(f"Setting brightness level to {level}")
        try:
            if self.device_interface == "HDMI":
                subprocess.run(
                    [
                        self.ddcutil,
                        "setvcp",
                        self.ddcutil_brightness_code,
                        "--bus",
                        self.ddcutil_detected_bus,
                        str(level),
                    ],
                    check=True,
                )
            elif self.device_interface == "DSI":
                with open("/sys/class/backlight/rpi_backlight/brightness", "w") as f:
                    f.write(str(level))
            LOG.info(f"Brightness level set to {level}")
        except subprocess.CalledProcessError as e:
            LOG.error(f"Error setting brightness: {e}")
        except Exception as e:
            LOG.exception(f"Unexpected error setting brightness: {e}")

    def set_brightness_from_bus(self, message: Message):
        """
        Set the brightness level from a bus message.

        Args:
            message: The incoming message containing the brightness level.
        """
        LOG.debug("Setting brightness level from bus")
        level = message.data.get("brightness")
        if level is None:
            self.bus.emit(
                message.forward(
                    Message(
                        "phal.brightness.control.set.error",
                        {"error": "No brightness level provided"},
                    )
                )
            )
            return

        try:
            level = float(level)
        except ValueError:
            self.bus.emit(
                message.forward(
                    Message(
                        "phal.brightness.control.set.error",
                        {"error": "Invalid brightness level provided"},
                    )
                )
            )
            return

        if self.device_interface == "HDMI":
            percent_level = 100 * level
            apply_level = max(
                BrightnessConstants.MIN_BRIGHTNESS.value,
                min(
                    round(percent_level / 10) * 10,
                    BrightnessConstants.MAX_BRIGHTNESS_HDMI.value,
                ),
            )
        elif self.device_interface == "DSI":
            percent_level = BrightnessConstants.MAX_BRIGHTNESS_DSI.value * level
            apply_level = max(
                BrightnessConstants.MIN_BRIGHTNESS.value,
                min(
                    round(percent_level / 10) * 10,
                    BrightnessConstants.MAX_BRIGHTNESS_DSI.value,
                ),
            )

        self.set_brightness(apply_level)
        self.bus.emit(
            message.forward(
                Message(
                    "phal.brightness.control.set.response", {"brightness": apply_level}
                )
            )
        )

    def start_auto_dim(self):
        """
        Start the auto dim feature or adjust brightness if auto dim is disabled.
        """
        current_brightness = self.get_brightness()
        if current_brightness is None:
            LOG.error("Failed to get current brightness. Cannot start auto dim.")
            return

        if self.auto_dim:
            if current_brightness > BrightnessConstants.AUTO_DIM_BRIGHTNESS.value:
                LOG.debug("Starting auto dim")
                self.bus.emit(
                    Message(
                        "phal.brightness.control.auto.dim.update",
                        {"brightness": BrightnessConstants.AUTO_DIM_BRIGHTNESS.value},
                    )
                )
                self.set_brightness(BrightnessConstants.AUTO_DIM_BRIGHTNESS.value)

            # Schedule the next check
            if not self.event_scheduler.is_scheduled("ovos-shell.auto.dim.check"):
                self.event_scheduler.schedule_event(
                    self.start_auto_dim,
                    when=now_local()
                    + timedelta(
                        minutes=BrightnessConstants.AUTO_DIM_CHECK_INTERVAL.value
                    ),
                    name="ovos-shell.auto.dim.check",
                )
        else:
            # If auto_dim is disabled but brightness is lower than max, adjust it
            if current_brightness < self.max_brightness:
                LOG.debug("Auto dim is disabled. Adjusting brightness to maximum.")
                self.set_brightness(self.max_brightness)
                self.bus.emit(
                    Message(
                        "phal.brightness.control.auto.dim.update",
                        {"brightness": self.max_brightness},
                    )
                )
            else:
                LOG.debug(
                    "Auto dim is disabled and brightness is already at maximum. No action needed."
                )

    def stop_auto_dim(self):
        """
        Stop the auto dim feature.
        """
        LOG.debug("Stopping Auto Dim")
        self.auto_dim = False
        if self.event_scheduler.is_scheduled("ovos-shell.auto.dim.check"):
            self.event_scheduler.cancel_scheduled_event("ovos-shell.auto.dim.check")

        # Ensure brightness is set to maximum when auto dim is stopped
        current_brightness = self.get_brightness()
        if current_brightness is not None and current_brightness < self.max_brightness:
            self.set_brightness(self.max_brightness)
            self.bus.emit(
                Message(
                    "phal.brightness.control.auto.dim.update",
                    {"brightness": self.max_brightness},
                )
            )

    def undim_display(self, message=None):
        """
        Undim the display.

        Args:
            message: The incoming message (unused).
        """
        if self.get_brightness() < self.max_brightness:
            LOG.debug("Undimming display")
            self.set_brightness(self.max_brightness)
            self.bus.emit(
                message.forward(
                    Message(
                        "phal.brightness.control.auto.dim.update",
                        {"brightness": self.max_brightness},
                    )
                )
            )
        else:
            LOG.debug(
                "Received request to undim display, but auto dim is not enabled. No action needed."
            )

    def get_sunset_time(self, message=None):
        """
        Get the sunset and sunrise times.

        Args:
            message: The incoming message (unused).
        """
        LOG.debug("Getting sunset time")
        now_time = now_local()
        try:
            location = Configuration()["location"]
            lat = location["coordinate"]["latitude"]
            lon = location["coordinate"]["longitude"]
            tz = location["timezone"]["code"]
            city = LocationInfo("Some city", "Some location", tz, lat, lon)
            s = sun(city.observer, date=now_time)
            self.sunset_time = s["sunset"]
            self.sunrise_time = s["sunrise"]
        except Exception as e:
            LOG.exception(f"Using default times for sunrise/sunset: {e}")
            self.sunset_time = datetime.datetime(
                year=now_time.year, month=now_time.month, day=now_time.day, hour=22
            )
            self.sunrise_time = self.sunset_time + timedelta(hours=8)

        # check sunset times again in 24 hours
        self.event_scheduler.schedule_event(
            self.get_sunset_time,
            when=now_time
            + timedelta(hours=BrightnessConstants.SUNSET_CHECK_INTERVAL.value),
            name="ovos-shell.suntimes.check",
        )

    def start_auto_night_mode(self, message: Optional[Message] = None):
        """
        Start the auto night mode feature.

        Args:
            message: The incoming message (optional).
        """
        if self.auto_nightmode:
            date = now_local()
            if not self.event_scheduler.is_scheduled("ovos-shell.night.mode.check"):
                self.event_scheduler.schedule_event(
                    self.start_auto_night_mode,
                    when=date
                    + timedelta(
                        hours=BrightnessConstants.AUTO_NIGHT_MODE_CHECK_INTERVAL.value
                    ),
                    name="ovos-shell.night.mode.check",
                )
            if self.sunset_time < date < self.sunrise_time:
                LOG.debug("It is night time")
                if not self.auto_dim:
                    self.auto_dim = True
            else:
                LOG.debug("It is day time")
                if self.auto_dim:
                    self.auto_dim = False

    def stop_auto_night_mode(self):
        """
        Stop the auto night mode feature.
        """
        LOG.debug("Stopping auto night mode")
        self.auto_nightmode = False
        if self.event_scheduler.is_scheduled("ovos-shell.night.mode.check"):
            self.event_scheduler.cancel_scheduled_event("ovos-shell.night.mode.check")
        if self.auto_dim:
            self.auto_dim = False

    def is_auto_night_mode_enabled(self, message: Optional[Message] = None):
        """
        Check if auto night mode is enabled and start/stop it accordingly.

        Args:
            message: The incoming message (optional).
        """
        if self.auto_nightmode:
            self.start_auto_night_mode(message)
        else:
            self.stop_auto_night_mode()

    def reset_to_default(self):
        """
        Reset brightness settings to default values.
        """
        LOG.info("Resetting brightness settings to default values")
        self.auto_dim = False
        self.auto_nightmode = True
        self.set_brightness(self.max_brightness)
        self.bus.emit(
            Message(
                "phal.brightness.control.reset",
                {
                    "brightness": self.max_brightness,
                    "auto_dim": False,
                    "auto_nightmode": True,
                },
            )
        )
