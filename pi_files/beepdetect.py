#!/usr/bin/env python3
"""
Beep sequence detector script for variable fan speed on a MightyBoard-based 3D printer.
Make sure this is started at about the same time the PWM control server has fully started.

Why and How:
The MightyBoard totally lacks a way of sending serial commands from within G-code / X3G to an
  external device. The only thing it can do, is toggle a device through the EXTRA output.
  Therefore I came up with this somewhat crazy solution of using beeps from the buzzer to send
  digital data to an external device like a fan PWM controller, a bit like a modem.
The workflow is to configure your slicer to output G-code for a printer that supports variable
  fan speed commands. Then, use a post-processing script to convert those commands into
  sequences of 3 beeps played with the M300 command, using 4 specific frequencies and specific
  timings (M300 S0 inserts a pause). Each sequence represents a 6-bit number which is then
  mapped to a 64-level PWM value.
This Python script, running on e.g. a Raspberry Pi, continuously analyses an FFT of audio input,
  and sends a command to a PWM controller daemon whenever it detects a sequence of the signal
  frequencies with the right timing. (The script can be easily adapted to listen to sequences of
  4 beeps, which allows to send a whole whopping byte of information per sequence!)
This works fine with a microphone, if it is placed very close to the buzzer, using a mount made
  from a flexible material to minimize noises from the stepper motors being transferred to the
  mic. A more robust alternative (that allows to remove the buzzer as well if it drives you
  crazy), is to solder a direct electrical connection between the buzzer contacts and your audio
  input (through a decoupling capacitor). This makes the system impervious against noises like
  pigs squealing exactly at the high signal frequencies, which obviously happens all the time
  while 3D printing.
This script only consumes between 5 and 6% of CPU on a Raspberry Pi 3, so it can run many other
  things at the same time. However, this should be run at a low 'nice' value to ensure it gets
  priority over other processes.

Alexander Thomas a.k.a. DrLex, https://www.dr-lex.be/
Released under Creative Commons Attribution 4.0 International license.
"""

# TODO: revise detection algorithm to allow even sloppier beep playback. If two M300 commands
#   within one sequence were stretched, it should still be deemed OK. To compensate for the extra
#   sloppiness, there should be a more strict test on silence between the beeps. Maybe the
#   continuous tone check should be enabled after all.
# TODO: calibration mode should check whether all the expected sequences could be detected (don't
#   need the same detection algorithm, can use something more crude).


import argparse
import logging
import os
import sys
from collections import deque
from operator import add
from time import sleep, time

import pyaudio
import requests
# pylint: disable=no-name-in-module
from numpy import short, fromstring, zeros
from scipy import fft
from requests_futures.sessions import FuturesSession


#### Defaults, either pass custom values as command-line parameters, or edit these. ####

PWM_SERVER_IP = "127.0.0.1"
PWM_SERVER_PORT = 8080

# Maximum seconds for performing a request to the PWM server. Because these requests are offloaded
# to a separate thread, this (plus 1 second) is also the time before the result will be checked
# and errors will be reported.
PWM_REQUEST_TIMEOUT = 4

# Sensitivity threshold for detecting signals. This is tricky! Too low threshold will lead to
# false detections, down to the point where even a slight echo of a real signal can cause
# problems. Obviously, too high threshold will lead to missed detections.
# Normally, the default should be fine if you configured the sound card as described in the README
# file. If you do want to verify it, run this script in calibration mode (-c option) and 'print'
# the BeepCalibration file. Exit the script (ctrl-C) when the LCD panel tells you to. Amidst the
# console output will be a suggested threshold value.
SENSITIVITY = 20.0

#### End of defaults section ####

#### Configuration section for fixed values ####

# Make sure this matches what your sound card can handle. Lower is actually better, because it
# means higher frequency resolution for the same size of FFT.
SAMPLING_RATE = 44100

# Number of samples in the FFT. This must be small enough to be able to detect a single beep, but
# a beep must not span more than 2 windows. The duration over which a single window is calculated,
# is NUM_SAMPLES/SAMPLING_RATE seconds.
# For 44.1k and 1024 samples, this is 23.2ms or 43.07 windows per second.
NUM_SAMPLES = 1024

