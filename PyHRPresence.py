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
import logging
from logging.handlers import RotatingFileHandler
from pynput.keyboard import Key, Listener
import csv
from async_timeout import timeout
from Includes.ConfigFunctions import load_config, get_config_value, set_config_value, save_config
import traceback

# From https://stackoverflow.com/a/42615559
# determine if application is a script file or frozen exe
if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

# Create a logger
logger = logging.getLogger(__name__)
log_path = 'pyhrpresence.log'
log_path = os.path.join(application_path, 'pyhrpresence.log')

file_handler = RotatingFileHandler(log_path, maxBytes=1024*1024*10, backupCount=3)
file_handler.setLevel(logging.DEBUG)

# Create a logging format for files
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)


# Create a logging format for the console
stream_formatter = logging.Formatter('%(message)s')
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(stream_formatter)

# Add the handlers to the logger
logger.addHandler(stream_handler)
logger.addHandler(file_handler)

logger.setLevel(logging.DEBUG)


# This is kinda awful but it will be very helpful for debugging
old_hook = sys.excepthook
def exception_handler(exc_type, exc_value, exc_traceback):
    # Handle uncaught exception
    logger.error("Uncaught exception", 
                 exc_info=(exc_type, exc_value, exc_traceback))
    old_hook(exc_type, exc_value, exc_traceback)

sys.excepthook = exception_handler

def decode_logging_level(level: str) -> int:
    if level.lower() == "debug":
        return logging.DEBUG
    elif level.lower() == "info":
        return logging.INFO
    elif level.lower() == "warning":
        return logging.WARNING
    elif level.lower() == "error":
        return logging.ERROR
    elif level.lower() == "critical":
        return logging.CRITICAL
    else:
        return logging.INFO

HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT_CHARACTERISTIC_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

BATTERY_LEVEL_CHARACTERISTIC_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"

OSC_IP = "127.0.0.1"
OSC_PORT = 9000

OSC_PREFIX = "/avatar/parameters/"

RR_CONVERSION_FACTOR = 0.9765625 # 1000/1024, since the RR values are 1/1024 of a second
# according to https://www.bluetooth.org/docman/handlers/DownloadDoc.ashx?doc_id=555004 (GATT Specification Supplement v8) page 123

# This has been put into its own process so the GIL can't 
# interfere with the timing of the pulse
def beat_process(stop_event, new_rr, osc_settings, pulse_time):
    osc_ip = osc_settings["ip"]
    osc_port = osc_settings["port"]
    osc_prefix = osc_settings["prefix"]
    osc_flip_flop_beat_state = False
    osc_client = SimpleUDPClient(osc_ip, osc_port)
    while not stop_event.is_set():
        rr = new_rr.value
        if rr > 0:
            osc_flip_flop_beat_state = not osc_flip_flop_beat_state
            osc_client.send_message(osc_prefix+"HeartBeatToggle", osc_flip_flop_beat_state)
            osc_client.send_message(osc_prefix+"isHRBeat", True)
            time.sleep(pulse_time/1000) # /1000's to convert to seconds
            osc_client.send_message(osc_prefix+"isHRBeat", False)
            new_wait_time = (rr/1000 - (pulse_time/1000))
            if new_wait_time < 0:
                new_wait_time = rr
            time.sleep(new_wait_time)
        else:
            time.sleep(0.5)


