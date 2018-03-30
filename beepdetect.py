#!/usr/bin/env python
# Beep sequence detector script for variable fan speed on a MightyBoard-based 3D printer.
# Make sure this is started at about the same time the PWM control server has fully started.
#
# Why and How:
# The MightyBoard totally lacks a way of sending serial commands from within G-code / X3G to an
#   external device. The only thing it can do, is toggle a device through the EXTRA output.
#   Therefore I came up with this somewhat crazy solution of using beeps from the buzzer to send
#   digital data to an external device like a fan PWM controller, a bit like a modem.
# The workflow is to configure your slicer to output G-code for a printer that supports variable
#   fan speed commands. Then, use a post-processing script to convert those commands into
#   sequences of 3 beeps played with the M300 command, using 4 specific frequencies and specific
#   timings (M300 S0 inserts a pause). Each sequence represents a 6-bit number which is then
#   mapped to a 64-level PWM value.
# This Python script, running on e.g. a Raspberry Pi, continuously analyses an FFT of audio input,
#   and sends a command to a PWM controller daemon whenever it detects a sequence of the signal
#   frequencies with the right timing. (The script can be easily adapted to listen to sequences of
#   4 beeps, which allows to send a whole whopping byte of information per sequence!)
# This works fine with a microphone, if it is placed very close to the buzzer, using a mount made
#   from a flexible material to minimize noises from the stepper motors being transferred to the
#   mic. A more robust alternative (that allows to remove the buzzer as well if it drives you
#   crazy), is to solder a direct electrical connection between the buzzer contacts and your audio
#   input (through a decoupling capacitor). This makes the system impervious against noises like
#   pigs squealing exactly at the high signal frequencies, which obviously happens all the time
#   while 3D printing.
# This script only consumes between 5 and 6% of CPU on a Raspberry Pi 3, so it can run many other
#   things at the same time. However, this should be run at a low 'nice' value to ensure it gets
#   priority over other processes.
#
# Alexander Thomas a.k.a. DrLex, https://www.dr-lex.be/
# Released under Creative Commons Attribution 4.0 International license.

import argparse
import pyaudio
import sys
from collections import deque
from numpy import short, fromstring, zeros
from scipy import fft
from operator import add
from requests import ConnectionError
from requests_futures.sessions import FuturesSession
from time import sleep, time

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
# To help determining a good threshold, run this script in calibration mode (-c option). It will
# show intensities for the signal frequencies if they exceed SENSITIVITY. If you play the
# BeepCalibration file on your printer, you should see responses for all the frequencies, and
# there should always be at least one response that is well above the sensitivity threshold.
SENSITIVITY = 10

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

#### End of configuration section ####


class DetectionState(object):
  """Manages detected sequence state."""
  def __init__(self, debug=False):
    self.debug = debug
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
    if self.time_index < 8:  # should be at least 186ms
      if self.debug:
        print "Reset because signal {} too soon ({}) after last reset".format(
              signal_id, self.time_index)
      self.reset()
      return False

    if self.detected:
      if self.current_sig_start and signal_id == self.detected[-1]:
        signal_length = 1 + self.time_index - self.current_sig_start
        # If thresholds would be perfectly tuned, we should only allow seeing the same frequency
        # across 2 consecutive windows. However, thresholds are never perfect, and the printer
        # sometimes stretches beeps when really busy, therefore allow 4 windows.
        if signal_length > 4:
          if self.debug:
            print "Reset because signal {} too long ({}x)".format(signal_id, signal_length)
          self.reset()
          return False
        if self.debug: print "Signal {} seen {}x, OK".format(signal_id, signal_length)
        self.last_sig_end = self.time_index
        return True
      t_since_last = self.time_index - self.last_sig_end
      if t_since_last < 3 or t_since_last > 7:
        # Should be between 70ms and 163ms: consider overlap due to detecting across 2 successive
        # windows, and allow reasonable stretch on playback of the silent part between beeps.
        if self.debug:
          print "Reset because signal {} too soon or late after previous signal {} ({})".format(
                signal_id, self.detected[-1], t_since_last)
        self.reset()
        return False
      if len(self.detected) > SEQUENCE_LENGTH - 1:
        if self.debug:
          print "Reset because of sequence with more than {} signals".format(SEQUENCE_LENGTH)
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
      seqstr = "".join([str(s) for s in self.detected])
      print "DETECTION: {} PWM {}%".format(seqstr, duty)
      if self.debug: print "  sequence value: {}".format(value)
      sys.stdout.flush()  # If there's one thing we want to see immediately in logs, it's this.
      self.reset()
      return duty
    elif len(self.detected) < SEQUENCE_LENGTH and t_since_last > 8:
      if self.debug:
        print "Reset because incomplete detection ({} signals)".format(len(self.detected))
      self.reset()
      return False
    return None


