from pythonosc.udp_client import SimpleUDPClient
import multiprocessing
import time
from Includes.ConfigFunctions import load_config, get_config_value, set_config_value, save_config
from distutils.util import strtobool
import os, sys

# From https://stackoverflow.com/a/42615559
# determine if application is a script file or frozen exe
if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

def beat_process(stop_event, new_rr, OSC_IP, OSC_PORT, PULSE_LENGTH, OSC_PREFIX):
    osc_client = SimpleUDPClient(OSC_IP, OSC_PORT)
    while not stop_event.is_set():
        rr = new_rr.value
        if rr > 0:
            osc_client.send_message(OSC_PREFIX+"isHRBeat", True)
            time.sleep(PULSE_LENGTH/1000) # /1000's to convert to seconds
            osc_client.send_message(OSC_PREFIX+"isHRBeat", False)
            new_wait_time = (rr/1000 - (PULSE_LENGTH/1000))
            if new_wait_time < 0:
                new_wait_time = rr
            time.sleep(new_wait_time)
        else:
            time.sleep(1)

def send_osc(is_connected, bpm, newest_rr, only_positive_floathr):
    global OSC_CLIENT
    if OSC_CLIENT is not None:
        floatHR = 0.0
        bundle = []
        bundle.append((OSC_PREFIX+"isHRConnected", is_connected))
        if is_connected:
            bundle.append((OSC_PREFIX+"HR", bpm))
            bundle.append((OSC_PREFIX+"RRInterval", newest_rr))
            if only_positive_floathr:
                floatHR = (bpm/255.0) # 0-1 (Less accurate, but may be more useful for some applications)
            else:
                floatHR = ((bpm*0.0078125) - 1.0) # -1.0-1.0
            bundle.append((OSC_PREFIX+"floatHR", floatHR))
        else:
            bundle.append((OSC_PREFIX+"isHRBeat", False))
            
        for msg in bundle:
            OSC_CLIENT.send_message(msg[0], msg[1])
        os.system('cls' if os.name == 'nt' else 'clear')
        print("DummyHRTest")
        if is_connected:
            print("HR: "+str(bpm))
            print("RR: "+str(newest_rr))
            print("floatHR: "+str(floatHR))

            
        else:
            print("Sending not connected")

if "__main__" == __name__:
    multiprocessing.freeze_support()
    print("Starting Dummy HR Test")
    global OSC_CLIENT, OSC_IP, OSC_PORT, OSC_PREFIX, ONLY_POSITIVE_HR

    OSC_IP = "127.0.0.1"
    OSC_PORT = 9000
    ONLY_POSITIVE_HR = False
    PULSE_LENGTH = 100
    OSC_PREFIX = "/avatar/parameters/"

    config = load_config(application_path)
    OSC_IP = get_config_value(config, "osc", "ip", OSC_IP, True)
    OSC_PORT = int(get_config_value(config, "osc", "port", OSC_PORT, True))
    OSC_PREFIX = get_config_value(config, "osc", "prefix", OSC_PREFIX, True)
    PULSE_LENGTH = int(get_config_value(config, "osc", "pulse_length", PULSE_LENGTH, True))
    ONLY_POSITIVE_HR = bool(strtobool(get_config_value(config, "osc", "only_positive_floathr", "False", True)))

    OSC_CLIENT = SimpleUDPClient(OSC_IP, OSC_PORT)

    hi_bpm = int(get_config_value(config, "dummy", "hi_bpm", 190, True))
    lo_bpm = int(get_config_value(config, "dummy", "lo_bpm", 40, True))
    loops_before_dc = int(get_config_value(config, "dummy", "loops_before_dc", 5, True))
    save_config(config)

    # Set up beat process
    stop_event = multiprocessing.Event()
    new_rr = multiprocessing.Value('i', -1)
    beat_process_thread = multiprocessing.Process(target=beat_process, args=(stop_event, new_rr, OSC_IP, OSC_PORT, PULSE_LENGTH, OSC_PREFIX))
    beat_process_thread.start()
    bpm = lo_bpm
    loops = 0
    direction = 1
    while True:
        bpm = bpm + direction
        if bpm > hi_bpm or bpm < lo_bpm:
            direction *= -1
            loops += 1
            if loops > loops_before_dc:
                loops = 0
                send_osc(False, 0, -1, ONLY_POSITIVE_HR)
                time.sleep(5)
            
            
        newest_rr =  int((1 / (bpm / 60)) * 1000)
        send_osc(True, bpm, newest_rr, ONLY_POSITIVE_HR)
        new_rr.value = newest_rr
        time.sleep(0.2)