class TimeoutTimer:
    def __init__(self, timeout, callback):
        self.timeout = timeout
        self.callback = callback
        self.timer = None

    def reset(self):
        if self.timer:
            self.timer.cancel()

        loop = asyncio.get_event_loop()
        self.timer = loop.call_later(self.timeout, lambda: loop.create_task(self.callback()))

    def stop(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

ble_devices = {}
selected_ble_device = None
detected_ble_devices = 0
osc_client = None

class HRData:
    def __init__(self, rr_output, config, session_log_path, data_length=30):
        self.bpm_history = []
        self.rr_history = []
        self.bpm = 0
        self.bpm_session_max = 0
        self.bpm_session_max_time = 0
        self.bpm_session_min = 65535
        self.bpm_session_min_time = 0
        self.rr = 0
        self.newest_rr = 0
        self.data_length = data_length
        self.osc_client = osc_client
        self.is_connected = False
        self.rr_output = rr_output
        self.config = config
        self.battery = -1
        self.only_positive_floathr = bool(strtobool(get_config_value(config, "osc", "only_positive_floathr", "False", True)))
        self.should_txt_write = bool(strtobool(get_config_value(config, "misc", "write_bpm_to_file", "False", True)))
        self.txt_write_path = os.path.join(application_path, get_config_value(config, "misc", "write_bpm_file_path", "bpm.txt", True))
        self.timer = TimeoutTimer(15, self.timeout_callback)
        self.disconnect_call = None
        self.session_log_path = session_log_path
    
    def update_hr(self, data, is_connected):
        self.timer.reset()
        self.is_connected = is_connected
        self.bpm = data['BeatsPerMinute']
        
        if data['BeatsPerMinute'] == 0:
            self.rr = 0
            self.newest_rr = 0
        else:
            if self.bpm > self.bpm_session_max:
                self.bpm_session_max = self.bpm
                self.bpm_session_max_time = time.time()
            elif self.bpm < self.bpm_session_min and self.bpm > 0:
                self.bpm_session_min = self.bpm
                self.bpm_session_min_time = time.time()
                
            if "RRIntervals" in data.keys():
                self.rr = data['RRIntervals']
                self.newest_rr = self.rr[-1]
                self.newest_rr *= RR_CONVERSION_FACTOR
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
        
        if self.bpm_session_max > 0:
            print(f"Session Max BPM: {self.bpm_session_max} BPM at {time.strftime('%I:%M%p', time.localtime(self.bpm_session_max_time))}")
        if self.bpm_session_min < 65535:
            print(f"Session Min BPM: {self.bpm_session_min} BPM at {time.strftime('%I:%M%p', time.localtime(self.bpm_session_min_time))}")

        if type(self.rr) == list:
            print(f"RR: {self.rr} (ms)")
        else:
            print(f"RR: {self.rr}ms")
            
        if self.battery > -1:
            print(f"Battery: {Fore.RED if self.battery < 30 else Fore.WHITE}{self.battery}%{Fore.RESET}")

        print_histogram(self.bpm_history, 30, 250)

        self.rr_output.value = self.newest_rr
        send_osc(self.is_connected, self.bpm, self.newest_rr, self.only_positive_floathr, self.should_txt_write, self.txt_write_path)

        # Not checking if logging is enabled, since if it wasn't, this would never be True
        if session_log_created and self.bpm > 0:
            with open(self.session_log_path, "a", newline='') as f:
                writer = csv.writer(f)
                writer.writerow([datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), self.bpm, self.newest_rr, self.battery])


    def update_hr_to_zero(self, connected=False):
        self.bpm = 0
        self.update_hr({'BeatsPerMinute': 0}, connected)

    def update_battery(self, data):
        self.battery = data

    async def timeout_callback(self):
        self.is_connected = False
        self.bpm = 0
        self.update_hr_to_zero()
        logger.error("Stopped recieving data. Disconnecting.")
        sys.stdout.flush()
        await self.disconnect_call() # type: ignore
        # TODO: Find ways around pylance complaining about awaiting Never and calling None