def open_input_stream(audio):
  # All examples and most programs I find, set frames_per_buffer to the same size as the chunks to
  # be processed. However, I have encountered sporadic input buffer overflows when doing this. It
  # appears PyAudio has only one extra buffer to fill up while waiting for the first one to be
  # emptied, and sometimes this gives just too little time to do our work in between two reads.
  # So to get a larger margin, I simply request a buffer twice as big as my chunk size, which
  # seems to work fine.
  return audio.open(format=pyaudio.paInt16,
                    channels=1, rate=SAMPLING_RATE, input=True,
                    frames_per_buffer=2 * NUM_SAMPLES)

def seq_to_value(sequence):
  # Converts a sequence of base 4 numbers to an integer.
  value = 0
  for i in xrange(0, len(sequence), 1):
    value += 4 ** i * sequence[-(i+1)]
  return value

def start_detecting(audio, options):
  debug = hasattr(options, 'debug')
  server_ip = options.ip
  server_port = options.port
  request_timeout = options.timeout
  sensitivity = options.sensitivity

  # To avoid having to do time system calls which may be expensive and return non-monotonic values,
  # all timings rely on the number of audio chunks processed, because the duration of one chunk is a
  # known fixed time.
  chunk_duration = float(NUM_SAMPLES) / SAMPLING_RATE
  request_countdown = int(round(float(options.timeout + 1) / chunk_duration))

  # HTTP requests to PWM server are done asynchronously. We really don't want to risk a buffer
  # overflow due to a slow response. Only when we're sure the request will be either done or has
  # timed out, check on it to print an error message if it failed.
  session = FuturesSession(max_workers=4)
  # Deques are very efficient for a FIFO like this.
  futures = deque()
  future_countdowns = deque()

  # This will probably barf many errors, you could clean up your asound.conf to get rid of some of
  # them, but they are harmless anyway.
  in_stream = open_input_stream(audio)

  # Perform a first request to the PWM server. This has three functions:
  # 1, warn early if the server isn't running;
  # 2, ensure the server is in enabled state;
  # 3, ensure these bits of code are already cached when we need to do our first real request.
  attempts = 3
  while server_ip and attempts:
    future = session.get('http://{}:{}/enable'.format(server_ip, server_port))
    try:
      req = future.result()
      if req.status_code == 200:
        print "OK: Successfully enabled PWM server."
        break
      else:
        print "ERROR: test request to PWM server failed with status {}".format(req.status_code)
    except ConnectionError as err:
      print "ERROR: the PWM server may be down? {}".format(err)
    attempts -= 1
    print "Attempts left: {}".format(attempts)
    if attempts: sleep(2)

  print "Beep sequence detector started."
  # TODO: make some basic logging handler so I don't need to call this all the time
  sys.stdout.flush()

  sig_bin_indices = range(0, len(SIG_BINS))
  empty_sig_bins = zeros(len(SIG_BINS))
  last_sig_bins = empty_sig_bins[:]  # Ensure to copy by value, not reference

  detections = DetectionState(debug)
  last_peak = None
  peak_count = 0

  while True:
    try:
      # Wait until we have enough samples to work with
      while in_stream.get_read_available() < NUM_SAMPLES: sleep(0.01)
    except IOError as err:
      # Most likely an overflow despite my attempts to avoid them. Only try to reopen the stream
      # once, because it could also be the sound device having been unplugged or some other fatal
      # error, and we don't want to hog the CPU with futile attempts to recover in such cases.
      print "ERROR while probing stream, retrying once to reopen stream: {}".format(err)
      sys.stdout.flush()
      in_stream = open_input_stream(audio)
      while in_stream.get_read_available() < NUM_SAMPLES: sleep(0.01)

    try:
      audio_data = fromstring(in_stream.read(NUM_SAMPLES, exception_on_overflow=True), dtype=short)
    except IOError as err:
      # I could restart the stream here, but the above except catcher already does this anyway.
      print "ERROR while reading audio data: {}".format(err)
      sys.stdout.flush()
      continue

    # Each data point is a signed 16 bit number, so divide by 2^15 to get more reasonable FFT
    # values. Because our input is real (no imaginary component), we can ditch the redundant
    # second half of the FFT.
    intensity = abs(fft(audio_data / 32768.0)[:NUM_SAMPLES/2])
    detections.time_increment()

    # Check any previously created requests to the PWM server.
    if future_countdowns:
      future_countdowns = deque([x - 1 for x in future_countdowns])
      # Handle at most one request per loop, CPU cycles are precioussss
      if future_countdowns[0] < 1:
        future_countdowns.popleft()
        future = futures.popleft()
        try:
          req = future.result()
          if req.status_code != 200:
            print "ERROR: request to PWM server failed with status {}".format(req.status_code)
        except ConnectionError as err:
          print "ERROR: could not connect to PWM server: {}".format(err)

    if DETECT_CONTINUOUS:
      # Find the peak frequency. If the same one occurs loud enough for long enough, assume the
      # buzzer is playing a song and we should reset detection state.
      peak = intensity[TONE_BIN_LOWER:TONE_BIN_UPPER].argmax() + TONE_BIN_LOWER
      if intensity[peak] > sensitivity:
        if peak == last_peak:
          peak_count += 1
          if peak_count > 2:
            if debug:
              print "Reset because of continuous tone (bin {}, {}x)".format(peak, peak_count)
            self.detections.reset()
            continue
        else:
          last_peak = peak
          peak_count = 1
      else:
        last_peak = None
        peak_count = 0

    # See if one of our signal frequencies occurred. Sum responses over current and previous
    # windows, to get a more consistent intensity value even when beep spans two windows.
    current_sig_bins = [intensity[SIG_BINS[i]] for i in sig_bin_indices]
    total_sig_bins = map(add, last_sig_bins, current_sig_bins)
    signals = [i for i in sig_bin_indices if total_sig_bins[i] > sensitivity]
    last_sig_bins = current_sig_bins[:]

    if len(signals) != 1:  # either 'silence' or multiple signals
      # If multiple occurred simultaneously, assume it is loud noise and treat as silence. This
      # means that unless you're using an electrical connection instead of a microphone, a loud
      # clap at the exact moment a beep is played, may cause its detection to be missed. This
      # seems lower risk than allowing any loud noise to appear to be a valid signal.
      if len(signals) > 1:
        if debug:
          print "Ignoring {} simultaneous signals".format(len(signals))
      # Check if we have a valid sequence
      duty = detections.check_silence()
      if duty is None:  # Nothing interesting happened
        continue
      last_sig_bins = empty_sig_bins[:]
      if duty is not False and server_ip:
        future_countdowns.append(request_countdown)
        futures.append(
          session.get('http://{}:{}/setduty?d={}'.format(server_ip, server_port, duty),
                      timeout=request_timeout)
        )
    else:  # 1 signal
      if not detections.check_signal(signals[0]):
        last_sig_bins = empty_sig_bins[:]


