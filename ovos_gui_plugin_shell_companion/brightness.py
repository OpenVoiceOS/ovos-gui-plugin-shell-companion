import datetime
import platform
import subprocess
import threading
import time
from datetime import timedelta
from distutils.spawn import find_executable
from os.path import isfile
from astral import LocationInfo
from astral.sun import sun
from ovos_bus_client import Message
from ovos_config import Configuration
from ovos_utils.events import EventSchedulerInterface
from ovos_utils.log import LOG
from ovos_utils.time import now_local
from ovos_gui_plugin_shell_companion.config import get_ovos_shell_config


class BrightnessManager:
    def __init__(self, bus):
        self.bus = bus
        self.event_scheduler = EventSchedulerInterface()
        self.event_scheduler.set_id("ovos-shell")
        self.event_scheduler.set_bus(self.bus)
        self.device_interface = "DSI"
        self.ddcutil_detected_bus = None
        self.ddcutil_brightness_code = None
        self.auto_dim_enabled = False
        self.auto_night_mode_enabled = False
        self.timer_thread = None  # TODO - use event scheduler too

        self.sunset_time = None
        self.sunrise_time = None
        self.get_sunset_time()

        self.is_auto_dim_enabled()
        self.is_auto_night_mode_enabled()

        self.discover()

        self.bus.on("phal.brightness.control.get", self.query_current_brightness)
        self.bus.on("phal.brightness.control.set", self.set_brightness_from_bus)
        self.bus.on("speaker.extension.display.auto.dim.changed", self.is_auto_dim_enabled)
        self.bus.on("speaker.extension.display.auto.nightmode.changed", self.is_auto_night_mode_enabled)
        self.bus.on("gui.page_interaction", self.undim_display)
        self.bus.on("gui.page_gained_focus", self.undim_display)
        self.bus.on("recognizer_loop:wakeword", self.undim_display)
        self.bus.on("recognizer_loop:record_begin", self.undim_display)

    def init_event_scheduler(self):
        self.event_scheduler = EventSchedulerInterface()
        self.event_scheduler.set_id(self.name)
        self.event_scheduler.set_bus(self.bus)

    @staticmethod
    def validate():
        if not platform.machine().startswith("arm"):
            return False
        # check if needed utils installed
        vcgencmd = find_executable("vcgencmd") or isfile("/opt/vc/bin/vcgencmd")
        ddcutil = find_executable("ddcutil") or isfile("/usr/bin/ddcutil")
        if not (vcgencmd or ddcutil):
            return False

    #### brightness manager - TODO generic non rpi support
    # Check if the auto dim is enabled
    def is_auto_dim_enabled(self, message=None):
        LOG.debug("Checking if auto dim is enabled")
        display_config = get_ovos_shell_config()
        self.auto_dim_enabled = display_config.get("auto_dim", False)
        if self.auto_dim_enabled:
            self.start_auto_dim()
        else:
            self.stop_auto_dim()

    # Discover the brightness control device interface (HDMI / DSI) on the Raspberry PI
    def discover(self):
        try:
            LOG.info("Discovering brightness control device interface")
            proc = subprocess.Popen(["/opt/vc/bin/vcgencmd",
                                     "get_config", "display_default_lcd"], stdout=subprocess.PIPE)
            if proc.stdout.read().decode("utf-8").strip() == "1":
                self.device_interface = "DSI"
            else:
                self.device_interface = "HDMI"
            LOG.info("Brightness control device interface is {}".format(
                self.device_interface))

            if self.device_interface == "HDMI":
                proc_detect = subprocess.Popen(
                    ["/usr/bin/ddcutil", "detect"], stdout=subprocess.PIPE)

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
                        ["/usr/bin/ddcutil", "getvcp", "known", "--bus", self.ddcutil_detected_bus],
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

    def get_brightness(self):
        LOG.info("Getting current brightness level")
        if self.device_interface == "HDMI":
            proc_fetch_vcp = subprocess.Popen(
                ["/usr/bin/ddcutil", "getvcp", self.ddcutil_brightness_code, "--bus", self.ddcutil_detected_bus],
                stdout=subprocess.PIPE)
            for line in proc_fetch_vcp.stdout:
                if "current value" in line.decode("utf-8"):
                    brightness_level = line.decode(
                        "utf-8").split("current value = ")[1].split(",")[0].strip()
                    return int(brightness_level)

        if self.device_interface == "DSI":
            proc_fetch_vcp = subprocess.Popen(
                ["cat", "/sys/class/backlight/rpi_backlight/actual_brightness"], stdout=subprocess.PIPE)
            for line in proc_fetch_vcp.stdout:
                brightness_level = line.decode("utf-8").strip()
                return int(brightness_level)

    def query_current_brightness(self, message):
        current_brightness = self.get_brightness()
        if self.device_interface == "HDMI":
            self.bus.emit(message.response(
                data={"brightness": current_brightness}))
        elif self.device_interface == "DSI":
            brightness_percentage = int((current_brightness / 255) * 100)
            self.bus.emit(message.response(
                data={"brightness": brightness_percentage}))

        # Set the brightness level

    def set_brightness(self, level):
        LOG.debug("Setting brightness level")
        if self.device_interface == "HDMI":
            subprocess.Popen(["/usr/bin/ddcutil", "setvcp", self.ddcutil_brightness_code,
                              "--bus", self.ddcutil_detected_bus, str(level)])
        elif self.device_interface == "DSI":
            subprocess.call(
                f"echo {level} > /sys/class/backlight/rpi_backlight/brightness", shell=True)

        LOG.info("Brightness level set to {}".format(level))

    def set_brightness_from_bus(self, message):
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

    def start_auto_dim(self):
        LOG.debug("Starting auto dim")
        self.timer_thread = threading.Thread(target=self.auto_dim_timer)
        self.timer_thread.start()

    def auto_dim_timer(self):
        while self.auto_dim_enabled:
            time.sleep(60)
            LOG.debug("Adjusting brightness automatically")
            if self.device_interface == "HDMI":
                current_brightness = 100
            if self.device_interface == "DSI":
                current_brightness = 255

            self.bus.emit(
                Message("phal.brightness.control.auto.dim.update", {"brightness": 20}))
            self.set_brightness(20)

    def stop_auto_dim(self):
        LOG.debug("Stopping Auto Dim")
        self.auto_dim_enabled = False
        if self.timer_thread:
            self.timer_thread.join()

    def restart_auto_dim(self):
        LOG.debug("Restarting Auto Dim")
        self.stop_auto_dim()
        self.auto_dim_enabled = True
        self.start_auto_dim()

    def undim_display(self, message=None):
        if self.auto_dim_enabled:
            LOG.debug("Undimming display on interaction")
            if self.device_interface == "HDMI":
                self.set_brightness(100)
            if self.device_interface == "DSI":
                self.set_brightness(255)
            self.bus.emit(
                Message("phal.brightness.control.auto.dim.update", {"brightness": "100"}))
            self.restart_auto_dim()
        else:
            pass

        ##### AUTO NIGHT MODE HANDLING #####

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

        # check sunset times again in 24 hours
        self.event_scheduler.schedule_event(self.get_sunset_time,
                                            when=now_time + timedelta(hours=24),
                                            name="ovos-shell.suntimes.check")

    def start_auto_night_mode(self, message=None):
        if self.auto_night_mode_enabled:
            date = now_local()
            self.event_scheduler.schedule_event(self.start_auto_night_mode,
                                                when=date + timedelta(hours=1),
                                                name="ovos-shell.night.mode.check")
            if self.sunset_time < date < self.sunrise_time:
                LOG.debug("It is night time")
                self.bus.emit(Message("phal.brightness.control.auto.night.mode.enabled"))
            else:
                LOG.debug("It is day time")
                # TODO - implement this message in shell / check if it exists
                # i just made it up without checking
                self.bus.emit(Message("phal.brightness.control.auto.night.mode.disabled"))

    def stop_auto_night_mode(self):
        LOG.debug("Stopping auto night mode")
        self.auto_night_mode_enabled = False
        self.event_scheduler.cancel_scheduled_event("ovos-shell.night.mode.check")

    def is_auto_night_mode_enabled(self):
        display_config = get_ovos_shell_config()
        self.auto_night_mode_enabled = display_config.get("auto_nightmode", False)

        if self.auto_night_mode_enabled:
            self.start_auto_night_mode()
        else:
            self.stop_auto_night_mode()