def send_osc(is_connected, bpm, newest_rr, only_positive_floathr, should_txt_write, txt_write_path):
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

    if should_txt_write:
        with open(txt_write_path, "w") as f:
            if is_connected:
                f.write(str(bpm))
                #f.write("\n")
                #f.write(str(self.newest_rr))
            else:
                f.write("D/C")

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
        self.receivedZero = 0
        self.receivedNonZero = 0
        
        
    async def connect(self):
        self.client = BleakClient(self.device_address, disconnected_callback=self.on_disconnect)
        logger.debug("Created client.")
        sys.stdout.flush()
        
        # If this actually exceeds 30 seconds, something has gone horribly wrong
        async with timeout(30) as cm:
            try:
                logger.debug("Connecting.")
                sys.stdout.flush()
                self.connected = await self.client.connect()
                logger.debug("Connected, checking if battery service is available.")
                sys.stdout.flush()
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
                logger.error("Couldn't connect to device, try again?")
                pass
        if cm.expired:
            logger.error("Timed out while connecting...")
            self.connected = False
            sys.stdout.flush()

    async def disconnect(self):
        #if self.connected:
        if self.client is not None:
            try:
                await self.client.stop_notify(HEART_RATE_MEASUREMENT_CHARACTERISTIC_UUID)
                await self.client.disconnect()
            except (KeyError, BleakError, AttributeError):
                # Not exactly sure why these get thrown, hopefully this helps
                # I think sometimes on disconnecting when it may already be DC'd, it just double freaks out and throws exceptions?

                # Adding this to see if it helps with not re-connecting after timing out/leaving BLE range.
                self.connected = False
                pass

    def on_disconnect(self, client: BleakClient | None = None):
        self.connected = False
        self.hrdata.update_hr_to_zero()
        logger.info("Disconnected!")

    async def on_hr_data_received(self, characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        raw = read_hr_buffer(data)
        self.hrdata.update_hr(raw, self.connected)
        bpm = raw['BeatsPerMinute']
        if not self.connected or bpm == 0:
            self.battery_characteristic = None
        if bpm == 0:
            self.receivedZero += 1
            self.receivedNonZero = 0
        elif bpm > 0 and self.connected == False:
            self.receivedNonZero += 1
            self.receivedZero = 0

        if self.client is not None:
            if self.receivedZero > 10:
                self.connected = False
                await self.client.disconnect()
            elif self.receivedNonZero > 5:
                self.connected = True
                self.receivedNonZero = 0
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

def on_device_press(key: Key):
    global selected_ble_device
    choice = -1
    try:
        # Check if the pressed key is a digit and within the range of devices
        if hasattr(key, 'char'):
            choice = int(key.char)
    except (ValueError, TypeError, AttributeError):
        # Ignore keys that are not digits
        # Numpad handling
        if hasattr(key, 'vk') and 96 <= key.vk <= 105 and choice == -1:
            choice = key.vk - 96
        else:
            return
            
    if choice in range(len(ble_devices)):
        selected_ble_device = list(ble_devices.values())[choice]
        logger.info(f"{Style.DIM}Selected device {selected_ble_device[0].address}{Style.RESET_ALL}")
        # Stop the listener when a valid choice is made
        return False

def on_save_press(key):
    global should_save
    # Detecting if the key pressed is Y for yes, N for no
    try:
        if str(key.char).lower() == "y":
            should_save = True
            return False
        elif str(key.char).lower() == "n":
            should_save = False
            return False
    except (ValueError, TypeError, AttributeError):
        pass

def device_selection_loop():
    # Start the listener
    with Listener(on_press=on_device_press) as listener:
        listener.join()


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

    known_device_address = get_config_value(config, "ble", "saved_address")
    known_device_name = get_config_value(config, "ble", "saved_name")

    input_thread = Thread(target=device_selection_loop)
    input_thread_ever_started = False
    logger.info("Scanning for BLE Heart Rate devices...")
    sys.stdout.flush()
    await scanner.start()
    check_time = datetime.datetime.now() + datetime.timedelta(seconds=5)
    end_time = datetime.datetime.now() + datetime.timedelta(seconds=120)
    while datetime.datetime.now() < end_time:
        # If any devices have been found, turn on the input thread
        if detected_ble_devices > 0 and not input_thread_ever_started:
            input_thread_ever_started = True
            input_thread.start()

        device_selected = selected_ble_device is not None
        known_device_address_found = (known_device_address in ble_devices)
        # uhhh thanks copilot
        known_device_name_found = (known_device_name is not None and (known_device_name in [device[1] for device in ble_devices.values()]))
        if known_device_address_found or known_device_name_found or device_selected:
            if known_device_address_found:
                logger.info(f"{Fore.YELLOW}Known device {known_device_address} found. Selecting.{Fore.RESET}")
                selected_ble_device = ble_devices[known_device_address]
            elif known_device_name_found:
                logger.info(f"{Fore.YELLOW}Known device {known_device_name} found. Selecting.{Fore.RESET}")
                # this works, thanks again copilot
                selected_ble_device = [device for device in ble_devices.values() if device[1] == known_device_name][0]
            try:
                await scanner.stop()
            except AttributeError:
                pass
            break
        if datetime.datetime.now() > check_time and detected_ble_devices == 0:
            logger.info("No devices found. Restarting Scan.")
            check_time = datetime.datetime.now() + datetime.timedelta(seconds=15)
            try:
                await scanner.stop()
            except AttributeError:
                pass
            await asyncio.sleep(1.5)
            await scanner.start()

        await asyncio.sleep(0.1)

    logger.info(f"{Style.DIM}Stopping device discovery.{Style.RESET_ALL}")
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
            logger.info("No devices found. Exiting.")
            return None
        
    device_address = selected_ble_device[0].address

    # TODO: Redo logic for saving names/addresses when already saved.

    # To account for people who didn't have saved_name, but did reconnect to a saved device, we should check if it's None and if the selected name isn't None, then save it if so
    # Yeesh.
    if known_device_address is not None and device_address is known_device_address and known_device_name is None and selected_ble_device[1] is not None:
        set_config_value(config, "ble", "saved_address", device_address)
        set_config_value(config, "ble", "saved_name", selected_ble_device[1])

    # If this wasn't a known device, ask the user if they want to save it
    if selected_ble_device is not None and (known_device_address != device_address and known_device_name != selected_ble_device[1]):
        global should_save
        should_save = None
        # To avoid poking the prompt on device selection
        await asyncio.sleep(1)

        print("Save this device? (Y/N) ")
        with Listener(on_press=on_save_press) as listener:
            listener.join()
        if should_save:
            set_config_value(config, "ble", "saved_address", device_address)
            set_config_value(config, "ble", "saved_name", selected_ble_device[1])

    return device_address


async def main():
    global osc_client, OSC_IP, OSC_PORT, OSC_PREFIX, session_log_created

    # Read config
    config = load_config(application_path)
    OSC_IP = get_config_value(config, "osc", "ip", OSC_IP, True)
    OSC_PORT = int(get_config_value(config, "osc", "port", OSC_PORT, True))
    OSC_PREFIX = get_config_value(config, "osc", "prefix", OSC_PREFIX, True)
    osc_client = SimpleUDPClient(OSC_IP, OSC_PORT)
    pulse_length = int(get_config_value(config, "osc", "pulse_length", 100, True))

    log_sessions_to_csv = bool(strtobool(get_config_value(config, "misc", "log_sessions_to_csv", "False", True)))
    session_log_path = os.path.join(application_path, "session_logs", f"PyHRP-{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv")
    session_log_created = False

    # Set console log level
    stream_handler.setLevel(decode_logging_level(get_config_value(config, "misc", "console_log_level", "info", True)))

    # Set up beat process
    stop_event = multiprocessing.Event()
    new_rr = multiprocessing.Value('i', -1)
    osc_settings = {"ip": OSC_IP, "port": OSC_PORT, "prefix": OSC_PREFIX}

    beat_process_thread = multiprocessing.Process(target=beat_process, args=(stop_event, new_rr, osc_settings, pulse_length))

    # Set up timeout timer for recieving data

    hrdata = HRData(new_rr, config, session_log_path)

    send_osc(False, 0, 0, hrdata.only_positive_floathr, hrdata.should_txt_write, hrdata.txt_write_path)
    device = await select_device(config)

    # Should only save after device selection and hrdata init, since they may modify the config
    save_config(config)

    if device == None:
        stop_event.set()
        try:
            beat_process_thread.terminate() 
        except AttributeError:
            pass
        #beat_process_thread.join()
        #raise Exception("No device address found! Retry?")
    else:
        monitor = HeartRateMonitor(device, hrdata)
        hrdata.disconnect_call = monitor.disconnect # type: ignore
        # TODO: Find ways around pylance complaining about assigning to None
        beat_process_thread.start()
        while True:
            try:
                if not monitor.connected:
                    logger.info(f"{Fore.GREEN}Attempting to connect...{Fore.RESET}")
                    sys.stdout.flush()
                    await monitor.connect()
                    
                    # Set up the logging CSV if enabled
                    if log_sessions_to_csv and not session_log_created:
                        os.makedirs(os.path.dirname(session_log_path), exist_ok=True)
                        with open(session_log_path, "w", newline='') as f:
                            writer = csv.writer(f)
                            writer.writerow(["Timestamp", "BPM", "RR", "Battery"])
                            logger.debug(f"Created session log at {session_log_path}")
                        session_log_created = True
            except TimeoutError:
                logger.error("Connection timed out. Retrying in 3s...")
                await asyncio.sleep(3)
            except BleakDeviceNotFoundError:
                logger.error("Device not found. Retrying in 3s...")
                await asyncio.sleep(3)
            except (BleakError, OSError):
                logger.error("BLE Error. Retrying in 15s...")
                type, value, traceback = sys.exc_info()
                logger.debug(f"Exception: {type} - {value}")
                await asyncio.sleep(15)
            except (asyncio.exceptions.CancelledError):
                logger.error("System error? Probably not good. Retrying in 3s...")
                type, value, traceback = sys.exc_info()
                logger.debug(f"Exception: {type} - {value}")
                await asyncio.sleep(3)

            # finally:
            #     await monitor.disconnect()
            #     await asyncio.sleep(3)
            #     continue

            if not monitor.connected:
                logger.info("Attempting to reconnect...")
            
            await asyncio.sleep(1)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    init()
    asyncio.run(main())