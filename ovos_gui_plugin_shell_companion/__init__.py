import json
import os
import platform
from os.path import exists, join

from json_database import JsonStorage
from ovos_bus_client import Message
from ovos_config import Configuration
from ovos_config.config import update_mycroft_config
from ovos_utils import network_utils
from ovos_utils.fingerprinting import get_mycroft_version
from ovos_utils.log import LOG
from ovos_utils.xdg_utils import xdg_config_home

from ovos_gui_plugin_shell_companion.brightness import BrightnessManager
from ovos_gui_plugin_shell_companion.color_manager import ColorManager
from ovos_gui_plugin_shell_companion.wigets import WidgetManager
from ovos_plugin_manager.templates.gui import GUIExtension


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
        self.init_config_provider()

        self.color_manager = ColorManager(self.bus)
        self.widgets = WidgetManager(self.bus)
        self.bright = BrightnessManager(self.bus)

    def init_config_provider(self):
        self.settings_meta = {}
        self.build_settings_meta()

        self.bus.on("ovos.phal.configuration.provider.list.groups",
                    self.list_groups)
        self.bus.on("ovos.phal.configuration.provider.get",
                    self.get_settings_meta)
        self.bus.on("ovos.phal.configuration.provider.set",
                    self.set_settings_in_config)

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
