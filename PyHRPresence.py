import asyncio
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakDeviceNotFoundError, BleakError
from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.scanner import AdvertisementData
from pythonosc.udp_client import SimpleUDPClient
#import PySimpleGUI as sg
import configparser
import datetime
from colorama import init, Fore, Style
import os
import struct
from io import BytesIO
import time
from pprint import pprint
from distutils.util import strtobool
import multiprocessing
import sys
from threading import Thread

HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT_CHARACTERISTIC_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

BATTERY_LEVEL_CHARACTERISTIC_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"

OSC_IP = "127.0.0.1"
OSC_PORT = 9000

OSC_PREFIX = "/avatar/parameters/"

CONVERSION_FACTOR = 0.9765625 # 1000/1024, since the RR values are 1/1024 of a second
# according to https://www.bluetooth.org/docman/handlers/DownloadDoc.ashx?doc_id=555004 (GATT Specification Supplement v8) page 123

# This has been put into its own process so the GIL can't 
# interfere with the timing of the pulse
def beat_process(stop_event, new_rr, osc_settings, pulse_time):
    osc_ip = osc_settings["ip"]
    osc_port = osc_settings["port"]
    osc_prefix = osc_settings["prefix"]
    osc_client = SimpleUDPClient(osc_ip, osc_port)
    while not stop_event.is_set():
        rr = new_rr.value
        if rr > 0:
            osc_client.send_message(osc_prefix+"isHRBeat", True)
            time.sleep(pulse_time/1000) # /1000's to convert to seconds
            osc_client.send_message(osc_prefix+"isHRBeat", False)
            new_wait_time = (rr/1000 - (pulse_time/1000))
            if new_wait_time < 0:
                new_wait_time = rr
            time.sleep(new_wait_time)
        else:
            time.sleep(1)


ble_devices = {}
selected_ble_device = None
detected_ble_devices = 0
osc_client = None

class HRData:
    def __init__(self, rr_output, config, data_length=30):
        self.bpm_history = []
        self.rr_history = []
        self.bpm = 0
        self.bpm_peak = 0
        self.bpm_peak_time = 0
        self.rr = 0
        self.newest_rr = 0
        self.data_length = data_length
        self.osc_client = osc_client
        self.is_connected = False
        self.rr_output = rr_output
        self.config = config
        self.only_positive_floathr = bool(strtobool(get_config_value(config, "osc", "only_positive_floathr", "False")))
        self.battery = -1
        set_config_value(config, "osc", "only_positive_floathr", str(self.only_positive_floathr))

    
    def update_hr(self, data, is_connected):
        self.is_connected = is_connected
        self.bpm = data['BeatsPerMinute']
        
        if data['BeatsPerMinute'] == 0:
            self.rr = 0
            self.newest_rr = 0
        else:
            if self.bpm > self.bpm_peak:
                self.bpm_peak = self.bpm
                self.bpm_peak_time = time.time()
            if "RRIntervals" in data.keys():
                self.rr = data['RRIntervals']
                self.newest_rr = self.rr[-1]
                self.newest_rr *= CONVERSION_FACTOR
                self.newest_rr = int(self.newest_rr)
            else:
                self.rr = int(60000/data['BeatsPerMinute'])
                self.newest_rr = self.rr

            self.bpm_history.append(self.bpm)
            self.rr_history.append(self.rr)
            while len(self.bpm_history) > self.data_length:
                self.bpm_history.pop(0)
            while len(self.rr_history) > self.data_length:
                self.rr_history.pop(0)

        os.system('cls' if os.name == 'nt' else 'clear')
        #pprint(raw)
        print(f"BPM: {self.bpm}")
        
        if self.bpm_peak > 0:
            print(f"Peak: {self.bpm_peak} BPM at {time.strftime('%I:%M%p', time.localtime(self.bpm_peak_time))}")

        if type(self.rr) == list:
            print(f"RR: {self.rr} (ms)")
        else:
            print(f"RR: {self.rr}ms")
            
        if self.battery > -1:
            print(f"Battery: {Fore.RED if self.battery < 50 else Fore.WHITE}{self.battery}%{Fore.RESET}")

        print_histogram(self.bpm_history, 30, 250)

        self.rr_output.value = self.newest_rr
        send_osc(self.is_connected, self.bpm, self.newest_rr, self.only_positive_floathr)

    def update_battery(self, data):
        self.battery = data

def send_osc(is_connected, bpm, newest_rr, only_positive_floathr):
    global osc_client
    if osc_client is not None:
        bundle = []
        bundle.append((OSC_PREFIX+"isHRConnected", is_connected))
        if is_connected:
            bundle.append((OSC_PREFIX+"HR", bpm))
            bundle.append((OSC_PREFIX+"RRInterval", newest_rr))
            if only_positive_floathr:
                bundle.append((OSC_PREFIX+"floatHR", bpm/255.0)) # 0-1 (Less accurate, but may be more useful for some applications)
            else:
                bundle.append((OSC_PREFIX+"floatHR", (bpm*0.0078125) - 1.0)) # -1.0-1.0
        else:
            bundle.append((OSC_PREFIX+"isHRBeat", False))
            
        for msg in bundle:
            osc_client.send_message(msg[0], msg[1])


