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

include voltage divider. R1 = 1kohm, R2 = 2kohms. encoder signal at start, beagle bone after r1, then everything to ground through r2

works on outdated beagle bones. however has delay in readings and reads jitter while stationary
"""

from __future__ import print_function

import glob
import os
import select
import sys
import time

GPIO_NUM = 7  # P9_42 = GPIO0_7
GPIO_DIR = "/sys/class/gpio/gpio{0}".format(GPIO_NUM)

PRINT_THRESHOLD = 15  # only print when position changes by >= this many counts
FILTER_ALPHA = 0.1  # smoothing: new = 0.9 * old + 0.1 * sample
MEDIAN_WINDOW = 31  # median of the last N samples rejects timing spikes
PERIOD_ALPHA = 0.05  # the PWM period is fixed, so average it heavily

# The MA3 PWM period is about 4098 us (244 Hz). Any cycle far outside this
# window is a glitch or a missed edge, so it is discarded.
PERIOD_MIN_NS = 3500000
PERIOD_MAX_NS = 4700000

# Optional: override the print threshold from the command line, e.g.
#     python encoder.py 30
if len(sys.argv) > 1:
    try:
        PRINT_THRESHOLD = int(sys.argv[1])
    except ValueError:
        print("Usage: python encoder.py [print_threshold_counts]")
        sys.exit(1)

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
        self.recent = []
        self.rejected = 0  # glitch / missed-edge cycles thrown away

    def on_edge(self, rising, now):
        # Rising edge starts a PWM cycle
        if rising:
            previous_rising = self.rising_time
            self.rising_time = now
            if previous_rising != 0:
                measured = self.rising_time - previous_rising
                if not (PERIOD_MIN_NS <= measured <= PERIOD_MAX_NS):
                    # Glitch or missed edge: discard this cycle
                    self.rejected += 1
                    self.period = 0
                    return
                if self.period == 0:
                    self.period = measured
                else:
                    self.period = (
                        (1 - PERIOD_ALPHA) * self.period
                        + PERIOD_ALPHA * measured
                    )
            return

        # Falling edge ends the high pulse
        if self.rising_time == 0 or self.period == 0:
            return

        pulse_width = now - self.rising_time

        # A pulse longer than the whole period cannot be real
        if pulse_width <= 0 or pulse_width > self.period:
            self.rejected += 1
            return

        # Convert nanoseconds to microseconds
        high_time = pulse_width / 1000.0
        total_time = self.period / 1000.0

        # Calculate the 12-bit encoder position
        position = ((high_time * 4098) / total_time) - 1
        position = max(0.0, min(4095.0, position))

        # Reject timing spikes: median of the last few samples, then smooth
        self.recent.append(position)
        if len(self.recent) > MEDIAN_WINDOW:
            self.recent.pop(0)
        median = sorted(self.recent)[len(self.recent) // 2]

        if self.first_reading:
            self.filtered_position = median
            self.first_reading = False
        else:
            self.filtered_position = (
                (1 - FILTER_ALPHA) * self.filtered_position
                + FILTER_ALPHA * median
            )

        rounded = int(round(self.filtered_position))

        # Print only changes of at least PRINT_THRESHOLD counts
        if self.last_printed == -1 or abs(rounded - self.last_printed) >= PRINT_THRESHOLD:
            duty = (high_time / total_time) * 100
            print("Position: {0} / 4095, duty cycle: {1:.2f}%".format(rounded, duty))
            self.last_printed = rounded


def main():
    print("Starting encoder program (v5: glitch gate, median {0}, "
          "print threshold {1} counts)".format(MEDIAN_WINDOW, PRINT_THRESHOLD))
    decoder = Ma3Decoder()

    try:
        setup_pin()
    except (IOError, OSError, RuntimeError) as err:
        print("Pin setup error: {0}".format(err), file=sys.stderr)
        sys.exit(1)

    print("Pin configured: P9_42 (gpio{0})".format(GPIO_NUM))

    # Unbuffered file descriptor: buffered file objects can return a stale
    # cached value after seek(0) instead of re-reading the sysfs file.
    fd = os.open(GPIO_DIR + "/value", os.O_RDONLY)
    poller = select.poll()
    poller.register(fd, select.POLLPRI | select.POLLERR)

    os.read(fd, 1)  # clear the initial pending event
    last_value = None
    idle = 0

    try:
        while True:
            events = poller.poll(1000)  # 1 s timeout
            if not events:
                idle += 1
                if idle % 5 == 0:
                    os.lseek(fd, 0, os.SEEK_SET)
                    raw = os.read(fd, 1)
                    print("Waiting for edges... no interrupt in {0} s "
                          "(pin currently reads {1!r})".format(idle, raw))
                continue
            idle = 0
            t = now_ns()  # timestamp as soon as we wake up
            os.lseek(fd, 0, os.SEEK_SET)
            value = os.read(fd, 1)
            if value == last_value:
                continue  # missed edge or duplicate wakeup
            last_value = value
            decoder.on_edge(value == b"1", t)
    except KeyboardInterrupt:
        print("\nStopped ({0} bad cycles discarded)".format(decoder.rejected))
    finally:
        os.close(fd)
        cleanup_pin()


if __name__ == "__main__":
    main()
