# Copyright 2019 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from copy import deepcopy
from unittest.mock import Mock

from ovos_config.config import LocalConf
from ovos_config.locations import DEFAULT_CONFIG

__CONFIG = LocalConf(DEFAULT_CONFIG)


class AnyCallable:
    """Class matching any callable.

    Useful for assert_called_with arguments.
    """
    def __eq__(self, other):
        return callable(other)


def base_config():
    """Base config used when mocking.

    Preload to skip hitting the disk each creation time but make a copy
    so modifications don't mutate it.

    Returns:
        (dict) Mycroft default configuration
    """
    return deepcopy(__CONFIG)


def mock_config(temp_dir):
    """Supply a reliable return value for the Configuration.get() method."""
    get_config_mock = Mock()
    config = base_config()
    config['skills']['priority_skills'] = ['foobar']
    config['data_dir'] = str(temp_dir)
    config['server']['metrics'] = False
    config['enclosure'] = {}

    get_config_mock.return_value = config
    return get_config_mock

