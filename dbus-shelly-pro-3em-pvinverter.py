#!/usr/bin/env python

# ─────────────────────────────────────────────────────────────────────────────
# Project: dbus-shelly-pro-3em-smartmeter
# Source: https://github.com/f5uii/dbus-shelly-pro-3em-smartmeter
#
# Feedback, suggestions, and bug reports are welcome and can be submitted
# via the GitHub repository. They will be reviewed as time permits.
# Please note that support is provided on a best-effort basis,
# with no guarantee of response or resolution.
#
# Contributions and pull requests are encouraged!
# ─────────────────────────────────────────────────────────────────────────────

# import normal packages
import platform
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests  # for http GET
import configparser  # for config/ini file


# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusShellyEMService:
    def __init__(self, servicename, paths, productname='Shelly Pro3EM', connection='Shelly EM RPC JSON service'):
        # Read the configuration only once at startup
        self.config = self._getConfig()
        deviceinstance = int(self.config['DEFAULT']['Deviceinstance'])
        customname = self.config['DEFAULT']['CustomName']

        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), register=False)
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))
        paths_wo_unit = [
            '/Status',
            '/Mode'
        ]

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 41281 )  # id assigned by Victron Support from SDM630v2.py
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', customname, writeable=False)
        self._dbusservice.add_path('/Connected', 1, writeable=True)
        self._dbusservice.add_path('/Latency', None)
        self._dbusservice.add_path('/FirmwareVersion', 0.1)
        self._dbusservice.add_path('/Ac/MaxPower', 500.1, writeable=False, gettextcallback=lambda p, v: f"{v} W")



        # Initialize default URLs
        self._status_url = None
        self._config_url = None
        self._energy_url = None

        # Prepare URLs only once
        self._status_url, self._config_url, self._energy_url = self._getShellyStatusUrl()

        self._dbusservice.add_path('/HardwareVersion', self._getShellyFWVersion())
        self._dbusservice.add_path('/Position', int(self.config['PVINVERTER']['ACPosition']))
        self._dbusservice.add_path('/Serial', self._getShellySerial())
        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/StatusCode', 8)  # Dummy path so VRM detects us as a PV-inverter.
        # 0=Startup 0; 1=Startup 1; 2=Startup 2; 3=Startup 3; 4=Startup 4; 5=Startup 5; 6=Startup 6; 7=Running; 8=Standby; 9=Boot loading; 10=Error

        # add paths without units
        for path in paths_wo_unit:
            self._dbusservice.add_path(path, None)

        # add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path, settings['initial'], gettextcallback=settings['textformat'], writeable=True,
                onchangecallback=self._handlechangedvalue)
        self._dbusservice.register()
        # last update
        self._lastUpdate = 0


        # add _update function 'timer'
        gobject.timeout_add(1000, self._update)  # pause 1000ms before the next request

    def _getShellySerial(self):
        meter_data = self._getShellyGetConfig()
        if meter_data is not None:
            if not meter_data['device']['mac']:
                raise ValueError("Response does not contain 'mac' attribute")

            serial = meter_data['device']['mac']
        else:
            serial = None
        return serial

    def _getShellyFWVersion(self):
        meter_data = self._getShellyGetConfig()
        if meter_data is not None:
            if not meter_data['device']['fw_id']:
                raise ValueError("Response does not contain 'device/fw_id' attribute")

            ver = meter_data['device']['fw_id']
        else:
            ver = None
        return ver

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
        return config

    def _getShellyStatusUrl(self):
        URL = "http://%s:%s@%s/rpc/EM.GetStatus?id=0" % (
            self.config['SHELLY_CONNECTION']['Username'],
            self.config['SHELLY_CONNECTION']['Password'],
            self.config['SHELLY_CONNECTION']['Host']
        )
        URL = URL.replace(":@", "")
        URL_Config = "http://%s:%s@%s/rpc/Sys.GetConfig?id=0" % (
            self.config['SHELLY_CONNECTION']['Username'],
            self.config['SHELLY_CONNECTION']['Password'],
            self.config['SHELLY_CONNECTION']['Host']
        )
        URL_Config = URL_Config.replace(":@", "")
        URL_Energy = "http://%s:%s@%s/rpc/EMData.GetStatus?id=0" % (
            self.config['SHELLY_CONNECTION']['Username'],
            self.config['SHELLY_CONNECTION']['Password'],
            self.config['SHELLY_CONNECTION']['Host']
        )
        URL_Energy = URL_Energy.replace(":@", "")
        return URL, URL_Config, URL_Energy

    def _getShellyGetConfig(self):

        if self._status_url is None or self._config_url is None:
            logging.warning("URLs Shelly non définies. Impossible de récupérer la configuration.")
            return None

        logging.info(" URL : %s, URLConfig : %s", self._status_url, self._config_url)
        try:
            meter_r = requests.get(url=self._config_url, timeout=5)

            # check for response
            if not meter_r:
                raise ConnectionError("No response from Shelly EM - %s" % (self._config_url))

            meter_data = meter_r.json()
            self._dbusservice['/Connected'] = 1

            for key, value in meter_data.items():
                logging.info(" _getShellyGetConfig meter_data['%s'] : %s", key, value)
            # check for Json
            if not meter_data:
                raise ValueError("Converting response to JSON failed")
        except requests.exceptions.RequestException as e:
            logging.error("Error accessing URL: %s %s", self._config_url, e)
            meter_data = None
            self._dbusservice['/Connected'] = 0

        return meter_data

    def _getShellyData(self):
        if self._status_url is None:
            logging.warning("Status URL Shelly non définie. Impossible de récupérer les données.")
            return None

        try:
            meter_r = requests.get(url=self._status_url, timeout=5)
            
            # check for response
            if not meter_r:
                raise ConnectionError("No response from Shelly EM - %s" % (self._status_url))
            self._dbusservice['/Connected'] = 1
            meter_data = meter_r.json()

            # check for Json
            if not meter_data:
                raise ValueError("Converting response to JSON failed")
        except requests.exceptions.RequestException as e:
            logging.error("Error accessing URL: %s %s", self._status_url, e)
            meter_data = None
            self._dbusservice['/Connected'] = 0

        return meter_data

    def _getShellyEnergyData(self):
        if self._energy_url is None:
            logging.warning("Energy URL Shelly non définie. Impossible de récupérer les données d'énergie.")
            return None

        try:
            meter_r = requests.get(url=self._energy_url, timeout=5)
            # check for response
            if not meter_r:
                raise ConnectionError("No response from Shelly EM Energy - %s" % (self._energy_url))
            self._dbusservice['/Connected'] = 1
            energy_data = meter_r.json()

            # check for Json
            if not energy_data:
                raise ValueError("Converting energy response to JSON failed")
        except requests.exceptions.RequestException as e:
            logging.error("Error accessing Energy URL: %s %s", self._energy_url, e)
            energy_data = None
            self._dbusservice['/Connected'] = 0

        return energy_data

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s" % (self._lastUpdate))
        logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            # get data from Shelly
            meter_data = self._getShellyData()
            energy_data = self._getShellyEnergyData()

            if meter_data is None or energy_data is None:
                return True  

            for key, value in meter_data.items():
                logging.debug("_update meter_data['%s'] : %s", key, value)

            source_phase = str(self.config['PVINVERTER']['Phase'])
            valid_phases = {'A', 'B', 'C', 'OFF'}
            if source_phase not in valid_phases:
                raise ValueError(f"Phase value '{source_phase}' is not valid. Must be one of {valid_phases}.")
            pvinverter_phase = str(self.config['PVINVERTER']['PhaseDestination'])
            valid_Dbus_phases = {'L1', 'L2', 'L3'}
            if pvinverter_phase not in valid_Dbus_phases:
                raise ValueError(f"PhaseDestination value '{pvinverter_phase}' is not valid. Must be one of {valid_Dbus_phases}.")
            invertpowersign = str(self.config['PVINVERTER']['InvertPowerSign'])
            valid_invertpowersign = {'0', '1'}
            if invertpowersign not in valid_invertpowersign:
                raise ValueError(f"InvertPowerSign value '{invertpowersign}' is not valid. Must be one of {valid_invertpowersign}.")

            energy_type = str(self.config['PVINVERTER'].get('EnergyType', 'direct')).lower()
            valid_energy_types = {'direct', 'return'}
            if energy_type not in valid_energy_types:
                logging.warning(f"EnergyType value '{energy_type}' is not valid. Must be one of {valid_energy_types}. Using 'direct' as default.")
                energy_type = 'direct'

            if pvinverter_phase != 'OFF':
                # send data to DBus

                for phase in ['L1', 'L2', 'L3']:
                    pre = '/Ac/' + phase

                    self._dbusservice['/StatusCode'] = 7  # Running

                    if phase == pvinverter_phase:
                        power = meter_data.get(f'{source_phase.lower()}_act_power')
                        voltage = meter_data.get(f'{source_phase.lower()}_voltage')
                        current = meter_data.get(f'{source_phase.lower()}_current')

                        if power is None or voltage is None or current is None:
                            logging.error(f"Missing data for phase {source_phase}. Skipping update.")
                            continue 

                        if invertpowersign == '1':
                            power = -power
                            current = -current

                        self._dbusservice[pre + '/Voltage'] = voltage
                        self._dbusservice[pre + '/Current'] = current
                        self._dbusservice[pre + '/Power'] = power
                        self._dbusservice['/Ac/Power'] = power
                        

                        for keyenergy_data, valueenergy_data in energy_data.items():
                                logging.debug("_update energy_data['%s'] : %s", keyenergy_data, valueenergy_data)
                        if energy_data is not None:
                            if energy_type == 'direct':
                                energy_key = f"{source_phase.lower()}_total_act_energy"
                                energy_reverse_key = f"{source_phase.lower()}_total_act_ret_energy"
                            else:
                                energy_key = f"{source_phase.lower()}_total_act_ret_energy"
                                energy_reverse_key = f"{source_phase.lower()}_total_act_energy"
                            logging.debug("_energy_key : %s", energy_key)
                            logging.debug("energy_reverse_key : %s", energy_reverse_key)
                            energy_value = energy_data.get(energy_key)
                            energy_reverse_value = energy_data.get(energy_reverse_key)
                            logging.debug("_energy_value : %s", energy_value)
                            logging.debug("_energy_reverse_value: %s", energy_reverse_value)
                            
                            if energy_value is not None:
                                self._dbusservice[pre + '/Energy/Forward'] = energy_value / 1000
                                self._dbusservice['/Ac/Energy/Forward'] = energy_value / 1000
                            else:
                                logging.error(f"Missing energy data for key {energy_key}.")
                                self._dbusservice[pre + '/Energy/Forward'] = 0

                            if energy_reverse_value is not None:
                                self._dbusservice[pre + '/Energy/Reverse'] = energy_reverse_value / 1000  
                                self._dbusservice['/Ac/Energy/Reverse'] = energy_reverse_value / 1000
                            else:
                                logging.error(f"Missing reverse energy data for key {energy_reverse_key}.")
                                self._dbusservice[pre + '/Energy/Reverse'] = 0



                    else:
                        self._dbusservice[pre + '/Voltage'] = None
                        self._dbusservice[pre + '/Current'] = None
                        self._dbusservice[pre + '/Power'] = None
                        self._dbusservice[pre + '/Energy/Forward'] = None
                        self._dbusservice[pre + '/Energy/Reverse'] = None
                        


            # increment UpdateIndex - to show that new data is available
            index = self._dbusservice['/UpdateIndex'] + 1  # increment index
            if index > 255:  # maximum value of the index
                index = 0  # overflow from 255 to 0
            self._dbusservice['/UpdateIndex'] = index
            self._dbusservice['/Mode'] = 0  # Manual, no control

            # update lastupdate vars
            self._lastUpdate = time.time()
        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)

        # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True

    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))

        return True  # accept the change


