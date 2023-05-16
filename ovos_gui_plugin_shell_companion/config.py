
from os.path import isfile, join
import json
from ovos_utils.xdg_utils import xdg_config_home
from json_database import JsonStorage


def get_ovos_shell_config():
    # Paths to find the local display config
    display_config_path_local = join(xdg_config_home(), "OvosDisplay.conf")
    display_config_path_system = "/etc/xdg/OvosDisplay.conf"
    local_display_config = JsonStorage(display_config_path_local)
    if isfile(display_config_path_system):
        with open(display_config_path_system) as f:
            d = json.load(f)
            local_display_config.merge(d)
    return local_display_config