def print_histogram(data, min_value, max_value):
    histogram_height = 10  # height of the histogram
    histogram = [[' ' for _ in data] for _ in range(histogram_height)]
    
    for i, value in enumerate(data):
        # scale the value to histogram_height
        scaled_value = int((value - min_value) / (max_value - min_value) * (histogram_height - 1))
        histogram[scaled_value][i] = '#'
        
    # print the histogram row by row, starting from the top
    for row in reversed(histogram):
        print(''.join(row))

def load_config():
    config = configparser.ConfigParser()
    config.read('pyhrpresence.ini')
    return config


def get_config_value(config, section, key, default=None):
    if config.has_section(section) and config.has_option(section, key):
        return config.get(section, key)
    return default

def set_config_value(config, section, key, value):
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, key, value)

def save_config(config):
    with open('pyhrpresence.ini', 'w') as configfile:
        config.write(configfile)

def read_hr_buffer(data):
    length = len(data)
    if length == 0:
        return None

    ms = BytesIO(data)
    flags = ms.read(1)[0]
    isshort = flags & 1
    contactSensor = (flags >> 1) & 3
    hasEnergyExpended = flags & 8
    hasRRInterval = flags & 16
    minLength = 3 if isshort else 2

    if len(data) < minLength:
        return None

    reading = {}
    reading['Flags'] = flags
    reading['Status'] = contactSensor
    reading['BeatsPerMinute'] = struct.unpack('<H', ms.read(2))[0] if isshort else ms.read(1)[0]

    if hasEnergyExpended:
        reading['EnergyExpended'] = struct.unpack('<H', ms.read(2))[0]

    if hasRRInterval:
        rrvalueCount = int((len(data) - ms.tell()) / 2)
        rrvalues = [struct.unpack('<H', ms.read(2))[0] for _ in range(rrvalueCount)]
        reading['RRIntervals'] = rrvalues

    return reading


