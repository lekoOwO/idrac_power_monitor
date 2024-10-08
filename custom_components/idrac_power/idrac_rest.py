import logging
from typing import Callable

import requests
import urllib3
from homeassistant.exceptions import HomeAssistantError
from requests import Response
from requests.exceptions import RequestException, JSONDecodeError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .const import (
    JSON_NAME, JSON_MANUFACTURER, JSON_MODEL, JSON_SERIAL_NUMBER,
    JSON_POWER_CONSUMED_WATTS, JSON_FIRMWARE_VERSION, JSON_STATUS, JSON_STATUS_STATE, JSON_POWERSTATE
)

_LOGGER = logging.getLogger(__name__)

protocol = 'https://'
drac_managers_path = '/redfish/v1/Managers/iDRAC.Embedded.1'
drac_chassis_path = '/redfish/v1/Chassis/System.Embedded.1'
drac_powercontrol_path = '/redfish/v1/Chassis/System.Embedded.1/Power/PowerControl'
drac_powerON_path = '/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset'
drac_thermals = '/redfish/v1/Chassis/System.Embedded.1/Thermal'


def handle_error(result):
    if result.status_code == 401:
        raise InvalidAuth()

    if result.status_code == 404:
        try:
            error = result.json()['error']
        except JSONDecodeError:
            # start of iDRAC can cause 404 error, ignore it
            raise CannotConnect(f"iDRAC responed with 404, but no JSON present:\n{result.text}")
        if error['code'] == 'Base.1.0.GeneralError' and 'RedFish attribute is disabled' in \
                error['@Message.ExtendedInfo'][0]['Message']:
            raise RedfishConfig()

    if result.status_code != 200:
        raise CannotConnect(result.text)


class IdracRest:
    def __init__(self, host, username, password, interval):
        self.host = host
        self.auth = (username, password)
        self.interval = interval

        self.callback_thermals: list[Callable[[dict | None], None]] = []
        self.callback_status: list[Callable[[bool | None], None]] = []
        self.callback_power_status: list[Callable[[bool | None], None]] = []
        self.callback_power_usage: list[Callable[[int | None], None]] = []

        self.thermal_values: dict = {}
        self.status: bool = False
        self.power_status: bool = False
        self.power_usage: int = 0

    def get_device_info(self) -> dict | None:
        try:
            result = self.get_path(drac_chassis_path)
        except RequestException:
            raise CannotConnect(f"Cannot connect to {self.host}")

        handle_error(result)

        chassis_results = result.json()
        return {
            JSON_NAME: chassis_results[JSON_NAME],
            JSON_MANUFACTURER: chassis_results[JSON_MANUFACTURER],
            JSON_MODEL: chassis_results[JSON_MODEL],
            JSON_SERIAL_NUMBER: chassis_results[JSON_SERIAL_NUMBER]
        }

    def get_firmware_version(self) -> str | None:
        try:
            result = self.get_path(drac_managers_path)
        except RequestException:
            raise CannotConnect(f"Could not get firmware version of {self.host}")

        handle_error(result)

        manager_results = result.json()
        return manager_results[JSON_FIRMWARE_VERSION]

    def get_path(self, path):
        return requests.get(protocol + self.host + path, auth=self.auth, verify=False, timeout=300)

    def power_on(self) -> Response | None:
        try:
            result = requests.post(protocol + self.host + drac_powerON_path, auth=self.auth, verify=False,
                                   json={"ResetType": "On"}, timeout=300)
        except RequestException as e:
            raise CannotConnect(f"Could not power on {self.host}: {e}")

        json = result.json()
        if result.status_code == 401:
            raise InvalidAuth()

        if result.status_code == 404:
            error = result.json()['error']
            if error['code'] == 'Base.1.0.GeneralError' and 'RedFish attribute is disabled' in \
                    error['@Message.ExtendedInfo'][0]['Message']:
                raise RedfishConfig()
        if "error" in json:
            _LOGGER.error("iDRAC power on failed: %s", json["error"]["@Message.ExtendedInfo"][0]["Message"])

        return result

    def register_callback_thermals(self, callback: Callable[[dict | None], None]) -> None:
        self.callback_thermals.append(callback)

    def register_callback_status(self, callback: Callable[[bool | None], None]) -> None:
        self.callback_status.append(callback)

    def register_callback_power_status(self, callback: Callable[[bool | None], None]) -> None:
        self.callback_power_status.append(callback)

    def register_callback_power_usage(self, callback: Callable[[int | None], None]) -> None:
        self.callback_power_usage.append(callback)

    def update_thermals(self) -> dict:
        try:
            req = self.get_path(drac_thermals)
            handle_error(req)
            new_thermals = req.json()

        except (RequestException, RedfishConfig, CannotConnect) as e:
            _LOGGER.debug(f"Couldn't update {self.host} thermals: {e}")
            new_thermals = None

        if new_thermals != self.thermal_values:
            self.thermal_values = new_thermals
            for callback in self.callback_thermals:
                callback(self.thermal_values)
        return self.thermal_values

    def update_status(self):
        try:
            result = self.get_path(drac_chassis_path)
            handle_error(result)
            status_values = result.json()

            try:
                new_status = status_values[JSON_STATUS][JSON_STATUS_STATE] == 'Enabled'
            except:
                new_status = None

            try:
                new_power_status = status_values[JSON_POWERSTATE] == 'On'
            except:
                new_power_status = None

        except (RequestException, RedfishConfig, CannotConnect) as e:
            _LOGGER.debug(f"Couldn't update {self.host} status: {e}")
            new_status = None
            new_power_status = None

        if new_status != self.status:
            self.status = new_status
            for callback in self.callback_status:
                callback(self.status)
        
        if new_power_status != self.power_status:
            self.power_status = new_power_status
            for callback in self.callback_power_status:
                callback(self.power_status)


    def update_power_usage(self):
        try:
            result = self.get_path(drac_powercontrol_path)
            handle_error(result)
            power_values = result.json()
        except (RequestException, RedfishConfig, CannotConnect) as e:
            _LOGGER.debug(f"Couldn't update {self.host} power usage: {e}")
            for callback in self.callback_power_usage:
                callback(None)
            return

        try:
            new_power_usage = power_values[JSON_POWER_CONSUMED_WATTS]
            if new_power_usage != self.power_usage:
                self.power_usage = new_power_usage
                for callback in self.callback_power_usage:
                    callback(self.power_usage)
        except:
            pass


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class RedfishConfig(HomeAssistantError):
    """Error to indicate that Redfish was not properly configured"""


