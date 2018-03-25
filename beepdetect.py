#!/usr/bin/env python
# Beep sequence detector script for variable fan speed on a MightyBoard-based 3D printer.
# Make sure this is started after the PWM control server has fully started.
#
# Why and How:
# The MightyBoard totally lacks a way of sending serial commands from within G-code / X3G to an external device. The
#   only thing it can do, is toggle a device through the EXTRA output. Therefore I came up with this somewhat crazy
#   solution of using beeps from the buzzer to send digital data to an external device like a fan PWM controller.
# The workflow is to configure your slicer to output G-code for a printer that supports variable fan speed commands.
#   Then, use a post-processing script to convert those commands into sequences of 3 beeps played with the M300
#   command, using 4 specific frequencies and specific timings (M300 S0 inserts a pause). Each sequence represents a
#   6-bit number which is then mapped to a 64-level PWM value.
# This Python script, running on e.g. a Raspberry Pi, continuously analyses an FFT of the audio input, and sends a
#   command to a PWM controller daemon whenever it detects a sequence of the signal frequencies with the right timing.
#   (The script can be easily adapted to listen to sequences of 4 beeps, which allows to send a whole whopping byte of
#   information per sequence!)
# In theory this should work with a microphone, if it is placed very close to the buzzer, with a mount made from a
#   flexible material to minimize noises from the stepper motors being transferred to the mic. A more robust
#   alternative (that allows to remove the buzzer as well if it drives you crazy), is to solder a direct electrical
#   connection between the buzzer contacts and your audio input (you should include a decoupling capacitor). This
#   makes the system impervious against noises like pigs squealing exactly at the high signal frequencies, which
#   obviously happens all the time while 3D printing.
# This script only consumes between 5 and 6% of CPU on a Raspberry Pi 3, so you can use it for many other things at the
#   same time. However, it should be run at a low 'nice' value to ensure it gets priority over other processes.

import pyaudio
import sys
from collections import deque
from numpy import short, fromstring, zeros
from scipy import fft
from operator import add
from requests import ConnectionError
from requests_futures.sessions import FuturesSession
from time import sleep, time

### Configuration section ###

PWM_SERVER_IP = "127.0.0.1"
PWM_SERVER_PORT = 8080
# Maximum seconds for performing a request to the PWM server. Because these requests are offloaded to a separate
# thread, this (plus 1 second) is also the time before the result will be checked and errors will be reported.
PWM_REQUEST_TIMEOUT = 4

# This is tricky!!! Too low threshold will lead to false detections of the signal frequencies, down to the point where
# even a slight echo of a real signal can cause problems. Obviously, too high threshold will lead to missed detections.
# We'll need some way to calibrate this. I would play the 4 signal frequencies through the buzzer, while a simplified
# script simply shows the FFT responses. You should then pick a sensitivity value that is slightly below the lowest FFT
# intensity the script reports.
SENSITIVITY = 10

# Indices of the frequency bins that are used for signals. The frequency for bin i is i*SAMPLING_RATE/NUM_SAMPLES.
SIG_BINS = [139, 150, 161, 172]

# Make sure this matches what your sound card can handle. Lower is actually better, because it means higher frequency
# resolution for the same size of FFT.
SAMPLING_RATE = 44100

# Number of samples in the FFT. This must be small enough to be able to detect a single beep, but a beep must not span
# more than 2 windows. The duration over which a single window is calculated, is NUM_SAMPLES/SAMPLING_RATE seconds.
# For 44.1k and 1024 samples, this is 23.2ms or 43.07 windows per second.
NUM_SAMPLES = 1024

# Indices of the bins that may contain frequencies produced by the buzzer. Anything outside this will be ignored.
TONE_BIN_LOWER = 3
TONE_BIN_UPPER = 174

# Enable debug output, useful when debugging this script. You should disable this under normal use, because the less
# IO this program outputs, the less risk of something getting delayed.
DEBUG = False

# If True, explicitly reset detection state when detecting a loud continuous tone.
# This is experimental and I do not recommend enabling it. The idea was to provide a barrier zone around the moment
#   when the printer is playing a song or error tone. However, in retrospect it only really protects against an X3G
#   file playing a song that happens to have the signal frequencies in it at the exact right timings, which is
#   exceedingly unlikely. On the other hand, enabling this check means the detector will be temporarily deaf every time
#   someone whistles, yells, or plays loud music near the printer.
DETECT_CONTINUOUS = False

### End of configuration section ###

# To avoid having to do time system calls which may be expensive and return non-monotonic values, all timings rely on
# the number of audio chunks processed, because the duration of one chunk is a known fixed time.
chunk_duration = float(NUM_SAMPLES) / SAMPLING_RATE
request_countdown = int(round(float(PWM_REQUEST_TIMEOUT + 1) / chunk_duration))
chunks_recorded = 0
pa = pyaudio.PyAudio()