def get_log_level(config):
    # Dictionnaire de correspondance entre les niveaux de log textuels et les constantes de logging
    log_levels = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }

    # Récupérer le niveau de log depuis le fichier config.ini
    log_level_str = config.get('DEFAULT', 'Log_Level', fallback='ERROR')

    # Convertir en majuscules pour l'insensibilité à la casse
    log_level_str = log_level_str.upper()

    print(f"Started with log level : '{log_level_str}'")
    return log_levels.get(log_level_str, logging.ERROR)


def main():
    # configure logging

    log_directory = os.path.dirname(os.path.realpath(__file__))
    log_file = os.path.join(log_directory, "current.log")
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    log_level = get_log_level(config)

    logging.basicConfig(
        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        level=log_level,
        handlers=[
            logging.handlers.TimedRotatingFileHandler(log_file, when='midnight', interval=1, backupCount=31),
            logging.StreamHandler()
        ])

    try:
        logging.info("Log level set to %s", logging.getLevelName(log_level))
        logging.info("Start shelly reading")

        from dbus.mainloop.glib import DBusGMainLoop
        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        # formatting
        _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
        _a = lambda p, v: (str(round(v, 1)) + 'A')
        _w = lambda p, v: (str(round(v, 1)) + 'W')
        _v = lambda p, v: (str(round(v, 1)) + 'V')

        # start our main-service
        pvac_output = DbusShellyEMService(
            servicename='com.victronenergy.pvinverter',
            paths={
                '/Ac/Energy/Forward': {'initial': None, 'textformat': _kwh},  # energy produced by pv inverter
                '/Ac/Energy/Reverse': {'initial': None, 'textformat': _kwh},  # energy produced by pv inverter
                '/Ac/Power': {'initial': 0, 'textformat': _w},

                '/Ac/Current': {'initial': 0, 'textformat': _a},
                '/Ac/Voltage': {'initial': 0, 'textformat': _v},
                '/SetCurrent': {'initial': 0, 'textformat': _a},
                '/StartStop': {'initial': 0, 'textformat': lambda p, v: (str(v))},
                '/Ac/L1/Voltage': {'initial': None, 'textformat': _v},
                '/Ac/L2/Voltage': {'initial': None, 'textformat': _v},
                '/Ac/L3/Voltage': {'initial': None, 'textformat': _v},
                '/Ac/L1/Current': {'initial': None, 'textformat': _a},
                '/Ac/L2/Current': {'initial': None, 'textformat': _a},
                '/Ac/L3/Current': {'initial': None, 'textformat': _a},
                '/Ac/L1/Power': {'initial': None, 'textformat': _w},
                '/Ac/L2/Power': {'initial': None, 'textformat': _w},
                '/Ac/L3/Power': {'initial': None, 'textformat': _w},
                '/Ac/L1/Energy/Forward': {'initial': None, 'textformat': _kwh},
                '/Ac/L2/Energy/Forward': {'initial': None, 'textformat': _kwh},
                '/Ac/L3/Energy/Forward': {'initial': None, 'textformat': _kwh},
                '/Ac/L1/Energy/Reverse': {'initial': None, 'textformat': _kwh},
                '/Ac/L2/Energy/Reverse': {'initial': None, 'textformat': _kwh},
                '/Ac/L3/Energy/Reverse': {'initial': None, 'textformat': _kwh},

            })

        logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
        mainloop = gobject.MainLoop()
        mainloop.run()
    except Exception as e:
        logging.critical('Error at %s', 'main', exc_info=e)


if __name__ == "__main__":
    main()


# ─────────────────────────────────────────────────────────────────────────────
# Project: dbus-shelly-pro-3em-smartmeter
# Source: https://github.com/f5uii/dbus-shelly-pro-3em-smartmeter
#
# Feedback, suggestions, and bug reports are welcome and can be submitted
# via the GitHub repository. They will be reviewed as time permits.
# Please note that support is provided on a best-effort basis,
# with no guarantee of response or resolution.
#
# Contributions and pull requests are encouraged!
# ─────────────────────────────────────────────────────────────────────────────