# Indices of the frequency bins that are used for signals. The frequency for bin i is:
#   i*SAMPLING_RATE/NUM_SAMPLES.
SIG_BINS = [139, 151, 161, 172]

# Scale factors for each bin, applied before thresholding on sensitivity. These may vary between
# microphones and buzzers. When running the calibration procedure exactly as prescribed, you will
# automatically get suggested values to enter here. The scales are not used during calibration,
# which is why the calibration procedure should be run at a lower sensitivity setting.
SIG_SCALES = [1.0, 1.8, 2.9, 3.6]

# Indices of the bins that may contain frequencies produced by the buzzer. Anything outside this
# will be ignored. (Only used if DETECT_CONTINUOUS is enabled)
TONE_BIN_LOWER = 3
TONE_BIN_UPPER = 174

# If True, explicitly reset detection state when detecting a loud continuous tone.
# This is experimental and I do not recommend enabling it. The idea was to provide a barrier zone
#   around the moment the printer plays a song or error tone. However, in retrospect it only
#   really protects against an X3G file playing a song that happens to have signal frequencies in
#   it at the exact right timings, which is exceedingly unlikely. On the other hand, enabling this
#   check means the detector is temporarily deaf every time there is a very loud continuous tone
#   near the printer, e.g. from a loud music player.
DETECT_CONTINUOUS = False

# The number of beeps in a sequence. You probably shouldn't change this unless you plan to use
# this detector for other things.
SEQUENCE_LENGTH = 3

# When a signal appears to be detected, the intensity of half the signal frequency is also checked
# and if it is at least the signal intensity times this factor, the signal is considered a
# harmonic and rejected.
HARMONIC_FACTOR = 1.3

# Path to a file that signals whether an instance of this script is active. The location must be
# writable. The PID of the running instance will be written to the file.
LOCK_FILE = "/run/lock/beepdetect.lock"

#### End of configuration section ####

LOG = logging.getLogger('beepdetect')
LOG.setLevel(logging.INFO)
LOG_FORMAT = logging.Formatter('%(levelname)s: %(message)s')


class DetectionState(object):
    """Manages detected sequence state."""

    def __init__(self):
        self.reset()

    def reset(self):
        """A reset must be performed whenever we are certain the buzzer is playing a sound
        that is not part of a sequence."""
        # Factor between decoded sequence value and duty cycle percentage
        self.seq_scale_factor = 100.0 / (4**SEQUENCE_LENGTH - 1)
        # The number of chunks since the last reset is used as reference for sequence timings.
        self.time_index = 0
        self.detected = []
        self.current_sig_start = None
        self.last_sig_end = None

    def time_increment(self):
        """To be invoked when a new audio chunk has been analyzed, before using any of the
        following methods."""
        self.time_index += 1

    def check_signal(self, signal_id):
        """Update detection state when a signal was seen.
        Returns True if this signal might be part of a sequence, False otherwise."""
        if self.time_index < 7:  # should be at least 163ms
            LOG.debug("Reset because signal %d too soon (%d) after last reset",
                      signal_id, self.time_index)
            self.reset()
            return False

        if self.detected:
            if self.current_sig_start and signal_id == self.detected[-1]:
                signal_length = 1 + self.time_index - self.current_sig_start
                # If thresholds would be perfectly tuned, we should only allow seeing the same
                # frequency across 2 consecutive windows. However, thresholds are never perfect,
                # and the printer sometimes stretches beeps when really busy, therefore allow
                # 4 windows.
                if signal_length > 4:
                    LOG.debug("Reset because signal %d too long (%dx)", signal_id, signal_length)
                    self.reset()
                    return False
                LOG.debug("Signal %d seen %dx, OK", signal_id, signal_length)
                self.last_sig_end = self.time_index
                return True
            t_since_last = self.time_index - self.last_sig_end
            if t_since_last < 3 or t_since_last > 9:
                # Should be between 70ms and 209ms: consider overlap due to detecting across 2
                # successive windows, and allow reasonable stretch on playback of the silent
                # part between beeps.
                LOG.debug("Reset because signal %d too soon or late after previous signal %d (%d)",
                          signal_id, self.detected[-1], t_since_last)
                self.reset()
                return False
            if len(self.detected) > SEQUENCE_LENGTH - 1:
                LOG.debug("Reset because of sequence with more than %d signals", SEQUENCE_LENGTH)
                self.reset()
                return False

        self.detected.append(signal_id)
        if self.current_sig_start is None:
            self.current_sig_start = self.time_index
        self.last_sig_end = self.time_index
        return True

    def check_silence(self):
        """If detection state matches a valid sequence, return the duty cycle it represents
        as a floating-point percentage, else return either None if nothing was detected,
        or False if a partially detected sequence proved invalid."""
        self.current_sig_start = None
        if not self.detected:
            return None

        t_since_last = self.time_index - self.last_sig_end
        if len(self.detected) == SEQUENCE_LENGTH and t_since_last >= 8:
            # It's party time!
            value = seq_to_value(self.detected)
            duty = round(float(value) * self.seq_scale_factor, 2)
            LOG.info("DETECTION: %s PWM %g%%", "".join([str(s) for s in self.detected]), duty)
            LOG.debug("  sequence value: %d", value)
            self.reset()
            return duty
        elif len(self.detected) < SEQUENCE_LENGTH and t_since_last > 8:
            LOG.debug("Reset because incomplete detection (%d signals)", len(self.detected))
            self.reset()
            return False
        return None


