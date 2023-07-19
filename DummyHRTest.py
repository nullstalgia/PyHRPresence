from pythonosc.udp_client import SimpleUDPClient
import multiprocessing
import time

ip = "127.0.0.1"
port = 9000
positive_hr = False
prefix = "/avatar/parameters/"

global osc_client
osc_client = SimpleUDPClient(ip, port)

def beat_process(stop_event, new_rr):
    pulse_time=100
    osc_client = SimpleUDPClient(ip, port)
    while not stop_event.is_set():
        rr = new_rr.value
        if rr > 0:
            osc_client.send_message(prefix+"isHRBeat", True)
            time.sleep(pulse_time/1000) # /1000's to convert to seconds
            osc_client.send_message(prefix+"isHRBeat", False)
            new_wait_time = (rr/1000 - (pulse_time/1000))
            if new_wait_time < 0:
                new_wait_time = rr
            time.sleep(new_wait_time)
        else:
            time.sleep(1)

def send_osc(is_connected, bpm, newest_rr, only_positive_floathr):
    global osc_client
    if osc_client is not None:
        bundle = []
        bundle.append((prefix+"isHRConnected", is_connected))
        if is_connected:
            bundle.append((prefix+"HR", bpm))
            bundle.append((prefix+"RRInterval", newest_rr))
            if only_positive_floathr:
                bundle.append((prefix+"floatHR", bpm/255.0)) # 0-1 (Less accurate, but may be more useful for some applications)
            else:
                bundle.append((prefix+"floatHR", (bpm*0.0078125) - 1.0)) # -1.0-1.0
        else:
            bundle.append((prefix+"isHRBeat", False))
            
        for msg in bundle:
            osc_client.send_message(msg[0], msg[1])

if "__main__" == __name__:
    multiprocessing.freeze_support()
    print("Starting Dummy HR Test")
    # Set up beat process
    stop_event = multiprocessing.Event()
    new_rr = multiprocessing.Value('i', -1)
    beat_process_thread = multiprocessing.Process(target=beat_process, args=(stop_event, new_rr))
    beat_process_thread.start()
    bpm = 60
    loops = 0
    direction = 1
    while True:
        bpm = bpm + direction
        if bpm > 100 or bpm < 60:
            direction *= -1
            loops += 1
            if loops > 8:
                loops = 0
                send_osc(False, 0, -1, positive_hr)
                time.sleep(5)
            
            
        newest_rr = int(60000/bpm)
        send_osc(True, bpm, newest_rr, positive_hr)
        new_rr.value = newest_rr
        time.sleep(0.25)