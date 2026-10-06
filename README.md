# Firewatch + Raspberry Pi Fire Sensor

This adds a Raspberry Pi sensor module to the existing Firewatch project.

The existing project already combines reported and satellite fires and uses
those locations for its map/routing system. The Pi module adds a third source:
`device`.

## Raspberry Pi hardware

- Raspberry Pi
- Raspberry Pi Camera supported by Picamera2
- Adafruit MLX90640 thermal camera
- Adafruit Ultimate GPS HAT (or another NMEA GPS)

The MLX90640 is read over I2C. The GPS is read over the Pi serial port.
The camera is handled by Picamera2.

## What gets recorded

Every persistent detection creates:

`data/device_fires.csv`

with:

- `timestamp`
- `latitude`
- `longitude`
- `fire_size_percent`
- `hot_pixels_percent`
- `max_temperature_c`
- `image`
- `place`
- `source`

The photograph is saved under:

`images/fire_YYYYMMDDTHHMMSSZ.jpg`

`fire_size_percent` means the percentage of MLX90640 thermal pixels above
`HOT_TEMP_C`. It is not a physical measurement of fire area.

## Raspberry Pi setup

Enable I2C:

```bash
sudo raspi-config
```

Enable I2C, then reboot.

Install the normal project dependencies:

```bash
pip install -r requirements-pi.txt
```

Picamera2 is normally installed from Raspberry Pi OS rather than from PyPI.
On Raspberry Pi OS, install it with the OS package manager if it is not
already installed:

```bash
sudo apt update
sudo apt install -y python3-picamera2
```

### GPS HAT

For the Adafruit GPS HAT using the Pi UART, configure the serial port so it
is available to the application and not used as a login console.

If your GPS appears as a USB serial device instead, set:

```bash
export GPS_PORT=/dev/ttyUSB0
```

The default in `sensor_monitor.py` is:

```text
/dev/serial0
```

## Run the sensor

From the project directory:

```bash
python3 sensor_monitor.py
```

Then start the existing Firewatch web app separately:

```bash
uvicorn map:app --reload
```

Open the existing Firewatch page on the Pi or another computer that can
reach the Pi.

## Detection settings

These can be changed without editing the program:

```bash
export HOT_TEMP_C=80
export MIN_HOT_PERCENT=10
export REQUIRED_FRAMES=5
```

For example:

```bash
HOT_TEMP_C=80
MIN_HOT_PERCENT=10
REQUIRED_FRAMES=5
```

These are starting values, not a validated fire-alarm configuration. Test
the software with simulated/test thermal data before relying on it for
real-world safety decisions.

## How it works

```text
MLX90640
   |
   v
Thermal frame
   |
   v
Hot-pixel threshold
   |
   v
Persistent detection
   |
   +----> Raspberry Pi Camera -> JPEG
   |
   +----> GPS -> latitude/longitude
   |
   +----> clock -> timestamp
   |
   +----> CSV -> fire event
```