def open_input_stream():
  # All examples and most programs I find, set frames_per_buffer to the same size as the chunks to be processed.
  # However, I have encountered sporadic input buffer overflows when doing this. It appears that PyAudio has only one
  # extra buffer to fill up while waiting for the first one to be emptied, and sometimes this gives us just too little
  # time to do our work in between two reads.
  # So to get a larger margin, I simply request a buffer twice as big as my chunk size, which seems to work fine.
  return pa.open(format=pyaudio.paInt16,
                 channels=1, rate=SAMPLING_RATE,
                 input=True,
                 frames_per_buffer=2 * NUM_SAMPLES)


def seq_to_value(sequence):
  # Converts a sequence of base 4 numbers to an integer.
  value = 0
  for i in xrange(0, len(sequence), 1):
    value += 4 ** i * sequence[-(i+1)]
  return value


def start_detecting():
  # HTTP requests to the PWM server are done asynchronously. We really don't want to risk a buffer overflow due to a
  # slow response. Only when we're sure the request will be either done or has timed out, we check on it to print an
  # error message if it failed.
  session = FuturesSession(max_workers=4)
  # Deques are very efficient for a FIFO like this.
  futures = deque()
  future_countdowns = deque()

  # This will probably barf many errors, you could clean up your asound.conf to get rid of some of them, but they are
  # harmless anyway.
  in_stream = open_input_stream()

  # Perform a first request to the PWM server. This has three functions:
  # 1, warn early if the server isn't running;
  # 2, ensure the server is in enabled state;
  # 3, ensure these bits of code are already cached when we need to do our first real request.
  attempts = 3
  while attempts:
    future = session.get('http://{}:{}/enable'.format(PWM_SERVER_IP, PWM_SERVER_PORT))
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

  print("Beep sequence detector working. Press CTRL-C to quit.")
  sys.stdout.flush()  # TODO: make some basic logging handler so I don't need to call this all the time

  sig_bin_indices = range(0, len(SIG_BINS))
  empty_sig_bins = zeros(len(SIG_BINS))
  last_sig_bins = empty_sig_bins[:]  # Ensure to copy by value, not reference

  detected = []
  last_peak = None
  peak_count = 0
  # For beep timings, use the last reset moment as reference.
  t_since_reset = 0

  while True:
    try:
      # Wait until we have enough samples to work with
      while in_stream.get_read_available() < NUM_SAMPLES: sleep(0.01)
    except IOError as err:
      # This will most likely be an overflow despite my attempts to avoid them. Only try to reopen the stream once,
      # because it could also be the sound device having been unplugged or some other fatal error, and we don't want to
      # hog the CPU with our futile attempts to recover in such cases.
      print "ERROR while probing stream, retrying once to reopen stream: {}".format(err)
      sys.stdout.flush()
      in_stream = open_input_stream()
      while in_stream.get_read_available() < NUM_SAMPLES: sleep(0.01)

    try:
      audio_data = fromstring(in_stream.read(NUM_SAMPLES, exception_on_overflow=True), dtype=short)
    except IOError as err:
      # I could restart the stream here, but the above except catcher already does this anyway.
      print "ERROR while reading audio data: {}".format(err)
      sys.stdout.flush()
      continue

    # Each data point is a signed 16 bit number, so we can divide by 2^15 to get more reasonable FFT values.
    # Because our input is real (no imaginary component), we can ditch the redundant second half of the FFT.
    intensity = abs(fft(audio_data / 32768.0)[:NUM_SAMPLES/2])
    t_since_reset += 1

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
      # Find the peak frequency. If the same one occurs loud enough for long enough, assume the buzzer is playing
      # a song and we should reset detection state.
      peak = intensity[TONE_BIN_LOWER:TONE_BIN_UPPER].argmax() + TONE_BIN_LOWER
      if intensity[peak] > SENSITIVITY:
        if peak == last_peak:
          peak_count += 1
          if peak_count > 2:
            if DEBUG: print "Reset because of continuous tone (bin {}, {}x)".format(peak, peak_count)
            detected = []
            last_sig_bins = empty_sig_bins[:]
            t_since_reset = 0
            continue
        else:
          last_peak = peak
          peak_count = 1
      else:
        last_peak = None
        peak_count = 0

    # See if one of our signal frequencies occurred. Take sum of responses over current and previous windows, to get
    # a more consistent intensity value even when a beep spans two windows.
    current_sig_bins = [intensity[SIG_BINS[i]] for i in sig_bin_indices]
    total_sig_bins = map(add, last_sig_bins, current_sig_bins)
    signals = [i for i in sig_bin_indices if total_sig_bins[i] > SENSITIVITY]
    last_sig_bins = current_sig_bins[:]
    if signals:
      if len(signals) > 1:
        # If multiple occurred simultaneously, assume it is loud noise and ignore it. This means that unless you're
        # using an electrical connection instead of a microphone, a loud clap at the exact moment a beep is played,
        # will cause its detection to be missed. This seems lower risk than allowing any loud noise to appear to be
        # a valid signal.
        if DEBUG: print "Ignoring multiple simultaneous signals"
        continue;
      if t_since_reset < 8:  # should be at least 186ms
        if DEBUG: print "Reset because signal {} too soon ({}) after last reset".format(signals[0], t_since_reset)
        detected = []
        last_sig_bins = empty_sig_bins[:]
        t_since_reset = 0
        continue
      if detected:
        t_since_last = t_since_reset - detected[-1][0]
        if t_since_last <= 2 and detected[-1][1] == signals[0]:
          # If thresholds would be perfectly tuned, we should only allow seeing the same frequency across 2 consecutive
          # windows. However, to avoid problems with too low thresholds, allow one extra window.
          if DEBUG: print "Seen signal {} again, OK".format(signals[0])
          continue
        if t_since_last < 4 or t_since_last > 7:  # should be between 93ms and 163ms (allow reasonable delay on playback)
          if DEBUG: print "Reset because signal {} too soon or late after previous signal {} ({})".format(signals[0], detected[-1][1], t_since_last)
          detected = []
          last_sig_bins = empty_sig_bins[:]
          t_since_reset = 0
          continue
        if len(detected) > 2:
          if DEBUG: print "Reset because of sequence with more than 3 signals"
          detected = []
          last_sig_bins = empty_sig_bins[:]
          t_since_reset = 0
          continue
      detected.append([t_since_reset, signals[0]])

    elif detected:
      # Nothing special happened. Check if our sequence is valid if we have one.
      t_since_last = t_since_reset - detected[-1][0]
      if len(detected) == 3 and t_since_last >= 8:
        # It's party time!
        sequence = [sig[1] for sig in detected]
        value = seq_to_value(sequence)
        duty = round(float(value) * 100.0 / 64, 2)
        print "DETECTION: PWM {}%".format(duty)
        if DEBUG: print "  {} -> {} = {}%".format("-".join([str(s) for s in sequence]), value, duty)
        sys.stdout.flush()  # If there's one thing we want to see immediately in logs, it's this.
        future_countdowns.append(request_countdown)
        futures.append(session.get('http://{}:{}/setduty?d={}'.format(PWM_SERVER_IP, PWM_SERVER_PORT, duty),
                                   timeout=PWM_REQUEST_TIMEOUT))
        detected = []
        t_since_reset = 0
      elif len(detected) < 3 and t_since_last > 7:
        if DEBUG: print "Reset because unfinished detection ({} signals)".format(len(detected))
        detected = []
        t_since_reset = 0
        continue


