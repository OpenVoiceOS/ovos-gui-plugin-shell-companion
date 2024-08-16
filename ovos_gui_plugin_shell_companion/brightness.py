import datetime
import shutil
import subprocess
from datetime import timedelta

from astral import LocationInfo
from astral.sun import sun
from ovos_bus_client import Message
from ovos_config import Configuration
from ovos_utils.events import EventSchedulerInterface
from ovos_utils.log import LOG
from ovos_utils.time import now_local

from ovos_gui_plugin_shell_companion.helpers import update_config


class BrightnessManager:
    def __init__(self, bus, config):
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

        self._brightness_level = 100
        self.sunset_time = None
        self.sunrise_time = None
        self.get_sunset_time()

        self.discover()

        self.bus.on("phal.brightness.control.get", self.handle_get_brightness)
        self.bus.on("phal.brightness.control.set", self.handle_set_brightness)
        self.bus.on("gui.page_interaction", self.handle_undim_screen)
        self.bus.on("gui.page_gained_focus", self.handle_undim_screen)
        self.bus.on("recognizer_loop:wakeword", self.handle_undim_screen)
        self.bus.on("recognizer_loop:record_begin", self.handle_undim_screen)

    ##############################################
    # brightness manager - TODO generic non rpi support
    # Discover the brightness control device interface (HDMI / DSI) on the Raspberry PI
    def discover(self):
        try:
            LOG.info("Discovering brightness control device interface")
            proc = subprocess.Popen([self.vcgencmd,
                                     "get_config", "display_default_lcd"], stdout=subprocess.PIPE)
            if proc.stdout.read().decode("utf-8").strip() in ('1', 'display_default_lcd=1'):
                self.device_interface = "DSI"
            else:
                self.device_interface = "HDMI"
            LOG.info("Brightness control device interface is {}".format(
                self.device_interface))

            if self.device_interface == "HDMI":
                proc_detect = subprocess.Popen(
                    [self.ddcutil, "detect"], stdout=subprocess.PIPE)

                ddcutil_detected_output = proc_detect.stdout.read().decode("utf-8")
                if "I2C bus:" in ddcutil_detected_output:
                    bus_code = ddcutil_detected_output.split(
                        "I2C bus: ")[1].strip().split("\n")[0]
                    self.ddcutil_detected_bus = bus_code.split("-")[1].strip()
                else:
                    ddcutil_detected_bus = None
                    LOG.error("Display is not detected by DDCUTIL")

                if self.ddcutil_detected_bus:
                    proc_fetch_vcp = subprocess.Popen(
                        [self.ddcutil, "getvcp", "known", "--bus", self.ddcutil_detected_bus],
                        stdout=subprocess.PIPE)
                    # check the vcp output for the Brightness string and get its VCP code
                    for line in proc_fetch_vcp.stdout:
                        if "Brightness" in line.decode("utf-8"):
                            self.ddcutil_brightness_code = line.decode(
                                "utf-8").split(" ")[2].strip()
        except Exception as e:
            LOG.error(e)
            LOG.info("Falling back to DSI interface")
            self.device_interface = "DSI"

        # Get the current brightness level

    def get_brightness(self) -> int:
        LOG.info("Getting current brightness level")
        if self.device_interface == "HDMI":
            proc_fetch_vcp = subprocess.Popen(
                [self.ddcutil, "getvcp", self.ddcutil_brightness_code, "--bus", self.ddcutil_detected_bus],
                stdout=subprocess.PIPE)
            for line in proc_fetch_vcp.stdout:
                if "current value" in line.decode("utf-8"):
                    brightness_level = line.decode(
                        "utf-8").split("current value = ")[1].split(",")[0].strip()
                    self._brightness_level = int(brightness_level)

        if self.device_interface == "DSI":
            proc_fetch_vcp = subprocess.Popen(
                ["cat", "/sys/class/backlight/rpi_backlight/actual_brightness"], stdout=subprocess.PIPE)
            for line in proc_fetch_vcp.stdout:
                brightness_level = line.decode("utf-8").strip()
                self._brightness_level = int(brightness_level)

        return self._brightness_level

    def handle_get_brightness(self, message):
        current_brightness = self.get_brightness()
        if self.device_interface == "HDMI":
            self.bus.emit(message.response(
                data={"brightness": current_brightness}))
        elif self.device_interface == "DSI":
            brightness_percentage = int((current_brightness / 255) * 100)
            self.bus.emit(message.response(
                data={"brightness": brightness_percentage}))

    # Set the brightness level
    def set_brightness(self, level: int):
        LOG.debug("Setting brightness level")
        if self.device_interface == "HDMI":
            subprocess.Popen([self.ddcutil, "setvcp", self.ddcutil_brightness_code,
                              "--bus", self.ddcutil_detected_bus, str(level)])
        elif self.device_interface == "DSI":
            subprocess.call(
                f"echo {level} > /sys/class/backlight/rpi_backlight/brightness", shell=True)

        LOG.info(f"Brightness level set to {level}")
        self._brightness_level = level

    def handle_set_brightness(self, message):
        LOG.debug("Setting brightness level from bus")
        level = message.data.get("brightness", "")

        if self.device_interface == "HDMI":
            percent_level = 100 * float(level)
            if float(level) < 0:
                apply_level = 0
            elif float(level) > 100:
                apply_level = 100
            else:
                apply_level = round(percent_level / 10) * 10

            self.set_brightness(apply_level)

        if self.device_interface == "DSI":
            percent_level = 255 * float(level)
            if float(level) < 0:
                apply_level = 0
            elif float(level) > 255:
                apply_level = 255
            else:
                apply_level = round(percent_level / 10) * 10

            self.set_brightness(apply_level)

    @property
    def auto_dim_enabled(self):
        return self.config.get("auto_dim", False)

    def start_auto_dim(self):
        LOG.debug("Enabling Auto Dim")
        self.config["auto_dim"] = True
        update_config("auto_dim", True)
        # dim screen in 60 seconds
        self.event_scheduler.schedule_event(self.handle_dim_screen,
                                            when=now_local() + timedelta(seconds=60), # TODO - seconds from config
                                            name="ovos-shell.autodim")

    def handle_dim_screen(self, message=None):
        if self.auto_dim_enabled:
            LOG.debug("Auto-dim: Lowering brightness")
            self.bus.emit(Message("phal.brightness.control.auto.dim.update",
                                  {"brightness": 20}))
            self.set_brightness(20)  # TODO - value from enum

    def _restore(self):
        if self._brightness_level == 20:
            LOG.debug("Auto-dim: Restoring brightness")
            if self.device_interface == "HDMI":
                self.set_brightness(100)  # TODO - value from enum
            if self.device_interface == "DSI":
                self.set_brightness(255)  # TODO - value from enum

    def stop_auto_dim(self):
        if self.auto_dim_enabled:
            LOG.debug("Stopping Auto Dim")
            self.config["auto_dim"] = False
            update_config("auto_dim", False)
            # cancel the next unfired dim event
            self.event_scheduler.cancel_scheduled_event("ovos-shell.autodim")
            self._restore()

    def handle_undim_screen(self, message=None):
        if self.auto_dim_enabled:
            self._restore()
            # schedule next auto-dim
            self.event_scheduler.schedule_event(self.handle_dim_screen,
                                                when=now_local() + timedelta(seconds=60),
                                                name="ovos-shell.autodim")

    ##################################
    # AUTO NIGHT MODE HANDLING
    # TODO - allow to do it based on camera, reacting live to brightness,
    #  instead of depending on sunset times
    def get_sunset_time(self, message=None):
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
            self.sunset_time = datetime.datetime(year=now_time.year,
                                                 month=now_time.month,
                                                 day=now_time.day, hour=22)
            self.sunrise_time = self.sunset_time + timedelta(hours=8)

        # check sunset times again in 12 hours
        self.event_scheduler.schedule_event(self.get_sunset_time,
                                            when=now_time + timedelta(hours=12),
                                            name="ovos-shell.suntimes.check")

    @property
    def auto_night_mode_enabled(self):
        return self.config.get("auto_nightmode", False)

    def start_auto_night_mode(self):
        LOG.debug("Starting auto night mode")
        self.config["auto_nightmode"] = True
        update_config("auto_nightmode", True)
        self.handle_auto_night_mode_check()

    def handle_auto_night_mode_check(self, message=None):
        if self.auto_night_mode_enabled:
            # TODO - we have exact sunset/sunrise times... why are we doing a periodic check?
            #  just schedule to the exact datetime right away....
            date = now_local()
            self.event_scheduler.schedule_event(self.handle_auto_night_mode_check,
                                                when=date + timedelta(hours=1),
                                                name="ovos-shell.night.mode.check")
            if self.sunset_time < date < self.sunrise_time:
                LOG.debug("It is night time")
                # show night clock in homescreen
                self.bus.emit(Message("phal.brightness.control.auto.night.mode.enabled"))
                # equivalent to
                # self.bus.emit(Message("ovos.homescreen.main_view.current_index.set", {"current_index": 0}))
            else:
                LOG.debug("It is day time")
                # go back to main homescreen page
                self.bus.emit(Message("ovos.homescreen.main_view.current_index.set", {"current_index": 1}))

    def stop_auto_night_mode(self):
        LOG.debug("Stopping auto night mode")
        self.config["auto_nightmode"] = False
        update_config("auto_nightmode", False)
        self.event_scheduler.cancel_scheduled_event("ovos-shell.night.mode.check")
