# ! Deprecated !
## This app has been superceded by [null_iron_heart](https://github.com/nullstalgia/null_iron_heart)!

### PyHRPresence

A Python and bleak-based heart rate sender for VRChat's OSC

```
# pyhrpresence.ini

[osc]
ip = 127.0.0.1
port = 9000
pulse_length = 100
only_positive_floathr = False
prefix = /avatar/parameters/

[ble]
saved_address = xx:xx:xx:xx:xx:xx
saved_name = Polar H10 XXXXXXXX

[misc]
write_bpm_to_file = False
write_bpm_file_path = bpm.txt
console_log_level = info
log_sessions_to_csv = False

[dummy]
hi_bpm = 190
lo_bpm = 40
loops_before_dc = 5
```

## OSC Parameters

| Parameter         | Path                                 | Description                          |
|-------------------|--------------------------------------|--------------------------------------|
| `HR`              | `/avatar/parameters/HR`              | actual heartrate as int              |
| `floatHR`         | `/avatar/parameters/floatHR`         | maps 0:255 to -1.0:1.0 or 0.0:1.0    |
| `isHRBeat`        | `/avatar/parameters/isHRBeat`        | bool, pulses on each heart beat      |
| `HeartBeatToggle` | `/avatar/parameters/HeartBeatToggle` | bool, flips state on each heart beat |
| `isHRConnected`   | `/avatar/parameters/isHRConnected`   | bool, set when HR monitor connected  |
| `RRInterval`      | `/avatar/parameters/RRInterval`      | heart beat interval int in ms        |

Inspired by HRPresence by [@Naraenda](https://github.com/Naraenda) and [@Natsumi-sama](https://github.com/Natsumi-sama)'s fork.