class IdracMock(IdracRest):
    def __init__(self, host, username, password, interval):
        super().__init__(host, username, password, interval)

    def get_device_info(self):
        return {
            JSON_NAME: "Mock Device",
            JSON_MANUFACTURER: "Mock Manufacturer",
            JSON_MODEL: "Mock Model",
            JSON_SERIAL_NUMBER: "Mock Serial"
        }

    def get_firmware_version(self):
        return "1.0.0"

    def power_on(self):
        return "ON"

    def update_thermals(self) -> dict:
        new_thermals = {
            'Fans': [
                {
                    'FanName': "First Mock Fan",
                    'Reading': 1
                },
                {
                    'FanName': "Second Mock Fan",
                    'Reading': 2
                }
            ],
            'Temperatures': [
                {
                    'Name': "Mock Temperature",
                    'ReadingCelsius': 10
                }
            ]
        }

        if new_thermals != self.thermal_values:
            self.thermal_values = new_thermals
            for callback in self.callback_thermals:
                callback(self.thermal_values)
        return self.thermal_values

    def update_status(self):
        new_status = True
        new_power_status = True

        if new_status != self.status:
            self.status = new_status
            for callback in self.callback_status:
                callback(self.status)
        
        if new_power_status != self.power_status:
            self.power_status = new_power_status
            for callback in self.callback_power_status:
                callback(self.power_status)

    def update_power_usage(self):
        power_values = {
            JSON_POWER_CONSUMED_WATTS: 100
        }
        try:
            new_power_usage = power_values[JSON_POWER_CONSUMED_WATTS]
            if new_power_usage != self.power_usage:
                self.power_usage = new_power_usage
                for callback in self.callback_power_usage:
                    callback(self.power_usage)
        except:
            pass
