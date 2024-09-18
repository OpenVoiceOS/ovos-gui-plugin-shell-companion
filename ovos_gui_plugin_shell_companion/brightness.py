import datetime
import threading
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
    """ovos-shell has a fake brightness setting, it will dim the QML itself, not control the screen

    ovos-shell slider: https://github.com/OpenVoiceOS/ovos-shell/blob/master/application/qml/panel/quicksettings/BrightnessSlider.qml
        - emitted bus event from shell slider: "phal.brightness.control.set", {"brightness": fixedValue}
        - to update slider externally: "phal.brightness.control.auto.dim.update"/"phal.brightness.control.get.response", {"brightness": fixedValue}
    """

    def __init__(self, bus, config: dict):
        """
        Initialize the BrightnessManager.

        Args:
            bus: Message bus for inter-process communication.
            config: Configuration dictionary for brightness settings.
        """
        self._lock = threading.RLock()
        self.bus = bus
        self.config = config
        self.event_scheduler = EventSchedulerInterface()
        self.event_scheduler.set_id("ovos-shell")
        self.event_scheduler.set_bus(self.bus)

        self.fake_brightness = not self.config.get("external_plugin", False)  # allow delegating to external PHAL plugin
        self.default_brightness = self.config.get("default_brightness", 100)
        self._brightness_level: int = self.default_brightness
        self.sunrise_time, self.sunset_time = None, None

        self.bus.on("phal.brightness.control.get", self.handle_get_brightness)
        self.bus.on("phal.brightness.control.set", self.handle_sync_brightness)  # from external PHAL plugin
        self.bus.on("phal.brightness.control.sync", self.handle_sync_brightness)  # from GUI slider

        self.bus.on("gui.page_interaction", self.handle_undim_screen)
        self.bus.on("gui.page_gained_focus", self.handle_undim_screen)
        self.bus.on("recognizer_loop:wakeword", self.handle_undim_screen)
        self.bus.on("recognizer_loop:record_begin", self.handle_undim_screen)

        self.start()

    def start(self):
        LOG.info(f"auto dim enabled: {self.auto_dim_enabled}")
        LOG.info(f"auto night mode enabled: {self.auto_night_mode_enabled}")
        if self.auto_night_mode_enabled:
            LOG.debug("Starting auto night mode on launch")
            sunrise = self.config.get("sunrise_time", "auto")
            sunset = self.config.get("sunset_time", "auto")
            LOG.debug(f"sunrise set by user - {sunrise}")
            LOG.debug(f"sunset set by user - {sunset}")
            self.start_auto_night_mode()
        if self.auto_dim_enabled:
            LOG.debug("Starting auto dim on launch")
            self.start_auto_dim()

    ##############################################
    # brightness manager
    # TODO - allow dynamic brightness based on camera, reacting live to brightness,
    def set_brightness(self, level: int):
        """
        Set the brightness level.

        Args:
            level: Brightness level to set.
        """
        with self._lock:  # use a lock so this doesnt fire multiple times
            level = int(level)
            if level == self._brightness_level:
                return  # avoid log spam
            LOG.info(f"Brightness level set to {level}")
            self._brightness_level = level

            if self.fake_brightness:
                LOG.debug("delegating brightness change to ovos-shell fake brightness")
                # ovos-shell will apply fake brightness
                self.bus.emit(Message("phal.brightness.control.auto.dim.update",
                                      {"brightness": level}))
            else:  # will NOT update ovos-shell slider
                LOG.debug("delegating brightness change to external plugin")
                self.bus.emit(Message("phal.brightness.control.set",
                                      {"brightness": level}))
                # sync GUI slider by reporting new value
                self.bus.emit(Message("phal.brightness.control.get.response",
                                      {"brightness": level}))

    def handle_get_brightness(self, message: Message):
        """
        Handle the 'get brightness' event from the message bus.

        Args:
            message: The message received from the bus.
        """
        if not self.fake_brightness:
            # let external PHAL plugin handle it
            return
        self.bus.emit(message.response(data={"brightness": self._brightness_level}))

    def handle_sync_brightness(self, message: Message):
        """
        Handle the 'set brightness' event from the message bus.

        Args:
            message: The message received from the bus.
        """
        level = message.data.get("brightness", 100)
        LOG.debug(f"brightness level update: {level}")
        self._brightness_level = int(level)
        if message.data.get("make_default") and level != self.default_brightness:
            self.default_brightness = level
            LOG.info(f"new brightness default level: {level}")

    @property
    def auto_dim_enabled(self) -> bool:
        """
        Check if auto-dim is enabled.

        Returns:
            bool: True if auto-dim is enabled, False otherwise.
        """
        return self.config.get("auto_dim", False) or (self.auto_night_mode_enabled and self.is_night)

    def start_auto_dim(self):
        """
        Start the auto-dim functionality.
        """
        if not self.config.get("auto_dim"):
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
                LOG.debug("triggering night face clock")
                # TODO - allow other actions, new bus event to trigger night mode
                # dont hardcode homescreen night clock face
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
        if self.config.get("auto_dim"):
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
            seconds = self.config.get("auto_dim_seconds", 60)
            self.event_scheduler.schedule_event(self.handle_dim_screen,
                                                when=now_local() + timedelta(seconds=seconds),
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
        # next events, guaranteed to be both in **the future**
        self.sunrise_time, self.sunset_time = self.get_suntimes()

        # it is daytime if the sun sets today and rises tomorrow
        # n = now_local()
        # is_day = self.sunset_time.day == n.day and self.sunrise_time.day > n.day

        # is_day = self.sunset_time.day != self.sunrise_time.day
        # return not is_day

        # before midnight -> both are next day
        # after midnight -> both are current day
        return self.sunset_time.day == self.sunrise_time.day

    def start_auto_night_mode(self):
        """
        Start the auto night mode functionality.
        """
        LOG.debug("Starting auto night mode")
        if not self.config.get("auto_nightmode"):
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
            if not self.config.get("auto_dim"):
                self.start_auto_dim()

    def stop_auto_night_mode(self):
        """
        Stop the auto night mode functionality.
        """
        if self.config.get("auto_nightmode"):
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
            LOG.debug("Determining sunset/sunrise times")
            try:
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
            except:
                LOG.exception("Failed to calculate suntimes! defaulting to 06:30 and 20:30")
                if reference.hour > 7:
                    self.sunrise_time = datetime.datetime(hour=6, minute=30, month=reference.month,
                                                          day=reference.day + 1, year=reference.year)
                else:
                    self.sunrise_time = datetime.datetime(hour=6, minute=30, month=reference.month,
                                                          day=reference.day, year=reference.year)
                if reference.hour < 21:
                    self.sunset_time = datetime.datetime(hour=22, minute=30, month=reference.month,
                                                         day=reference.day, year=reference.year)
                else:
                    self.sunset_time = datetime.datetime(hour=22, minute=30, month=reference.month,
                                                         day=reference.day + 1, year=reference.year)
        # info logs
        if self.sunrise_time is None or self.sunrise_time != sunrise_time:
            LOG.info(f"Sunrise time: {sunrise_time}")
        if self.sunset_time is None or self.sunset_time != sunset_time:
            LOG.info(f"Sunset time: {sunset_time}")
        return sunrise_time, sunset_time
