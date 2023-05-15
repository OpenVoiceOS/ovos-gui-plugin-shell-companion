import datetime
import json
import os
import platform
import re
import subprocess
import threading
import time
from datetime import timedelta
from distutils.spawn import find_executable
from os.path import exists, join, isfile

from json_database import JsonStorage
from ovos_bus_client import Message
from ovos_config import Configuration
from ovos_config.config import update_mycroft_config
from ovos_plugin_manager.templates.gui import GUIExtension
from ovos_utils import network_utils
from ovos_utils.events import EventSchedulerInterface
from ovos_utils.fingerprinting import get_mycroft_version
from ovos_utils.log import LOG
from ovos_utils.time import now_local
from ovos_utils.xdg_utils import xdg_config_home, xdg_data_home


class OVOSShellCompanionExtension(GUIExtension):
    """OVOS-shell Extension: This extension is responsible for managing the Smart Speaker
    specific GUI behaviours. This extension adds support for Homescreens and Homescreen Mangement.
    It handles all events sent from ovos-shell

    Args:
        bus: MessageBus instance
        gui: GUI instance
        preload_gui (bool): load GUI skills even if gui client not connected
        permanent (bool): disable unloading of GUI skills on gui client disconnections
    """

    def __init__(self, bus, gui, config, preload_gui=False, permanent=True):
        config["homescreen_supported"] = True
        LOG.info("OVOS Shell: Initializing")
        super().__init__(bus, gui, config, preload_gui, permanent)
        # Paths to find the local display config
        self.display_config_path_local = join(xdg_config_home(), "OvosDisplay.conf")
        self.display_config_path_system = "/etc/xdg/OvosDisplay.conf"
        self.local_display_config = JsonStorage(self.display_config_path_local)
        self.about_page_data = []

        if not exists(self.display_config_path_local):
            self.handle_display_config_load()

        self.build_initial_about_page_data()
        self.init_event_scheduler()
        self.init_color_manager()
        self.init_notifications_widgets()
        self.init_config_provider()
        self.init_brightness_ctrl()

    def init_event_scheduler(self):
        self.event_scheduler = EventSchedulerInterface()
        self.event_scheduler.set_id(self.name)
        self.event_scheduler.set_bus(self.bus)

    def init_notifications_widgets(self):
        self.bus.on("ovos.notification.api.request.storage.model",
                    self.notificationAPI_update_storage_model)
        self.bus.on("ovos.notification.api.set",
                    self.__notificationAPI_handle_display_notification)
        self.bus.on("ovos.notification.api.pop.clear",
                    self.__notificationAPI_handle_clear_notification_data)
        self.bus.on("ovos.notification.api.pop.clear.delete",
                    self.__notificationAPI_handle_clear_delete_notification_data)
        self.bus.on("ovos.notification.api.storage.clear",
                    self.__notificationAPI_handle_clear_notification_storage)
        self.bus.on("ovos.notification.api.storage.clear.item",
                    self.__notificationAPI_handle_clear_notification_storage_item)
        self.bus.on("ovos.notification.api.set.controlled",
                    self.__notificationAPI_handle_display_controlled)
        self.bus.on("ovos.notification.api.remove.controlled",
                    self.__notificationAPI_handle_remove_controlled)

        self.bus.on("ovos.widgets.display",
                    self.__widgetsAPI_handle_handle_widget_display)
        self.bus.on("ovos.widgets.remove",
                    self.__widgetsAPI_handle_handle_widget_remove)
        self.bus.on("ovos.widgets.update",
                    self.__widgetsAPI_handle_handle_widget_update)

        # Notifications Bits
        self.__notificationAPI_notifications_model = []
        self.__notificationAPI_notifications_storage_model = []

        LOG.info("Notification & Widgets Plugin Initalized")

    def init_config_provider(self):
        self.settings_meta = {}
        self.build_settings_meta()

        self.bus.on("ovos.phal.configuration.provider.list.groups",
                    self.list_groups)
        self.bus.on("ovos.phal.configuration.provider.get",
                    self.get_settings_meta)
        self.bus.on("ovos.phal.configuration.provider.set",
                    self.set_settings_in_config)

    def init_brightness_ctrl(self):
        if not platform.machine().startswith("arm"):
            return False
        # check if needed utils installed
        vcgencmd = find_executable("vcgencmd") or isfile("/opt/vc/bin/vcgencmd")
        ddcutil = find_executable("ddcutil") or isfile("/usr/bin/ddcutil")
        if not (vcgencmd or ddcutil):
            return False

        self.device_interface = "DSI"
        self.ddcutil_detected_bus = None
        self.ddcutil_brightness_code = None
        self.auto_dim_enabled = False
        self.auto_night_mode_enabled = False
        self.timer_thread = None  # TODO - use event scheduler too

        self.get_sunset_time()

        self.is_auto_dim_enabled()
        self.is_auto_night_mode_enabled()

        self.discover()

        self.bus.on("phal.brightness.control.get",
                    self.query_current_brightness)
        self.bus.on("phal.brightness.control.set",
                    self.set_brightness_from_bus)
        self.bus.on("speaker.extension.display.auto.dim.changed",
                    self.is_auto_dim_enabled)
        self.bus.on("speaker.extension.display.auto.nightmode.changed",
                    self.is_auto_night_mode_enabled)
        self.bus.on("gui.page_interaction",
                    self.undim_display)
        self.bus.on("gui.page_gained_focus",
                    self.undim_display)
        self.bus.on("recognizer_loop:wakeword",
                    self.undim_display)
        self.bus.on("recognizer_loop:record_begin",
                    self.undim_display)

    def init_color_manager(self):
        self.theme_path = join(xdg_data_home(), "OVOS", "ColorSchemes")
        self.bus.on("ovos.shell.gui.color.scheme.generate", self.generate_theme)
        self.bus.on("ovos.theme.get", self.provide_theme)
        self.provide_theme(Message("ovos.theme.get"))  # Emit theme on init

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
        self.gui.register_handler("speaker.extension.display.set.wallpaper.rotation",
                                  self.handle_display_wallpaper_rotation_config_set)
        self.gui.register_handler("speaker.extension.display.set.auto.dim", self.handle_display_auto_dim_config_set)
        self.gui.register_handler("speaker.extension.display.set.auto.nightmode",
                                  self.handle_display_auto_nightmode_config_set)

    def handle_remove_namespace(self, message):
        LOG.info("Got Clear Namespace Event In Skill")
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
        LOG.info("Display settings")
        LOG.info(self.local_display_config)

        self.gui['state'] = 'settings/display_settings'
        self.gui['display_wallpaper_rotation'] = self.local_display_config.get("wallpaper_rotation", False)
        self.gui['display_auto_dim'] = self.local_display_config.get("auto_dim", False)
        self.gui['display_auto_nightmode'] = self.local_display_config.get("auto_nightmode", False)
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_device_about_page(self, message):
        # TODO: Move `system_information` generation to util method
        uname_info = platform.uname()
        system_information = {"display_list": self.about_page_data}
        self.gui['state'] = 'settings/about_page'
        self.gui['system_info'] = system_information
        self.gui.show_page("SYSTEM_AdditionalSettings.qml", override_idle=True)

    def handle_display_wallpaper_rotation_config_set(self, message):
        wallpaper_rotation = message.data.get("wallpaper_rotation", False)
        self.local_display_config["wallpaper_rotation"] = wallpaper_rotation
        self.local_display_config.store()
        self.bus.emit(Message("speaker.extension.display.wallpaper.rotation.changed"))

    def handle_display_auto_dim_config_set(self, message):
        auto_dim = message.data.get("auto_dim", False)
        self.local_display_config["auto_dim"] = auto_dim
        self.local_display_config.store()
        self.bus.emit(Message("speaker.extension.display.auto.dim.changed"))

    def handle_display_auto_nightmode_config_set(self, message):
        auto_nightmode = message.data.get("auto_nightmode", False)
        self.local_display_config["auto_nightmode"] = auto_nightmode
        self.local_display_config.store()
        self.bus.emit(Message("speaker.extension.display.auto.nightmode.changed"))

    def handle_display_config_load(self):
        if exists(self.display_config_path_system):
            LOG.info("Loading display config from system")
            with open(self.display_config_path_system, "r") as f:
                writeable_conf = json.load(f)
                self.local_display_config["wallpaper_rotation"] = writeable_conf["wallpaper_rotation"]
                self.local_display_config["auto_dim"] = writeable_conf["auto_dim"]
                self.local_display_config["auto_nightmode"] = writeable_conf["auto_nightmode"]
                self.local_display_config.store()

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
        version = get_mycroft_version() or "unknown"
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

    #### color scheme manager
    def generate_theme(self, message):
        if "primaryColor" not in message.data or "secondaryColor" not in message.data or "textColor" not in message.data:
            return

        if "theme_name" not in message.data:
            return

        theme_name = message.data["theme_name"]
        file_name = theme_name.replace(" ", "_").lower() + ".json"

        LOG.info(f"Creating ColorScheme For {theme_name}")

        if not os.path.exists(self.theme_path):
            os.makedirs(self.theme_path)

        if file_name in os.listdir(self.theme_path):
            os.remove(join(self.theme_path, file_name))

        theme_file = open(join(self.theme_path, file_name), "w")
        theme_file.write("{\n")
        theme_file.write('"name":"' + theme_name + '",\n')
        theme_file.write('"primaryColor":"' + message.data["primaryColor"] + '",\n')
        theme_file.write('"secondaryColor":"' + message.data["secondaryColor"] + '",\n')
        theme_file.write('"textColor":"' + message.data["textColor"] + '"\n')
        theme_file.write("}\n")
        theme_file.close()
        self.bus.emit(Message("ovos.shell.gui.color.scheme.generated",
                              {"theme_name": theme_name,
                               "theme_path": self.theme_path}))

    def provide_theme(self, message):
        file_name = "OvosTheme"
        xdg_system_path = "/etc/xdg"
        try:
            if file_name in os.listdir(xdg_config_home()):
                theme_file = open(join(xdg_config_home(), file_name), "r")
                theme = theme_file.read()
                theme_file.close()
            elif file_name in os.listdir(xdg_system_path):
                theme_file = open(join(xdg_system_path, file_name), "r")
                theme = theme_file.read()
                theme_file.close()

            name = re.search(r"name=(.*)", theme).group(1)
            primaryColor = re.search(r"primaryColor=(.*)", theme).group(1)
            secondaryColor = re.search(r"secondaryColor=(.*)", theme).group(1)
            textColor = re.search(r"textColor=(.*)", theme).group(1)

            self.bus.emit(message.response({"name": name,
                                            "primaryColor": primaryColor,
                                            "secondaryColor": secondaryColor,
                                            "textColor": textColor}))

        except Exception as e:
            LOG.error(e)
            return

    #### notifications widget manager
    def notificationAPI_update_storage_model(self, message=None):
        """ Update Notification Storage Model """
        LOG.info("Notification API: Update Notification Storage Model")
        self.bus.emit(Message("ovos.notification.update_storage_model", data={"notification_model": {
            "storedmodel": self.__notificationAPI_notifications_storage_model,
            "count": len(self.__notificationAPI_notifications_storage_model)
        }}))

    def __notificationAPI_handle_display_notification(self, message):
        """ Get Notification & Action """
        LOG.info("Notification API: Display Notification")
        notification_message = {
            "duration": message.data.get("duration", 10),
            "sender": message.data.get("sender", ""),
            "text": message.data.get("text", ""),
            "action": message.data.get("action", ""),
            "type": message.data.get("type", ""),
            "style": message.data.get("style", "info"),
            "callback_data": message.data.get("callback_data", {}),
            "timestamp": time.time()
        }
        if notification_message not in self.__notificationAPI_notifications_model:
            self.__notificationAPI_notifications_model.append(
                notification_message)
            time.sleep(2)
            self.bus.emit(Message("ovos.notification.update_counter", data={
                "notification_counter": len(self.__notificationAPI_notifications_model)}))
            self.bus.emit(Message("ovos.notification.notification_data", data={
                "notification": notification_message}))
            self.bus.emit(Message("ovos.notification.show"))

    def __notificationAPI_handle_display_controlled(self, message):
        """ Get Controlled Notification """
        notification_message = {
            "sender": message.data.get("sender", ""),
            "text": message.data.get("text", ""),
            "style": message.data.get("style", "info"),
            "timestamp": time.time()
        }
        self.bus.emit(Message("ovos.notification.controlled.type.show",
                              data={"notification": notification_message}))

    def __notificationAPI_handle_remove_controlled(self, message):
        """ Remove Controlled Notification """
        self.bus.emit(Message("ovos.notification.controlled.type.remove"))

    def __notificationAPI_handle_clear_notification_data(self, message):
        """ Clear Pop Notification """
        notification_data = message.data.get("notification", "")
        self.__notificationAPI_notifications_storage_model.append(
            notification_data)
        for i in range(len(self.__notificationAPI_notifications_model)):
            if (
                    self.__notificationAPI_notifications_model[i]["sender"] == notification_data["sender"]
                    and self.__notificationAPI_notifications_model[i]["text"] == notification_data["text"]
            ):
                if not len(self.__notificationAPI_notifications_model) > 0:
                    del self.__notificationAPI_notifications_model[i]
                    self.__notificationAPI_notifications_model = []
                else:
                    del self.__notificationAPI_notifications_model[i]
                break
        self.notificationAPI_update_storage_model()
        self.bus.emit(Message("ovos.notification.notification_data",
                              data={"notification": {}}))

    def __notificationAPI_handle_clear_delete_notification_data(self, message):
        """ Clear Pop Notification & Delete Notification data """
        LOG.info(
            "Notification API: Clear Pop Notification & Delete Notification data")
        notification_data = message.data.get("notification", "")

        for i in range(len(self.__notificationAPI_notifications_model)):
            if (
                    self.__notificationAPI_notifications_model[i]["sender"] == notification_data["sender"]
                    and self.__notificationAPI_notifications_model[i]["text"] == notification_data["text"]
            ):
                if not len(self.__notificationAPI_notifications_model) > 0:
                    del self.__notificationAPI_notifications_model[i]
                    self.__notificationAPI_notifications_model = []
                else:
                    del self.__notificationAPI_notifications_model[i]
                break

    def __notificationAPI_handle_clear_notification_storage(self, _):
        """ Clear All Notification Storage Model """
        self.__notificationAPI_notifications_storage_model = []
        self.notificationAPI_update_storage_model()

    def __notificationAPI_handle_clear_notification_storage_item(
            self, message):
        """ Clear Single Item From Notification Storage Model """
        LOG.info(
            "Notification API: Clear Single Item From Notification Storage Model")
        notification_data = message.data.get("notification", "")
        for i in range(
                len(self.__notificationAPI_notifications_storage_model)):
            if (
                    self.__notificationAPI_notifications_storage_model[i]["sender"]
                    == notification_data["sender"]
                    and self.__notificationAPI_notifications_storage_model[i]["text"]
                    == notification_data["text"]
            ):
                self.__notificationAPI_notifications_storage_model.pop(i)

        self.notificationAPI_update_storage_model()

        # Skills that can display widgets on the homescreen are: Timer, Alarm and
        # Media Player

    def __widgetsAPI_handle_handle_widget_display(self, message):
        """ Handle Widget Display """
        LOG.info("Widgets API: Handle Widget Display")
        widget_data = message.data.get("data", "")
        widget_type = message.data.get("type", "")
        if widget_type == "timer":
            self.bus.emit(Message("ovos.widgets.timer.display", data={
                "widget": widget_data}))
        elif widget_type == "alarm":
            self.bus.emit(Message("ovos.widgets.alarm.display", data={
                "widget": widget_data}))
        elif widget_type == "audio":
            self.bus.emit(Message("ovos.widgets.media.display", data={
                "widget": widget_data}))

    def __widgetsAPI_handle_handle_widget_remove(self, message):
        """ Handle Widget Remove """
        LOG.info("Widgets API: Handle Widget Remove")
        widget_data = message.data.get("data", "")
        widget_type = message.data.get("type", "")
        if widget_type == "timer":
            self.bus.emit(Message("ovos.widgets.timer.remove"))
        elif widget_type == "alarm":
            self.bus.emit(Message("ovos.widgets.alarm.remove"))
        elif widget_type == "audio":
            self.bus.emit(Message("ovos.widgets.media.remove"))

    def __widgetsAPI_handle_handle_widget_update(self, message):
        """ Handle Widget Update """
        LOG.info("Widgets API: Handle Widget Update")
        widget_data = message.data.get("data", "")
        widget_type = message.data.get("type", "")
        if widget_type == "timer":
            self.bus.emit(Message("ovos.widgets.timer.update", data={
                "widget": widget_data}))
        elif widget_type == "alarm":
            self.bus.emit(Message("ovos.widgets.alarm.update", data={
                "widget": widget_data}))
        elif widget_type == "audio":
            self.bus.emit(Message("ovos.widgets.media.update", data={
                "widget": widget_data}))

    #### config provider
    def build_settings_meta(self):
        readable_config = Configuration()
        misc = {}
        new_config = {}

        for key in readable_config:
            if type(readable_config[key]) is not dict:
                misc[key] = readable_config[key]
            if type(readable_config[key]) is dict:
                new_config[key] = readable_config[key]

        new_config["misc"] = misc

        settings_meta_list = []
        for key in new_config:
            group_meta = {}
            group_meta["group_name"] = key.lower()
            group_meta["group_label"] = key.capitalize().replace("_", " ")
            group_meta["group_sections"] = []

            general = {}
            general["section_name"] = f"{key}_general"
            general["section_label"] = "General Configuration"
            general["section_fields"] = []
            general["section_description"] = "Configure the general settings of this module"

            subsections = []

            for subkey in new_config[key]:
                field = self.generate_field(subkey, type(
                    new_config[key][subkey]), new_config[key][subkey], group_name=key)

                if field[1] == "field":
                    general["section_fields"].append(field[0])
                elif field[1] == "obj":
                    subsections.append(field[0])
                    if field[2] is not None:
                        for sub_nested_section in field[2]:
                            if sub_nested_section is not None:
                                if len(sub_nested_section["section_fields"]) > 0:
                                    group_meta["group_sections"].append(
                                        sub_nested_section)

            if len(general["section_fields"]) > 0:
                group_meta["group_sections"].append(general)
            group_meta["group_sections"].extend(subsections)

            for section in group_meta["group_sections"]:
                if len(section["section_fields"]) == 0:
                    group_meta["group_sections"].remove(section)

            settings_meta_list.append(group_meta)

        self.settings_meta["settings"] = settings_meta_list

        # For Debug Write File To Disk
        with open("/tmp/settings_meta.json", "w") as f:
            f.write(json.dumps(self.settings_meta))

    def generate_section(self, section_name, value, group_name):
        subsection = {}
        subsection["section_name"] = section_name
        subsection["section_label"] = section_name.capitalize().replace(
            "_", " ")
        subsection["section_fields"] = []
        subsection["section_description"] = self.populate_section_description(
            section_name, group_name)
        nested_sections = []
        for key in value:
            if type(value[key]) != dict:
                field = self.generate_field(
                    key, type(value[key]), value[key], group_name=group_name)
                subsection["section_fields"].append(field[0])
            else:
                sub_nested_sections = self.generate_section(
                    key, value[key], group_name=group_name)
                if type(sub_nested_sections) == list:
                    for sub_nested_section in sub_nested_sections:
                        if sub_nested_section is not None:
                            if type(sub_nested_section) == list:
                                for sub_nested_section_item in sub_nested_section:
                                    nested_sections.append(
                                        sub_nested_section_item)
                            elif type(sub_nested_section) == dict:
                                nested_sections.append(sub_nested_section)

                else:
                    nested_sections.append(sub_nested_sections)

        if len(nested_sections) > 0:
            return [subsection, nested_sections]
        else:
            return [subsection, None]

    def generate_field(self, key, type, value, group_name):
        type = type
        section_key = key
        if type is dict:
            type_str = "obj"
            generated_section_data = self.generate_section(
                section_key, value, group_name)
            generated_section = generated_section_data[0]
            if generated_section_data[1] is not None:
                nested_sections = generated_section_data[1]
                return [generated_section, type_str, nested_sections]
            else:
                return [generated_section, type_str, None]

        else:
            value = value
            field = {}
            field["field_name"] = key
            field["field_label"] = key.capitalize().replace("_", " ")
            field["field_type"] = type.__name__
            field["field_value"] = value
            field["field_description"] = self.populate_field_description(
                field["field_name"], group_name)

            type_str = "field"
            return [field, type_str]

    def populate_section_description(self, section_name, section_group):
        with open(os.path.dirname(os.path.realpath(__file__)) + "/descriptions.json", "r") as f:
            description_json = json.load(f)
            descriptions = description_json["collection"]
            for description in descriptions:
                if description["type"] == "section":
                    if description["key"] == section_name and description["group"] == section_group:
                        return description["value"]
        return ""

    def populate_field_description(self, field_name, field_group):
        with open(os.path.dirname(os.path.realpath(__file__)) + "/descriptions.json", "r") as f:
            description_json = json.load(f)
            descriptions = description_json["collection"]
            for description in descriptions:
                if description["type"] == "field":
                    if description["key"] == field_name and description["group"] == field_group:
                        return description["value"]
        return ""

    def list_groups(self, message=None):
        group_names = []
        for group in self.settings_meta["settings"]:
            group_names.append(group["group_name"])

        self.bus.emit(Message("ovos.phal.configuration.provider.list.groups.response", {"groups": group_names}))

    def get_settings_meta(self, message=None):
        group_request = message.data.get("group")
        LOG.info(f"Getting settings meta for section: {group_request}")

        for group in self.settings_meta["settings"]:
            if group["group_name"] == group_request:
                LOG.info(f"Found group: {group_request}")
                self.bus.emit(Message("ovos.phal.configuration.provider.get.response", {
                    "settingsMetaData": group, "groupName": group_request}))

    def update_settings_meta(self, group_request):
        self.settings_meta = {}
        self.build_settings_meta()
        for group in self.settings_meta["settings"]:
            if group["group_name"] == group_request:
                self.bus.emit(Message("ovos.phal.configuration.provider.get.response", {
                    "settingsMetaData": group, "groupName": group_request}))

    def find_and_update_config(self, key, config, old_config_value):
        for item in config:
            if item["field_name"] == key:
                return item["field_value"]
        else:
            return old_config_value

    def set_settings_in_config(self, message=None):
        group_name = message.data.get("group_name")
        configuration = message.data.get("configuration")
        mycroft_config = Configuration()

        misc = {}
        new_config = {}

        for key in mycroft_config:
            if type(mycroft_config[key]) is not dict:
                misc[key] = mycroft_config[key]
            if type(mycroft_config[key]) is dict:
                new_config[key] = mycroft_config[key]

        new_config["misc"] = misc

        if group_name != "misc":
            for key in new_config:
                if key == group_name:
                    for subkey in new_config[key]:
                        if type(new_config[key][subkey]) is dict:
                            for subkey2 in new_config[key][subkey]:
                                new_config[key][subkey][subkey2] = self.find_and_update_config(
                                    subkey2, configuration, new_config[key][subkey][subkey2])
                                if type(new_config[key][subkey][subkey2]) is dict:
                                    for subkey3 in new_config[key][subkey][subkey2]:
                                        new_config[key][subkey][subkey2][subkey3] = self.find_and_update_config(
                                            subkey3, configuration, new_config[key][subkey][subkey2][subkey3])
                        else:
                            new_config[key][subkey] = self.find_and_update_config(
                                subkey, configuration, new_config[key][subkey])

                    mycroft_config_group = {}
                    mycroft_config_group[key] = new_config[key]

                    update_mycroft_config(mycroft_config_group)

        elif group_name == "misc":
            for key in new_config:
                if key == group_name:
                    for subkey in new_config[key]:
                        new_config[key][subkey] = self.find_and_update_config(
                            subkey, configuration, new_config[key][subkey])

                        update_mycroft_config(new_config[key][subkey])

    #### brightness manager - TODO generic non rpi support
    # Check if the auto dim is enabled
    def is_auto_dim_enabled(self, message=None):
        LOG.info("Checking if auto dim is enabled")
        display_config_path_local = join(xdg_config_home(), "OvosDisplay.conf")
        if exists(display_config_path_local):
            display_configuration = JsonStorage(display_config_path_local)
            self.auto_dim_enabled = display_configuration.get(
                "auto_dim", False)
        else:
            self.auto_dim_enabled = False

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
        date = now_local()
        try:
            from astral import LocationInfo
            from astral.sun import sun
            location = Configuration()["location"]
            lat = location["coordinate"]["latitude"]
            lon = location["coordinate"]["longitude"]
            tz = location["timezone"]["code"]
            city = LocationInfo("Some city", "Some location", tz, lat, lon)
            s = sun(city.observer, date=date)["sunset"]
            self.sunset_time = s["sunset"]
            self.sunrise_time = s["sunrise"]
        except:
            self.sunset_time = datetime.datetime(year=date.year, month=date.month,
                                                 day=date.day, hour=22)
            self.sunrise_time = self.sunset_time + timedelta(hours=8)

        # check sunset times again in 24 hours
        self.event_scheduler.schedule_event(self.get_sunset_time,
                                            when=date + timedelta(hours=24),
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
        # TODO - deprecate this config file and follow plugin convention
        display_config_path_local = join(xdg_config_home(), "OvosDisplay.conf")
        if exists(display_config_path_local):
            display_configuration = JsonStorage(display_config_path_local)
            self.auto_night_mode_enabled = display_configuration.get(
                "auto_nightmode", False)
        else:
            self.auto_night_mode_enabled = False

        if self.auto_night_mode_enabled:
            self.start_auto_night_mode()
        else:
            self.stop_auto_night_mode()