def list_devices():
    """Outputs a list of all devices PyAudio can find that have an available input channel."""
    audio = pyaudio.PyAudio()
    info = audio.get_host_api_info_by_index(0)
    numdevices = info.get('deviceCount')
    LOG.info("Total number of devices: %d", numdevices)
    LOG.info("Available devices with inputs:")
    count = 0
    for i in range(0, numdevices):
        if audio.get_device_info_by_host_api_device_index(0, i).get('maxInputChannels') > 0:
            count += 1
            LOG.info("  Input Device id %d: %s",
                     i, audio.get_device_info_by_host_api_device_index(0, i).get('name'))
    if not count:
        LOG.warning("None found. Check whether your sound card is plugged in " +
                    "and not in use by another program.")

def open_input_stream(audio, options):
    """Create a PyAudio input stream on the device specified by options.device."""
    # All examples and most programs I find, set frames_per_buffer to the same size as the chunks
    # to be processed. However, I have encountered sporadic input buffer overflows when doing
    # this. It appears PyAudio has only one extra buffer to fill up while waiting for the first
    # one to be emptied, and sometimes this gives just too little time to do our work in between
    # two reads. So to get a larger margin, I simply request a buffer that is a multiple of my
    # chunk size, which seems to work fine.
    # Note: the buffer of the audio input device (see asound.conf) must be at least as large.
    device = options.device if hasattr(options, 'device') else None
    return audio.open(input_device_index=device, format=pyaudio.paInt16,
                      channels=1, rate=SAMPLING_RATE, input=True,
                      frames_per_buffer=4 * NUM_SAMPLES)

def seq_to_value(sequence):
    """Converts a sequence of base 4 numbers to an integer."""
    value = 0
    for i in range(0, len(sequence), 1):
        value += 4 ** i * sequence[-(i+1)]
    return value

def make_duty_request(futures, session, options, duty):
    """Append to the @futures deque an asynchronous request to the PWM server for changing
    the duty cycle."""
    futures.append(
        session.get('http://{}:{}/setduty?d={}&basic=1'.format(
                        options.ip, options.port, duty),
                    timeout=options.timeout))

