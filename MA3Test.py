#!/usr/bin/env python
"""
Read a US Digital MA3-P12-125-B PWM encoder on a BeagleBone Black Rev C
running the old Debian 7 / kernel 3.8 image (no libgpiod available).

Encoder signal connected to P9_42 (GPIO0_7 -> sysfs gpio7).
The MA3 output must be reduced to 3.3 V before connecting it to the BeagleBone.

Runs on Python 2.7 or Python 3.2+. Run as root:
    python ma3_encoder_sysfs.py

Note: edge timestamps are taken in Python right after the kernel wakes the
script, so they include some scheduling jitter (same as the BoneScript
version, which used the same sysfs mechanism). The smoothing filter helps.


5v
gnd
p9_42 with voltage divider 1.8kohm and 3.3kohm
"""

from __future__ import print_function

import glob
import os
import select
import sys
import time

GPIO_NUM = 7  # P9_42 = GPIO0_7
GPIO_DIR = "/sys/class/gpio/gpio{0}".format(GPIO_NUM)

PRINT_THRESHOLD = 5  # only print when position changes by >= 5 counts
FILTER_ALPHA = 0.2  # smoothing: new = 0.8 * old + 0.2 * sample

if hasattr(time, "monotonic"):  # Python 3.3+

    def now_ns():
        return int(time.monotonic() * 1e9)

else:  # Python 2.7 / 3.2

    def now_ns():
        return int(time.time() * 1e9)


def write_file(path, text):
    with open(path, "w") as f:
        f.write(text)


def setup_pin():
    # Pin mux: P9_42 as GPIO input with pulldown (kernel 3.8 pinmux helper)
    matches = glob.glob("/sys/devices/ocp.*/P9_42_pinmux.*/state")
    if matches:
        write_file(matches[0], "gpio_pd")
    elif glob.glob("/sys/devices/ocp.*/bs_pinmode_P9_42_0x27_pinmux.*"):
        # Left behind by BoneScript's pinMode(): mode 7, pulldown, input
        print("P9_42 already muxed as GPIO input with pulldown (BoneScript overlay)")
    else:
        raise RuntimeError(
            "P9_42 is not muxed as a GPIO. Run the original BoneScript program "
            "once (it sets the pin up), then run this script."
        )

    # Export GPIO
    if not os.path.isdir(GPIO_DIR):
        write_file("/sys/class/gpio/export", str(GPIO_NUM))
        time.sleep(0.2)  # let udev/sysfs create the directory

    write_file(GPIO_DIR + "/direction", "in")
    write_file(GPIO_DIR + "/edge", "both")


def cleanup_pin():
    try:
        write_file(GPIO_DIR + "/edge", "none")
        write_file("/sys/class/gpio/unexport", str(GPIO_NUM))
    except (IOError, OSError):
        pass


class Ma3Decoder(object):
    """Turns timestamped rising/falling edges into a 12-bit position."""

    def __init__(self):
        self.rising_time = 0  # ns
        self.period = 0  # ns
        self.filtered_position = 0.0
        self.first_reading = True
        self.last_printed = -1

    def on_edge(self, rising, now):
        # Rising edge starts a PWM cycle
        if rising:
            previous_rising = self.rising_time
            self.rising_time = now
            if previous_rising != 0:
                self.period = self.rising_time - previous_rising
            return

        # Falling edge ends the high pulse
        if self.rising_time == 0 or self.period == 0:
            return

        pulse_width = now - self.rising_time

        # Convert nanoseconds to microseconds
        high_time = pulse_width / 1000.0
        total_time = self.period / 1000.0

        # Calculate the 12-bit encoder position
        position = ((high_time * 4098) / total_time) - 1
        position = max(0.0, min(4095.0, position))

        # Smooth the position
        if self.first_reading:
            self.filtered_position = position
            self.first_reading = False
        else:
            self.filtered_position = (
                (1 - FILTER_ALPHA) * self.filtered_position
                + FILTER_ALPHA * position
            )

        rounded = int(round(self.filtered_position))

        # Print only changes of at least PRINT_THRESHOLD counts
        if self.last_printed == -1 or abs(rounded - self.last_printed) >= PRINT_THRESHOLD:
            duty = (high_time / total_time) * 100
            print("Position: {0} / 4095, duty cycle: {1:.2f}%".format(rounded, duty))
            self.last_printed = rounded


def main():
    print("Starting encoder program")
    decoder = Ma3Decoder()

    try:
        setup_pin()
    except (IOError, OSError, RuntimeError) as err:
        print("Pin setup error: {0}".format(err), file=sys.stderr)
        sys.exit(1)

    print("Pin configured: P9_42 (gpio{0})".format(GPIO_NUM))

    f = open(GPIO_DIR + "/value", "r")
    poller = select.poll()
    poller.register(f.fileno(), select.POLLPRI | select.POLLERR)

    f.read()  # clear the initial pending event
    last_value = None

    try:
        while True:
            events = poller.poll(1000)  # 1 s timeout
            if not events:
                continue
            t = now_ns()  # timestamp as soon as we wake up
            f.seek(0)
            value = f.read(1)
            if value == last_value:
                continue  # missed edge or duplicate wakeup
            last_value = value
            decoder.on_edge(value == "1", t)
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        f.close()
        cleanup_pin()


if __name__ == "__main__":
    main()