def calibration():
  # This needs to be improved, must be able to pass sensitivity as CLI argument
  global chunks_recorded
  in_stream = open_input_stream()

  sig_bin_indices = range(0, len(SIG_BINS))
  last_sig_bins = zeros(len(SIG_BINS))

  print "Calibration mode. Press CTRL-C to quit."
  print "Intensities for the {} signal frequencies if any exceeds {}:".format(len(SIG_BINS), SENSITIVITY)

  while True:
    while in_stream.get_read_available() < NUM_SAMPLES: sleep(0.01)
    try:
      audio_data = fromstring(in_stream.read(NUM_SAMPLES, exception_on_overflow=True), dtype=short)
      chunks_recorded += 1
    except IOError as err:
      print "ERROR while reading audio data: {}".format(err)
      continue

    amax, amin = max(audio_data), min(audio_data)
    if amin == -32768 or amax == 32767: print "WARNING: clipping detected"

    intensity = abs(fft(audio_data / 32768.0)[:NUM_SAMPLES/2])
    current_sig_bins = [intensity[SIG_BINS[i]] for i in sig_bin_indices]
    total_sig_bins = map(add, last_sig_bins, current_sig_bins)
    signals = [i for i in sig_bin_indices if total_sig_bins[i] > SENSITIVITY]
    last_sig_bins = current_sig_bins[:]
    if signals:
      out = ["{:.3f}".format(total_sig_bins[i]) for i in sig_bin_indices]
      print "  ".join(out)


if len(sys.argv) > 1 and sys.argv[1] == "-c":
  start_time = time()
  try:
    calibration()
  except KeyboardInterrupt:
    elapsed_time = time() - start_time
    print "{} chunks in {} seconds = {:.3f}/s".format(chunks_recorded, elapsed_time, chunks_recorded/elapsed_time)
    print "If this is significantly lower than {:.3f}, you're in trouble.".format(float(SAMPLING_RATE)/NUM_SAMPLES)
else:
  start_detecting()
