import platform
from os.path import join, dirname

from ovos_bus_client import Message
from ovos_bus_client.apis.gui import GUIInterface
from ovos_bus_client.client import MessageBusClient
from ovos_config.config import Configuration
from ovos_plugin_manager.templates.gui import GUIExtension
from ovos_utils import network_utils
from ovos_utils.log import LOG

from ovos_gui_plugin_shell_companion.brightness import BrightnessManager
from ovos_gui_plugin_shell_companion.color_manager import ColorManager
from ovos_gui_plugin_shell_companion.helpers import ConfigUIManager
from ovos_gui_plugin_shell_companion.wigets import WidgetManager


class OVOSShellCompanionExtension(GUIExtension):
    """OVOS-shell Extension: This extension is responsible for managing the Smart Speaker
    specific GUI behaviours. This extension adds support for Homescreens and Homescreen Mangement.
    It handles all events sent from ovos-shell

    Args:
        config: plugin configuration
        bus: MessageBus instance
        gui: GUI instance
        preload_gui (bool): load GUI skills even if gui client not connected
        permanent (bool): disable unloading of GUI skills on gui client disconnections
    """

    def __init__(self, config: dict, bus: MessageBusClient = None,
                 gui: GUIInterface = None,
                 preload_gui=False, permanent=True):
        config["homescreen_supported"] = True
        res_dir = join(dirname(__file__), "res")
        gui = gui or GUIInterface("ovos_gui_plugin_shell_companion",
                                  bus=bus, config=Configuration(),
                                  ui_directories={"qt5": join(res_dir, "ui"),
                                                  "qt6": join(res_dir, "ui6")})
        if not gui.ui_directories:
            LOG.info(f"Setting default qt5 resource directory to: {res_dir}")
            gui.ui_directories["qt5"] = res_dir
        LOG.info("OVOS Shell: Initializing")
        super().__init__(config=config, bus=bus, gui=gui,
                         preload_gui=preload_gui, permanent=permanent)
        self.about_page_data = []
        self.build_initial_about_page_data()

        self.color_manager = ColorManager(self.bus)
        self.widgets = WidgetManager(self.bus)
        self.bright = BrightnessManager(self.bus, self.config)
        self.cui = ConfigUIManager(self.bus)

    def register_bus_events(self):
        # TODO - solve this namespace mess and unify things as much as possible
        self.bus.on("mycroft.gui.screen.close", self.handle_remove_namespace)
        self.bus.on("system.display.homescreen", self.handle_system_display_homescreen)

        self.bus.on("mycroft.device.settings", self.handle_device_settings)
        self.bus.on("ovos.phal.configuration.provider.get.response", self.display_advanced_config_for_group)
        self.bus.on("ovos.phal.configuration.provider.list.groups.response", self.display_advanced_config_groups)
        self.bus.on("smartspeaker.extension.extend.about", self.extend_about_page_data_from_event)

        self.gui.register_handler("mycroft.device.settings", self.handle_device_settings)
        self.gui.register_handler("mycroft.device.settings.homescreen", self.handle_device_homescreen_settings)
        self.gui.register_handler("mycroft.device.settings.ssh", self.handle_device_ssh_settings)
        self.gui.register_handler("mycroft.device.settings.developer", self.handle_device_developer_settings)
        self.gui.register_handler("mycroft.device.show.idle", self.handle_show_homescreen)
        self.gui.register_handler("mycroft.device.settings.customize", self.handle_device_customize_settings)
        self.gui.register_handler("mycroft.device.settings.create.theme", self.handle_device_create_theme)
        self.gui.register_handler("mycroft.device.settings.about.page", self.handle_device_about_page)
        self.gui.register_handler("mycroft.device.settings.display", self.handle_device_display_settings)
        self.gui.register_handler("mycroft.device.settings.factory", self.handle_device_display_factory)

        # Display settings
        self.gui.register_handler("speaker.extension.display.set.auto.dim",
                                  self.handle_display_auto_dim_config_set)
        self.gui.register_handler("speaker.extension.display.set.auto.nightmode",
                                  self.handle_display_auto_nightmode_config_set)

    def handle_remove_namespace(self, message):
        LOG.debug("Clearing namespace (mycroft.gui.screen.close)")
        get_skill_namespace = message.data.get("skill_id", "")
        if get_skill_namespace:
            self.bus.emit(Message("gui.clear.namespace",
                                  {"__from": get_skill_namespace}))

    def handle_system_display_homescreen(self, message):
        self.homescreen_manager.show_homescreen()

    def handle_device_settings(self, message):
        """ Display device settings page. """
        self.gui["state"] = "settings/settingspage"
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_device_homescreen_settings(self, message):
        """
        display homescreen settings page
        """
        screens = self.homescreen_manager.homescreens
        self.gui["idleScreenList"] = {"screenBlob": screens}
        self.gui["selectedScreen"] = self.homescreen_manager.get_active_homescreen()
        self.gui["state"] = "settings/homescreen_settings"
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_device_ssh_settings(self, message):
        """
        display ssh settings page
        """
        self.gui["state"] = "settings/ssh_settings"
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_set_homescreen(self, message):
        """
        Set the homescreen to the selected screen
        """
        homescreen_id = message.data.get("homescreen_id", "")
        if homescreen_id:
            self.homescreen_manager.set_active_homescreen(homescreen_id)

    def handle_show_homescreen(self, message):
        self.homescreen_manager.show_homescreen()

    def handle_device_developer_settings(self, message):
        self.gui['state'] = 'settings/developer_settings'

    def handle_device_customize_settings(self, message):
        self.gui['state'] = 'settings/customize_settings'
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_device_create_theme(self, message):
        self.gui['state'] = 'settings/customize_theme'
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_device_display_factory(self, message):
        self.gui['state'] = 'settings/factory_settings'
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_device_display_settings(self, message):
        LOG.debug(f"Display settings: {self.config}")
        self.gui['state'] = 'settings/display_settings'
        # wallpaper_rotation data is determined via Messagebus in Qt directly
        self.gui['display_auto_dim'] = self.config.get("auto_dim", False)
        self.gui['display_auto_nightmode'] = self.config.get("auto_nightmode", False)
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_device_about_page(self, message):
        # TODO: Move `system_information` generation to util method
        uname_info = platform.uname()
        system_information = {"display_list": self.about_page_data}
        self.gui['state'] = 'settings/about_page'
        self.gui['system_info'] = system_information
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_display_auto_dim_config_set(self, message):
        auto_dim = message.data.get("auto_dim", False)
        if auto_dim:
            self.bright.start_auto_dim()
        else:
            self.bright.stop_auto_dim()

    def handle_display_auto_nightmode_config_set(self, message):
        auto_nightmode = message.data.get("auto_nightmode", False)
        if auto_nightmode:
            self.bright.start_auto_night_mode()
        else:
            self.bright.stop_auto_night_mode()

    def display_advanced_config_for_group(self, message=None):
        group_meta = message.data.get("settingsMetaData")
        group_name = message.data.get("groupName")
        self.gui["groupName"] = group_name
        self.gui["groupConfigurationData"] = group_meta
        self.gui['state'] = 'settings/configuration_generator_display'
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def display_advanced_config_groups(self, message=None):
        groups_list = message.data.get("groups")
        self.gui["groupList"] = groups_list
        self.gui['state'] = 'settings/configuration_groups_display'
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def build_initial_about_page_data(self):
        uname_info = platform.uname()
        try:
            from ovos_core.version import OVOS_VERSION_STR as version
        except ImportError:
            version = "unknown"
        self.about_page_data.append({"display_key": "Kernel Version", "display_value": uname_info[2]})
        self.about_page_data.append({"display_key": "Core Version", "display_value": version})
        self.about_page_data.append({"display_key": "Python Version", "display_value": platform.python_version()})
        self.about_page_data.append({"display_key": "Local Address", "display_value": network_utils.get_ip()})

    def check_about_page_data_contains_key(self, key):
        for item in self.about_page_data:
            if item["display_key"] == key:
                return True
        return False

    def add_about_page_data(self, key, value):
        if not self.check_about_page_data_contains_key(key):
            self.about_page_data.append({"display_key": key, "display_value": value})
        else:
            for item in self.about_page_data:
                if item["display_key"] == key:
                    item["display_value"] = value
                    break

    def extend_about_page_data_from_event(self, message=None):
        extended_list = message.data.get("display_list")
        for item in extended_list:
            self.add_about_page_data(item["display_key"], item["display_value"])
