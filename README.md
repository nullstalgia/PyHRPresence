# PyHRPresence
A Python and bleak-based heart rate sender for VRChat's OSC

```
# pyhrpresence.ini

[osc]
ip = 127.0.0.1
port = 9000
pulse_length = 100
only_positive_floathr = False

[ble]
saved_address = xx:xx:xx:xx:xx:xx
saved_name = Polar H10 XXXXXXXX

[misc]
write_bpm_to_file = False
write_bpm_file_path = bpm.txt
console_log_level = info

```

## OSC Parameters

| Parameter       | Path                               | Description                        |
|-----------------|------------------------------------|------------------------------------|
| `HR`            | `/avatar/parameters/HR`            | actual heartrate as int            |
| `floatHR`       | `/avatar/parameters/floatHR`       | maps 0:255 to -1.0:1.0 or 0.0:1.0    |
| `isHRBeat`      | `/avatar/parameters/isHRBeat`      | bool set when heart beats          |
| `isHRConnected` | `/avatar/parameters/isHRConnected` | bool set when HR monitor connected |
| `RRInterval`    | `/avatar/parameters/RRInterval`    | heart beat interval int in ms      |

Inspired by HRPresence by [@Naraenda](https://github.com/Naraenda) and [@Natsumi-sama](https://github.com/Natsumi-sama)'s fork.