def calibration(audio, options):
  global chunks_recorded
  sensitivity = options.sensitivity

  sig_bin_indices = range(0, len(SIG_BINS))
  last_sig_bins = zeros(len(SIG_BINS))

  print "Calibration mode. Press CTRL-C to quit."
  print "Intensities for the {} signal frequencies if any exceeds {}:".format(
        len(SIG_BINS), sensitivity)

  in_stream = open_input_stream(audio)
  while True:
    while in_stream.get_read_available() < NUM_SAMPLES: sleep(0.01)
    try:
      audio_data = fromstring(in_stream.read(NUM_SAMPLES, exception_on_overflow=True),
                              dtype=short)
      chunks_recorded += 1
    except IOError as err:
      print "ERROR while reading audio data: {}".format(err)
      continue

    amax, amin = max(audio_data), min(audio_data)
    if amin == -32768 or amax == 32767: print "WARNING: clipping detected"

    intensity = abs(fft(audio_data / 32768.0)[:NUM_SAMPLES/2])
    current_sig_bins = [intensity[SIG_BINS[i]] for i in sig_bin_indices]
    total_sig_bins = map(add, last_sig_bins, current_sig_bins)
    signals = [i for i in sig_bin_indices if total_sig_bins[i] > sensitivity]
    last_sig_bins = current_sig_bins[:]
    if signals:
      out = ["{:.3f}".format(total_sig_bins[i]) for i in sig_bin_indices]
      print "  ".join(out)


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
                      default= PWM_SERVER_PORT)
  parser.add_argument('-t', '--timeout', type=int,
                      help='Timeout in seconds for requests to the PWM server',
                      default=PWM_REQUEST_TIMEOUT)
  parser.add_argument('-s', '--sensitivity', type=float, metavar='S',
                      help='Sensitivity threshold for detecting signals',
                      default=SENSITIVITY)

  args = parser.parse_args()

  audio = pyaudio.PyAudio()
  if hasattr(args, 'calibrate'):
    chunks_recorded = 0
    start_time = time()
    try:
      calibration(audio, args)
    except KeyboardInterrupt:
      elapsed_time = time() - start_time
      print "{} chunks in {} seconds = {:.3f}/s".format(
            chunks_recorded,
            elapsed_time,
            chunks_recorded/elapsed_time)
      print "If this is significantly lower than {:.3f}, you're in trouble.".format(
            float(SAMPLING_RATE)/NUM_SAMPLES)
  else:
    start_detecting(audio, args)
