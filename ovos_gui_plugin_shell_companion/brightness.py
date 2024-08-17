import datetime
import shutil
import subprocess
from datetime import timedelta
from typing import Optional, Tuple

from astral import LocationInfo
from astral.sun import sun
from ovos_bus_client import Message
from ovos_config import Configuration
from ovos_utils.events import EventSchedulerInterface
from ovos_utils.log import LOG
from ovos_utils.time import now_local

from ovos_gui_plugin_shell_companion.helpers import update_config


class BrightnessManager:
    def __init__(self, bus, config: dict):
        """
        Initialize the BrightnessManager.

        Args:
            bus: Message bus for inter-process communication.
            config: Configuration dictionary for brightness settings.
        """
        self.bus = bus
        self.config = config
        self.event_scheduler = EventSchedulerInterface()
        self.event_scheduler.set_id("ovos-shell")
        self.event_scheduler.set_bus(self.bus)
        self.device_interface = "DSI"
        self.vcgencmd = shutil.which("vcgencmd")
        self.ddcutil = shutil.which("ddcutil")
        self.ddcutil_detected_bus = None
        self.ddcutil_brightness_code = None
        self.device_interface = None

        self.default_brightness = self.config.get("default_brightness", 100)
        self._brightness_level: int = self.default_brightness
        self.sunrise_time, self.sunset_time = None, None

        self.discover()

        self.bus.on("phal.brightness.control.get", self.handle_get_brightness)
        self.bus.on("phal.brightness.control.set", self.handle_set_brightness)
        self.bus.on("gui.page_interaction", self.handle_undim_screen)
        self.bus.on("gui.page_gained_focus", self.handle_undim_screen)
        self.bus.on("recognizer_loop:wakeword", self.handle_undim_screen)
        self.bus.on("recognizer_loop:record_begin", self.handle_undim_screen)

    ##############################################
    # brightness manager
    # TODO - allow dynamic brightness based on camera, reacting live to brightness,

    # Discover the brightness control device interface (HDMI / DSI) on the Raspberry PI
    def discover(self):
        """
        Discover the brightness control device interface (HDMI / DSI) on the Raspberry Pi.
        """
        if not self.vcgencmd:
            LOG.error("Cannot find brightness control interface, 'vcgencmd' is unavailable.")
            return

        LOG.info("Discovering brightness control device interface")
        self.device_interface = self._get_device_interface()

        if self.device_interface == "HDMI" and self.ddcutil:
            self._discover_ddcutil_bus_and_brightness_code()
        elif self.device_interface is None:
            LOG.error("No compatible display interface found for brightness control.")

    def _get_device_interface(self) -> Optional[str]:
        """
        Get the display device interface type (DSI or HDMI).

        Returns:
            Optional[str]: 'DSI' for DSI interface, 'HDMI' for HDMI interface, or None if not found.
        """
        try:
            with subprocess.Popen([self.vcgencmd, "get_config", "display_default_lcd"],
                                  stdout=subprocess.PIPE) as proc:
                output = proc.stdout.read().decode("utf-8").strip()
                if output in ('1', 'display_default_lcd=1'):
                    return "DSI"
                return "HDMI" if self.ddcutil else None
        except subprocess.SubprocessError as e:
            LOG.error(f"Error discovering device interface: {e}")
            return None

    def _discover_ddcutil_bus_and_brightness_code(self):
        """
        Discover the DDC/CI bus and brightness VCP code for the HDMI interface.
        """
        try:
            with subprocess.Popen([self.ddcutil, "detect"], stdout=subprocess.PIPE) as proc:
                output = proc.stdout.read().decode("utf-8")
                bus_line = next((line for line in output.splitlines() if "I2C bus:" in line), None)
                if not bus_line:
                    LOG.error("Display is not detected by DDCUTIL.")
                    self.device_interface = None
                    return
                self.ddcutil_detected_bus = bus_line.split("I2C bus: ")[1].split("-")[1].strip()

            with subprocess.Popen([self.ddcutil, "getvcp", "known", "--bus", self.ddcutil_detected_bus],
                                  stdout=subprocess.PIPE) as proc:
                for line in proc.stdout:
                    decoded_line = line.decode("utf-8")
                    if "Brightness" in decoded_line:
                        self.ddcutil_brightness_code = decoded_line.split(" ")[2].strip()
                        break
        except subprocess.SubprocessError as e:
            LOG.error(f"Error discovering DDC/CI bus and brightness code: {e}")
            self.device_interface = None

    # Get the current brightness level
    def get_brightness(self) -> int:
        """
        Get the current brightness level.

        Returns:
            int: The current brightness level.
        """
        if self.device_interface is None:
            LOG.error("brightness control interface not available, can not read brightness level")
            return self._brightness_level

        LOG.info("Getting current brightness level")
        if self.device_interface == "HDMI":
            proc_fetch_vcp = subprocess.Popen(
                [self.ddcutil, "getvcp", self.ddcutil_brightness_code, "--bus", self.ddcutil_detected_bus],
                stdout=subprocess.PIPE
            )
            for line in proc_fetch_vcp.stdout:
                if "current value" in line.decode("utf-8"):
                    brightness_level = line.decode("utf-8").split("current value = ")[1].split(",")[0].strip()
                    self._brightness_level = int(brightness_level)
        elif self.device_interface == "DSI":
            proc_fetch_vcp = subprocess.Popen(
                ["cat", "/sys/class/backlight/rpi_backlight/actual_brightness"], stdout=subprocess.PIPE
            )
            for line in proc_fetch_vcp.stdout:
                brightness_level = int(line.decode("utf-8").strip())
                self._brightness_level = int(brightness_level * 100 / 255)  # convert from 0-255 to 0-100 range

        return self._brightness_level

    def handle_get_brightness(self, message: Message):
        """
        Handle the 'get brightness' event from the message bus.

        Args:
            message: The message received from the bus.
        """
        self.bus.emit(message.response(data={"brightness": self.get_brightness()}))

    # Set the brightness level
    def set_brightness(self, level: int):
        """
        Set the brightness level.

        Args:
            level: Brightness level to set.
        """
        if self.device_interface is None:
            LOG.error("brightness control interface not available, can not change brightness")
            return
        level = int(level)
        if level == self._brightness_level:
            return  # avoid log spam

        LOG.debug(f"Setting brightness level: {level}")
        if self.device_interface == "HDMI":
            subprocess.Popen(
                [self.ddcutil, "setvcp", self.ddcutil_brightness_code,
                 "--bus", self.ddcutil_detected_bus, str(level)]
            )
        elif self.device_interface == "DSI":
            level = int(level * 255 / 100)  # DSI goes from 0 to 255, HDMI from o to 100
            subprocess.call(
                f"echo {level} > /sys/class/backlight/rpi_backlight/brightness", shell=True
            )

        LOG.info(f"Brightness level set to {level}")
        self._brightness_level = level
        self.bus.emit(Message("phal.brightness.control.auto.dim.update",
                              {"brightness": level}))

    def handle_set_brightness(self, message: Message):
        """
        Handle the 'set brightness' event from the message bus.

        Args:
            message: The message received from the bus.
        """
        LOG.debug("Setting brightness level from bus")
        level = message.data.get("brightness", "")

        percent_level = 100 * float(level)
        if float(level) < 0:
            apply_level = 0
        elif float(level) > 100:
            apply_level = 100
        else:
            apply_level = round(percent_level / 10) * 10

        self.set_brightness(apply_level)

    @property
    def auto_dim_enabled(self) -> bool:
        """
        Check if auto-dim is enabled.

        Returns:
            bool: True if auto-dim is enabled, False otherwise.
        """
        if self.device_interface is None:
            return False
        return self.config.get("auto_dim", True)

    def start_auto_dim(self, nightmode: bool = False):
        """
        Start the auto-dim functionality.
        """
        if self.device_interface is None:
            LOG.error("brightness control interface not available, auto-dim functionality forcefully disabled")
            return

        if nightmode:
            LOG.info("Nightmode: Auto Dim enabled until sunrise")
        else:
            LOG.info("Enabling Auto Dim")
            self.config["auto_dim"] = True
            update_config("auto_dim", True)

        # cancel any previous autodim event
        self._cancel_next_dim()
        # dim screen in 60 seconds
        seconds = self.config.get("auto_dim_seconds", 60)
        self.event_scheduler.schedule_event(self.handle_dim_screen,
                                            when=now_local() + timedelta(seconds=seconds),
                                            name="ovos-shell.autodim")

    def handle_dim_screen(self, message: Optional[Message] = None):
        """
        Handle the dimming of the screen.

        Args:
            message: Optional message received from the bus.
        """
        if self.auto_dim_enabled:
            lowb = self.config.get("low_brightness", 20)
            if self._brightness_level != lowb:
                LOG.debug("Auto-dim: Lowering brightness")
                self.set_brightness(lowb)
            if self.auto_night_mode_enabled and self.is_night:
                # show night clock in homescreen
                self.bus.emit(Message("phal.brightness.control.auto.night.mode.enabled"))

    def _restore(self):
        """
        Restore the brightness level if auto-dim had reduced it.
        """
        if self._brightness_level < self.default_brightness:
            LOG.debug("Auto-dim: Restoring brightness")
            self.set_brightness(self.default_brightness)

    def stop_auto_dim(self):
        """
        Stop the auto-dim functionality.
        """
        LOG.debug("Stopping Auto Dim")
        self._cancel_next_dim()
        self._restore()
        if self.auto_dim_enabled:
            self.config["auto_dim"] = False
            update_config("auto_dim", False)

    def _cancel_next_dim(self):
        # cancel the next unfired dim event
        try:
            time_left = self.event_scheduler.get_scheduled_event_status("ovos-shell.autodim")
            self.event_scheduler.cancel_scheduled_event("ovos-shell.autodim")
        except:
            pass  # throws exception if event not registered

    def handle_undim_screen(self, message: Optional[Message] = None):
        """
        Handle the undimming of the screen upon user interaction.

        Args:
            message: Optional message received from the bus.
        """
        if self.auto_dim_enabled:
            self._restore()
            self._cancel_next_dim()
            # schedule next auto-dim
            self.event_scheduler.schedule_event(self.handle_dim_screen,
                                                when=now_local() + timedelta(seconds=60),
                                                name="ovos-shell.autodim")

    ##################################
    # AUTO NIGHT MODE HANDLING
    # TODO - allow to do it based on camera, reacting live to brightness,
    #  instead of depending on sunset times
    @property
    def auto_night_mode_enabled(self) -> bool:
        """
        Check if auto night mode is enabled.

        Returns:
            bool: True if auto night mode is enabled, False otherwise.
        """
        return self.config.get("auto_nightmode", False)

    @property
    def is_night(self) -> bool:
        self.sunrise_time, self.sunset_time = self.get_suntimes()  # sync
        return self.sunset_time <= now_local() < self.sunrise_time

    def start_auto_night_mode(self):
        """
        Start the auto night mode functionality.
        """
        LOG.debug("Starting auto night mode")
        self.config["auto_nightmode"] = True
        update_config("auto_nightmode", True)

        if self.is_night:
            self.handle_sunset()
        else:
            self.handle_sunrise()

    def handle_sunrise(self, message: Optional[Message] = None):
        """
        Handle the sunrise event for auto night mode.

        Args:
            message: Optional message received from the bus.
        """
        self.sunrise_time, self.sunset_time = self.get_suntimes()  # sync
        if self.auto_night_mode_enabled:
            LOG.debug("It is daytime")
            self.default_brightness = self.config.get("default_brightness", 100)
            # reset homescreen to day mode
            self.bus.emit(Message("ovos.homescreen.main_view.current_index.set",
                                  {"current_index": 1}))

            self.event_scheduler.schedule_event(self.handle_sunset,
                                                when=self.sunset_time,
                                                name="ovos-shell.sunset")

            if not self.auto_dim_enabled:
                # cancel the next unfired dim event
                self._cancel_next_dim()
                self._restore()

    def handle_sunset(self, message: Optional[Message] = None):
        """
        Handle the sunset event for auto night mode.

        Args:
            message: Optional message received from the bus.
        """
        self.sunrise_time, self.sunset_time = self.get_suntimes()  # sync
        if self.auto_night_mode_enabled:
            LOG.debug("It is nighttime")
            self.default_brightness = self.config.get("night_default_brightness", 70)
            self.set_brightness(self.default_brightness)
            # show night clock in homescreen
            self.bus.emit(Message("phal.brightness.control.auto.night.mode.enabled"))
            # equivalent to
            # self.bus.emit(Message("ovos.homescreen.main_view.current_index.set", {"current_index": 0}))

            self.event_scheduler.schedule_event(self.handle_sunrise,
                                                when=self.sunrise_time,
                                                name="ovos-shell.sunrise")
            if not self.auto_dim_enabled:
                self.start_auto_dim(nightmode=True)

    def stop_auto_night_mode(self):
        """
        Stop the auto night mode functionality.
        """
        LOG.debug("Stopping auto night mode")
        self.config["auto_nightmode"] = False
        update_config("auto_nightmode", False)

    def get_suntimes(self) -> Tuple[datetime.datetime, datetime.datetime]:
        sunrise = self.config.get("sunrise_time", "auto")
        sunset = self.config.get("sunset_time", "auto")
        sunset_time = None
        sunrise_time = None

        reference = now_local()  # now_local() is tz aware

        # check if sunrise has been explicitly configured by user
        if ":" in sunrise:
            hours, mins = sunrise.split(":")
            sunrise_time = datetime.datetime(hour=int(hours),
                                             minute=int(mins),
                                             day=reference.day,
                                             month=reference.month,
                                             year=reference.year,
                                             tzinfo=reference.tzinfo)
            if reference > sunrise_time:
                sunrise_time += timedelta(days=1)

        # check if sunset has been explicitly configured by user
        if ":" in sunset:
            hours, mins = sunset.split(":")
            sunset_time = datetime.datetime(hour=int(hours),
                                            minute=int(mins),
                                            day=reference.day,
                                            month=reference.month,
                                            year=reference.year,
                                            tzinfo=reference.tzinfo)
            if reference > sunset_time:
                sunset_time += timedelta(days=1)

        # auto determine sunrise/sunset
        if sunrise_time is None or sunset_time is None:
            location = Configuration()["location"]
            lat = location["coordinate"]["latitude"]
            lon = location["coordinate"]["longitude"]
            tz = location["timezone"]["code"]
            city = LocationInfo("Some city", "Some location", tz, lat, lon)

            s = sun(city.observer, date=reference)
            s2 = sun(city.observer, date=reference + timedelta(days=1))
            if not sunset_time:
                sunset_time = s["sunset"]
                if reference > sunset_time:  # get next sunset, today's already happened
                    sunset_time = s2["sunset"]
            if not sunrise_time:
                sunrise_time = s["sunrise"]
                if reference > sunrise_time:  # get next sunrise, today's already happened
                    sunrise_time = s2["sunrise"]

        # info logs
        if self.sunrise_time is None or self.sunrise_time != sunrise_time:
            LOG.info(f"Sunrise time: {sunrise_time}")
        if self.sunset_time is None or self.sunset_time != sunset_time:
            LOG.info(f"Sunset time: {sunset_time}")
        return sunrise_time, sunset_time