class HeartRateMonitor:
    def __init__(self, device_address, hrdata: HRData):
        self.device_address = device_address
        self.client = None
        self.connected = False
        self.hrdata = hrdata
        self.last_battery_run = 0
        self.battery_characteristic = None
        self.receivedZero = False
        
        
    async def connect(self):
        self.client = BleakClient(self.device_address, disconnected_callback=self.on_disconnect)
        try:
            self.connected = await self.client.connect()
            if self.connected:
                await self.client.start_notify(HEART_RATE_MEASUREMENT_CHARACTERISTIC_UUID, self.on_hr_data_received)
                # Check if the device has the battery service
                for service in self.client.services:
                    if service.uuid == BATTERY_SERVICE_UUID:

                        for characteristic in service.characteristics:
                            if characteristic.uuid == BATTERY_LEVEL_CHARACTERISTIC_UUID:
                                # Add the battery level function to the task loop if it's not already there
                                self.battery_characteristic = characteristic
                                break
                
        except asyncio.exceptions.CancelledError:
            print("Couldn't connect to device, try again?")
            pass

    async def disconnect(self):
        #if self.connected:
        if self.client is not None:
            await self.client.stop_notify(HEART_RATE_MEASUREMENT_CHARACTERISTIC_UUID)
            await self.client.disconnect()

    def on_disconnect(self, client: BleakClient):
        self.connected = False
        self.hrdata.update_hr({'BeatsPerMinute': 0}, self.connected)
        print("Disconnected!")

    async def on_hr_data_received(self, characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        raw = read_hr_buffer(data)
        self.hrdata.update_hr(raw, self.connected)
        await self.get_battery_level()

    async def get_battery_level(self):
       if self.connected and self.battery_characteristic is not None:
            if time.time() - self.last_battery_run > (5*60):
                if self.client is not None:
                    value = await self.client.read_gatt_char(self.battery_characteristic)
                    self.hrdata.update_battery(int.from_bytes(value, byteorder='little'))
                    self.last_battery_run = time.time()

def discovery_callback(device: BLEDevice, advertisement_data: AdvertisementData):
    global selected_ble_device
    global detected_ble_devices
    detected_ble_devices += 1
    if selected_ble_device is None:  # Do not add devices and print after selection
        # Check if device already exists in the list
        if device.address not in ble_devices or ble_devices[device.address][1] is None:
            ble_devices[device.address] = (device, advertisement_data.local_name)
            print_ble_devices()

def print_ble_devices():
    os.system('cls' if os.name == 'nt' else 'clear')  # Clear the console
    for i, device in enumerate(ble_devices.values()):
        print(f"{Fore.BLUE}{i}{Fore.RESET}: {device[0].address} - {Fore.GREEN}{device[1]}{Fore.RESET}")
        #pprint(device[0])
        #pprint(device[1])
    print("Enter device index to select: ")

def device_selection_loop():
    global selected_ble_device
    while selected_ble_device is None:  # Exit the loop once a device is selected
        choice = input()
        if choice.isdigit() and int(choice) in range(len(ble_devices)):
            selected_ble_device = list(ble_devices.values())[int(choice)]
            print(f"{Style.DIM}Selected device {selected_ble_device[0].address}{Style.RESET_ALL}")
            break


async def list_devices():
    devices = await BleakScanner.discover()
    for device in devices:
        pprint(device.metadata)
    return [device for device in devices if HEART_RATE_SERVICE_UUID.lower() in device.metadata["uuids"]]

async def select_device(config):
    global selected_ble_device
    scanner = BleakScanner(
        discovery_callback, [HEART_RATE_SERVICE_UUID]
    )

    known_device = get_config_value(config, "ble", "saved_address")

    input_thread = Thread(target=device_selection_loop)
    input_thread.start()

    await scanner.start()
    check_time = datetime.datetime.now() + datetime.timedelta(seconds=5)
    end_time = datetime.datetime.now() + datetime.timedelta(seconds=60)
    while datetime.datetime.now() < end_time:
        if known_device in ble_devices or selected_ble_device is not None:
            if known_device in ble_devices:
                print(f"{Fore.YELLOW}Known device {known_device} found. Selecting.{Fore.RESET}")
                selected_ble_device = ble_devices[known_device]
            try:
                await scanner.stop()
            except AttributeError:
                pass
            break
        if datetime.datetime.now() > check_time and detected_ble_devices == 0:
            print("No devices found. Restarting Scan.")
            check_time = datetime.datetime.now() + datetime.timedelta(seconds=10)
            try:
                await scanner.stop()
            except AttributeError:
                pass
            await asyncio.sleep(1.5)
            await scanner.start()

        await asyncio.sleep(1.0)

    if input_thread.is_alive():
        print(f"{Style.DIM}Stopping device discovery.{Style.RESET_ALL}")
        try:
            await scanner.stop()
        except AttributeError:
            pass
        if selected_ble_device is None:
            if len(ble_devices) > 0:
                print("Enter device index to select: ", end="")
                sys.stdout.flush()
                input_thread.join()  # Wait for the input thread to finish
            else:
                print("No devices found. Exiting.")
                return None
        
    device_address = selected_ble_device[0].address

    # If this wasn't a known device, ask the user if they want to save it_
    if selected_ble_device is not None and known_device != device_address:
        if strtobool(input("Save this device? (Y/N) ")):
            set_config_value(config, "ble", "saved_address", device_address)
            save_config(config)

    return device_address

async def main():
    global osc_client, OSC_IP, OSC_PORT
    config = load_config()
    OSC_IP = get_config_value(config, "osc", "ip", OSC_IP)
    OSC_PORT = int(get_config_value(config, "osc", "port", OSC_PORT))
    set_config_value(config, "osc", "ip", OSC_IP)
    set_config_value(config, "osc", "port", str(OSC_PORT))
    osc_client = SimpleUDPClient(OSC_IP, OSC_PORT)

    # Set up beat process
    stop_event = multiprocessing.Event()
    new_rr = multiprocessing.Value('i', -1)
    osc_settings = {"ip": OSC_IP, "port": OSC_PORT, "prefix": OSC_PREFIX}
    pulse_length = int(get_config_value(config, "osc", "pulse_length", 100))
    set_config_value(config, "osc", "pulse_length", str(pulse_length))

    beat_process_thread = multiprocessing.Process(target=beat_process, args=(stop_event, new_rr, osc_settings, pulse_length))
    beat_process_thread.start()

    hrdata = HRData(new_rr, config)
    send_osc(False, 0, 0, hrdata.only_positive_floathr)
    device = await select_device(config)
    save_config(config)
    if device == None:
        stop_event.set()
        beat_process_thread.terminate()
        #beat_process_thread.join()
        #raise Exception("No device address found! Retry?")
    else:
        monitor = HeartRateMonitor(device, hrdata)
        while True:
            try:
                if not monitor.connected:
                    print(f"{Fore.GREEN}Attempting to connect...{Fore.RESET}")
                    sys.stdout.flush()
                    await monitor.connect()
            except TimeoutError:
                print("Connection timed out. Retrying in 3s...")
                await asyncio.sleep(3)
            except BleakDeviceNotFoundError:
                print("Device not found. Retrying in 3s...")
                await asyncio.sleep(3)
            except (BleakError, OSError):
                print("BLE Error. Retrying in 15s...")
                await asyncio.sleep(15)
            except (asyncio.exceptions.CancelledError):
                print("System error? Probably not good. Retrying in 3s...")
                await asyncio.sleep(3)

            # finally:
            #     await monitor.disconnect()
            #     await asyncio.sleep(3)
            #     continue

            if not monitor.connected:
                print("Attempting to reconnect...")
            
            await asyncio.sleep(1)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    init()
    asyncio.run(main())