def start_detecting(options):
    """Run the main detection loop."""
    server_ip = options.ip
    sensitivity = options.sensitivity

    # To avoid having to do time system calls which may be expensive and return non-monotonic
    # values, all timings rely on the number of audio chunks processed, because the duration of
    # one chunk is a known fixed time.
    chunk_duration = float(NUM_SAMPLES) / SAMPLING_RATE
    request_countdown = int(round(float(options.timeout + 1) / chunk_duration))

    # HTTP requests to PWM server are done asynchronously. We really don't want to risk a buffer
    # overflow due to a slow response. Only when we're sure the request will be either done or
    # has timed out, check on it to print an error message if it failed.
    session = FuturesSession(max_workers=4)
    # Deques are very efficient for a FIFO like this.
    futures = deque()
    future_countdowns = deque()

    # This will probably barf many errors, you could clean up your asound.conf to get rid of
    # some of them, but they are harmless anyway.
    audio = pyaudio.PyAudio()

    # Perform a first request to the PWM server. This has three functions:
    # 1, warn early if the server isn't running;
    # 2, ensure the server is in enabled state;
    # 3, ensure these bits of code are already cached when we need to do our first real request.
    attempts_left = 3
    while server_ip and attempts_left:
        future = session.get('http://{}:{}/enable?basic=1'.format(server_ip, options.port))
        try:
            req = future.result()
            if req.status_code == 200:
                LOG.info("OK: Successfully enabled PWM server.")
                break
            else:
                LOG.error("Test request to PWM server failed with status %s", req.status_code)
        except requests.ConnectionError as err:
            LOG.error("The PWM server may be down? %s", err)
        attempts_left -= 1
        LOG.error("Attempts left: %d", attempts_left)
        if attempts_left:
            sleep(2)

    LOG.info("Beep sequence detector started.")

    # Also keep track of the halved signal frequencies. If there is a response at those
    # frequencies that is at least nearly as strong as the signal frequency, then the signal
    # frequency is probably a harmonic from a beep played at a lower frequency.
    all_bins = SIG_BINS[:] + [i // 2 for i in SIG_BINS]
    all_bin_indices = list(range(0, len(all_bins)))
    sig_bin_indices = list(range(0, len(SIG_BINS)))
    empty_bins = zeros(2 * len(all_bins))
    last_bins = empty_bins[:]  # Ensure to copy by value, not reference

    detections = DetectionState()
    last_peak = None
    peak_count = 0
    current_duty = None
    in_stream = open_input_stream(audio, options)

    while True:
        try:
            # Wait until we have enough samples to work with. Sleep at most 1/4 of the duration
            # of one audio chunk, otherwise input overflow risk increases.
            while in_stream.get_read_available() < NUM_SAMPLES:
                sleep(0.005)
        except IOError as err:
            # Most likely an overflow despite my attempts to avoid them. Only try to reopen the
            # stream once, because it could also be the sound device having been unplugged or
            # some other fatal error, and we don't want to hog the CPU with futile attempts to
            # recover in such cases.
            LOG.error("Failed to probe stream: %s. Now retrying once to reopen stream...", err)
            in_stream = open_input_stream(audio, options)
            while in_stream.get_read_available() < NUM_SAMPLES:
                sleep(0.005)

        try:
            audio_data = fromstring(in_stream.read(NUM_SAMPLES, exception_on_overflow=True),
                                    dtype=short)
        except IOError as err:
            # I could restart the stream here, but the above except catcher already does it anyway.
            LOG.error("Could not read audio data: %s", err)
            continue

        # Each data point is a signed 16 bit number, so divide by 2^15 to get more reasonable FFT
        # values. Because our input is real (no imaginary component), we can ditch the redundant
        # second half of the FFT.
        intensity = abs(fft(audio_data / 32768.0)[:NUM_SAMPLES // 2])
        detections.time_increment()

        # Check any previously created requests to the PWM server.
        if future_countdowns:
            future_countdowns = deque([x - 1 for x in future_countdowns])
            # Handle at most one request per loop, CPU cycles are precioussss
            if future_countdowns[0] < 1:
                future_countdowns.popleft()
                future = futures.popleft()
                success = False
                try:
                    req = future.result()
                    if req.status_code != 200:
                        LOG.error("Request to PWM server failed with status %d", req.status_code)
                    else:
                        success = True
                except requests.ConnectionError as err:
                    LOG.error("Could not connect to PWM server: %s", err)
                if not success and not future_countdowns and attempts_left:
                    # The request failed and no newer ones are queued. Because things are handled
                    # in a sequential manner, there is no risk of a race condition by retrying.
                    LOG.info("Retrying the request, %d attempts left", attempts_left)
                    future_countdowns.append(request_countdown)
                    make_duty_request(futures, session, options, current_duty)
                    attempts_left -= 1

        if DETECT_CONTINUOUS:
            # Find the peak frequency. If the same one occurs loud enough for long enough,
            # assume the buzzer is playing a song and we should reset detection state.
            peak = intensity[TONE_BIN_LOWER:TONE_BIN_UPPER].argmax() + TONE_BIN_LOWER
            if intensity[peak] > sensitivity:
                if peak == last_peak:
                    peak_count += 1
                    if peak_count > 2:
                        LOG.debug("Reset because of continuous tone (bin %d, %dx)",
                                  peak, peak_count)
                        detections.reset()
                        continue
                else:
                    last_peak = peak
                    peak_count = 1
            else:
                last_peak = None
                peak_count = 0

        # See if one of our signal frequencies occurred. Sum responses over current and previous
        # windows, to get a more consistent intensity value even when beep spans two windows.
        current_bins = [intensity[all_bins[i]] for i in all_bin_indices]
        total_bins = list(map(add, last_bins, current_bins))
        signals = [i for i in sig_bin_indices if total_bins[i] * SIG_SCALES[i] > sensitivity]
        last_bins = current_bins[:]

        if len(signals) != 1:  # either 'silence' or multiple signals
            # If multiple occurred simultaneously, assume it is loud noise and treat as silence.
            # This means that unless you're using an electrical connection instead of a
            # microphone, a loud clap at the exact moment a beep is played, may cause its
            # detection to be missed. This seems lower risk than allowing any loud noise to
            # appear to be a valid signal.
            if len(signals) > 1:
                LOG.debug("Ignoring %d simultaneous signals", len(signals))
            # Check if we have a valid sequence
            duty = detections.check_silence()
            if duty is None:  # Nothing interesting happened
                continue
            last_bins = empty_bins[:]
            if duty is not False and server_ip:
                current_duty = duty
                future_countdowns.append(request_countdown)
                make_duty_request(futures, session, options, duty)
                attempts_left = 2
        else:  # 1 signal
            harmonic_ratio = total_bins[len(SIG_BINS) + signals[0]] / total_bins[signals[0]]
            if harmonic_ratio > HARMONIC_FACTOR:
                LOG.debug("Reset because apparent signal %d is actually a harmonic (%.1f)",
                          signals[0], harmonic_ratio)
                detections.reset()
                continue
            if not detections.check_signal(signals[0]):
                last_bins = empty_bins[:]


def calibration(options):
    """Run the calibration procedure."""
    sensitivity = options.sensitivity

    sig_bins_groups = [[x-1, x, x+1] for x in SIG_BINS]
    sig_bins_ext = [sig_bin for group in sig_bins_groups for sig_bin in group]  # flatten it
    sig_bin_indices = list(range(0, len(sig_bins_ext)))
    last_sig_bins = zeros(len(sig_bins_ext))
    sum_sig_bins = last_sig_bins[:]
    count_sig_bins = [0] * len(sig_bins_ext)
    global_divider = 0
    clipped = False

    LOG.info("==== Calibration mode ====")
    LOG.info("""You should now 'print' a file that plays each of the 4 beep signals
    repeatedly an equal number of times. Make sure the printer does not play
    any other sounds.
    Press CTRL-C when done.
""")
    # Because the calibration procedure doesn't use SIG_SCALES, reduce the normal sensitivity
    # value to ensure we catch all signals.
    LOG.info("Using 1/4th of the normal sensitivity value %g.", sensitivity)
    sensitivity /= 4.0
    LOG.info("Intensities for the %s signal frequencies if any exceeds %g:",
             len(SIG_BINS), sensitivity)

    audio = pyaudio.PyAudio()
    in_stream = open_input_stream(audio, options)
    chunks_recorded = 0
    start_time = time()

    try:
        while True:
            while in_stream.get_read_available() < NUM_SAMPLES:
                sleep(0.005)
            try:
                audio_data = fromstring(in_stream.read(NUM_SAMPLES, exception_on_overflow=True),
                                        dtype=short)
                chunks_recorded += 1
            except IOError as err:
                LOG.error("Could not read audio data: %s", err)
                continue

            amax, amin = max(audio_data), min(audio_data)
            if amin == -32768 or amax == 32767:
                clipped = True
                LOG.warning("Clipping detected")
            elif amin == 0 and amax == 0:
                LOG.warning("Perfect silence detected, this is highly unlikely")

            intensity = abs(fft(audio_data / 32768.0)[:NUM_SAMPLES // 2])
            current_sig_bins = [intensity[sig_bins_ext[i]] for i in sig_bin_indices]
            total_sig_bins = list(map(add, last_sig_bins, current_sig_bins))
            signals = [i for i in sig_bin_indices if total_sig_bins[i] > sensitivity]
            last_sig_bins = current_sig_bins[:]
            if signals:
                # Only add to the statistics if there is any 'detected' signal, to avoid
                # accumulating noise
                for i in signals:
                    sum_sig_bins[i] += total_sig_bins[i]
                    count_sig_bins[i] += 1
                global_divider += 1
                out = ["{:.3f}".format(total_sig_bins[i])
                       for i in sig_bin_indices if (i + 2) % 3 == 0]
                LOG.info("  ".join(out))
    except KeyboardInterrupt:
        elapsed_time = time() - start_time
        LOG.info("-----")
        LOG.info("Exiting calibration mode and generating statistics...")

    LOG.info("%d chunks in %d seconds = %.3f/s",
             chunks_recorded,
             elapsed_time,
             chunks_recorded/elapsed_time)
    LOG.info("  If this is significantly lower than %.3f, you're in trouble.",
             float(SAMPLING_RATE)/NUM_SAMPLES)

    if clipped:
        LOG.warning("Too loud signal has been detected. If only valid beep sequences were \
  played, try again after reducing input gain in alsamixer. (It is OK to have clipping on other \
  sounds than the PWM sequences.)")

    avg_bin_intensities = [sum_sig_bins[i] / max(count_sig_bins[i], 1)
                           for i in range(1, len(sum_sig_bins), 3)]
    LOG.info("Average intensities for the signal bins when above the threshold:")
    LOG.info(", ".join(["{:.3f}".format(i) for i in avg_bin_intensities]))
    LOG.info("-> after applying current SIG_SCALES:")
    LOG.info(", ".join(["{:.3f}".format(i * j)
                        for i, j in zip(avg_bin_intensities, SIG_SCALES)]))
    max_avg_intensity = max(avg_bin_intensities)
    try:
        avg_bin_intensities.index(0.0)
        LOG.warning("Not all bins had detections, can suggest neither SIG_SCALES nor threshold!")
    except ValueError:
        # This is not the exception, it's the rule! I guess this is Pythonic...
        suggest_scales = [max_avg_intensity / i for i in avg_bin_intensities]
        LOG.info("-> suggested values for SIG_SCALES:")
        LOG.info(", ".join(["{:.1f}".format(i) for i in suggest_scales]))
        normalized_intensities = [i * j for i, j in zip(avg_bin_intensities, suggest_scales)]
        suggest_sensitivity = min(normalized_intensities) / 3
        LOG.info("-> suggested sensitivity: %.1f", suggest_sensitivity)

    LOG.info("-----")
    bins_vs_intensities = {sum_sig_bins[i]: sig_bins_ext[i] for i in sig_bin_indices}
    sorted_bins = [value for _, value in sorted(iter(bins_vs_intensities.items()), reverse=True)]
    LOG.info(
        "Bins, including neighboring ones, sorted by average response intensity from high to low:")
    LOG.info(" > ".join(map(str, sorted_bins)))
    for group in sig_bins_groups:
        try:
            pivot = sorted_bins.index(group[1])
        except ValueError:
            LOG.warning("Bin %d had no signals", group[1])
            continue
        better = None
        for i in [0, 2]:
            try:
                if sorted_bins.index(group[i]) < pivot:
                    better = group[i]
            except ValueError:
                pass
        if better:
            LOG.info("Bin %d appears to be better than bin %d", better, group[1])
        else:
            LOG.info("Bin %d looks good", group[1])


def clean_exit():
    """To be invoked when the program is about to stop."""
    LOG.info("Exiting...")
    if os.path.isfile(LOCK_FILE):
        os.unlink(LOCK_FILE)

def terminated(num, _):
    """SIGTERM signal handler."""
    LOG.warning("Caught signal %d", num)
    sys.exit(0)  # This will trigger clean_exit through atexit.

def create_lock_file():
    """If the expected lock file path exists, try to create a lock file and exit if there is
    one already; if the path is not writable, don't bother trying to create the lock file,
    just print a warning and continue."""
    if os.access(os.path.dirname(LOCK_FILE), os.W_OK):
        # Prevent two instances from trying to run at the same time, also useful to allow
        # pwm_server.py to show a warning if this daemon is not active.
        if os.path.isfile(LOCK_FILE):
            LOG.fatal(
                "Another instance is already running, or has exited without cleaning up its lock file at %s",
                LOCK_FILE)
            sys.exit(1)
        with open(LOCK_FILE, "w") as file_handle:
            file_handle.write(str(os.getpid()))
    else:
        # Do not make this a fatal error to facilitate testing on other platforms.
        LOG.warning(
            "Not creating a lockfile at %s because the directory is not writable or does not exist.",
            LOCK_FILE)
    import atexit
    import signal
    atexit.register(clean_exit)
    signal.signal(signal.SIGTERM, terminated)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Beep sequence detector script for variable fan speed on a MightyBoard-based 3D printer.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        argument_default=argparse.SUPPRESS)
    # SUPPRESS hides useless defaults in help text, the downside is needing to use hasattr().
    parser.add_argument('-c', '--calibrate', action='store_true',
                        help='Enable calibration mode')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='Enable debug output')
    parser.add_argument('-i', '--ip',
                        help='IP of the PWM server. Set to empty string to disable server requests.',
                        default=PWM_SERVER_IP)
    parser.add_argument('-p', '--port', type=int,
                        help='Port of the PWM server',
                        default=PWM_SERVER_PORT)
    parser.add_argument('-t', '--timeout', type=int,
                        help='Timeout in seconds for requests to the PWM server',
                        default=PWM_REQUEST_TIMEOUT)
    parser.add_argument('-s', '--sensitivity', type=float, metavar='S',
                        help='Sensitivity threshold for detecting signals',
                        default=SENSITIVITY)
    parser.add_argument('-D', '--device', type=int,
                        help='Use this device ID instead of the default input device')
    parser.add_argument('-L', '--list_devices', action='store_true',
                        help='List available devices with inputs')

    args = parser.parse_args()

    # Send info and debug messages to stdout, warning and above to stderr.
    handler_info = logging.StreamHandler(sys.stdout)
    if hasattr(args, 'debug'):
        handler_info.setLevel(logging.DEBUG)
        LOG.setLevel(logging.DEBUG)
    handler_info.addFilter(lambda record: record.levelno <= logging.INFO)
    handler_warn = logging.StreamHandler()
    handler_warn.setLevel(logging.WARNING)
    handler_info.setFormatter(LOG_FORMAT)
    handler_warn.setFormatter(LOG_FORMAT)
    LOG.addHandler(handler_info)
    LOG.addHandler(handler_warn)
    LOG.debug("Debug output enabled")

    if hasattr(args, 'list_devices'):
        list_devices()
        sys.exit(0)

    create_lock_file()
    if hasattr(args, 'calibrate'):
        calibration(args)
    else:
        start_detecting(args)
