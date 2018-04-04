#!/usr/bin/env python
# Post-processing script that converts M106 commands into sequences of M300 beep commands that can
#   be detected by the beepdetect.py running on a Raspberry Pi.
# It can also handle scaling of the PWM values depending on Z coordinate, to compensate for the
#   effect of exhaust air 'bouncing' against the bed and causing more cooling than expected.
# Because M300 commands are timed pretty accurately (unlike M126 and M127 whose timing is sloppily
#   anticipated based on the number of commands and not their duration), the script will try to
#   shift them forward in time to compensate for the duration of the beep sequence and the time
#   needed to spin up the fan.
# This script assumes the fan commands are M106 (with an S argument) and M107. This will only be
#   the case if you configure your slicer to output G-code for RepRap or another firmware that
#   supports variable fan speed (I recommend to stick to RepRap because there are only minor
#   differences in G-code output between it and Sailfish).
# As with all my other post-processing scripts, extrusion coordinates must be relative (M83).
#
# Alexander Thomas a.k.a. DrLex, https://www.dr-lex.be/
# Released under Creative Commons Attribution 4.0 International license.

# TODO: implement splitting of long print moves to obtain accurate lead time.
# TODO: I would like to disable lead time for the very last command that turns off the fan right
#       before the end G-code. Spiky things are often being printed at that moment, and we want
#       to keep cooling them even while the extruder starts to retract.

import argparse
import itertools
import math
import re
import sys
from collections import deque

#### Defaults, either pass custom values as command-line parameters, or edit these. ####

# Z coordinate (mm) below which fan speeds will be linearly scaled with increasing Z.
# The correct value for this depends heavily on the design of your fan duct and extruder assembly.
RAMP_UP_ZMAX = 10.0

# The scale factor at Z=0. In other words, the linear scaling curve is a line between the points
# (0.0, RAMP_UP_SCALE0) and (RAMP_UP_ZMAX, 1.0) on a (Z, scale) graph.
RAMP_UP_SCALE0 = 0.1

# The number of seconds to shift the fan commands forwards, to compensate for the time needed to
# play and decode the sequence, and spin up the fan. This will only be approximate, because time
# granularity depends on duration of print moves, moreover this script does not consider
# acceleration when estimating the duration of moves.
LEAD_TIME = 1.0

# Multiplier between speed in mm/s and feedrate numbers for your printer. For the FFCP this should
# be 60, and be the same for both X, Y, Z, and even E.
FEED_FACTOR = 60.0

# Even though the Z axis has the same feed factor as the X and Y axes, its top speed is much
# lower. On the FFCP the maximum Z feedrate is about 1100.
FEED_LIMIT_Z = 1100.0

# The line indicating the end of the actual print commands. It is not strictly necessary to define
# this, but it will increase efficiency, ensure the fan is turned off without needing to
# explicitly do this in the end G-code, and avoid problems due to the script possibly processing
# things where it shouldn't.
END_MARKER = ";- - - Custom finish printing G-code for FlashForge Creator Pro - - -"

# The 4 frequencies of the signal beeps. These should match as closely as possible with SIG_BINS
# from beepdetect.py. The buzzer cannot play any frequency, it is rounded to a limited set that
# seems to follow the progression of semitones. I measured the following frequencies to be the
# nearest ones to SIG_BINS = [139, 150, 161, 172] the buzzer actually plays, however in practice
# bin 151 provides a stronger response than 150, maybe due to resonances of the buzzer.
# (If you have no clue what I'm talking about here, the bottom line is: don't touch these values.)
SIGNAL_FREQS = [5988, 6452, 6944, 7407]

# Length of the beep sequences. This must match SEQUENCE_LENGTH in beepdetect.py, again you should
# probably not touch this unless you want to use this script for something else.
SEQUENCE_LENGTH = 3

#### End of defaults section ####


VERSION = '0.1'
debug = False
last_sequence = []


class EndOfPrint(Exception):
    pass


class GCodeStreamer(object):
  """Class for reading a GCode file without having to shove it entirely in memory, by only keeping
  a buffer of the last read lines. When a new line is read and the buffer exceeds a certain size,
  the oldest line(s) will be popped from the buffer and sent to output."""

  def __init__(self, in_file, output, feed_factor, feed_limit_z, max_buffer=64):
    """@max_buffer is the largest number of lines that will be kept in memory before sending
    the oldest ones to output while reading new lines."""
    self.in_file = in_file
    self.output = output
    self.feed_factor = feed_factor
    self.feed_limit_z = feed_limit_z
    self.max_buffer = max_buffer

    self.buffer = deque()
    self.buffer_ahead = deque()
    # We're only interested in the duration of a small set of moves, but calculate the duration
    # of all moves anyway. Inefficient, but much saner than having to implement a backtracking
    # algorithm.
    self.buffer_times = deque()
    self.buffer_ahead_times = deque()
    # Start out with just any values. The fan should not be enabled anywhere near the first few
    # commands in the print body anyway, hence time estimates are irrelevant there.
    self.xyzf = [0.0, 0.0, 0.0, 1.0]
    self.end_of_print = False
    self.current_layer_z = None  # Not necessarily the same as xyzf[2]
    self.current_target_speed = None
    self.m126_7_found = False

  def start(self, replace_commands=None, replace_lines=None, replace_once=True):
    """Read the file and immediately output every line, until the end of the start G-code has
    been reached. We use the same '@body' marker as the GPX program to detect this line.
    If @replace_commands is a string or tuple of strings, any lines starting with them, will be
    replaced with @replace_lines (must be a list), or removed if it is None.
    If @replace_once, only the first match will be replaced, the rest will be removed."""
    replaced = False
    while True:
      line = self.in_file.readline()
      if not line:
        raise EOFError("Unexpected end of file while looking for end of start G-code")
      if replace_commands and line.startswith(replace_commands):
        if replace_lines and (not replace_once or not replaced):
          print >> self.output, "\n".join(replace_lines)
          replaced = True
      else:
        print >> self.output, line.rstrip("\r\n")
      if re.search(r";\s*@body(\s+|$)", line):
        break

  def stop(self):
    """Output the rest of the buffers, and the rest of the file."""
    for line in self.buffer:
      print >> self.output, line
    self.buffer.clear()
    self.buffer_times.clear()
    for line in self.buffer_ahead:
      print >> self.output, line
    self.buffer_ahead.clear()
    self.buffer_ahead_times.clear()
    while True:
      line = self.in_file.readline()
      if not line:
        return
      print >> self.output, line.rstrip("\r\n")

  def _update_print_state(self, line):
    """Update the state of print position and feedrate, and return an estimate of how long
    this move takes. The estimate does not consider acceleration."""
    # I could try to capture this in one big awful regex, but doing it separately avoids being
    # locked into the output format of a specific slicer. I even allow "Z1.2 F321 X0.0 G1".
    found_x = re.match(r"[^;]*X(-?\d*\.?\d+)(\s|;|$)", line)
    found_y = re.match(r"[^;]*Y(-?\d*\.?\d+)(\s|;|$)", line)
    found_z = re.match(r"[^;]*Z(\d*\.?\d+)(\s|;|$)", line)
    found_f = re.match(r"[^;]*F(\d*\.?\d+)(\s|;|$)", line)

    xyzf2 = list(self.xyzf)  # copy values, not reference
    if found_z:
      xyzf2[2] = float(found_z.group(1))
      if found_x or found_y:
        # Only vase mode print moves should combine X or Y move with Z change.
        # TODO: strictly spoken we should read the layer height from the file's parameter section
        # and use that as the threshold.
        if xyzf2[2] >= self.current_layer_z + 0.2:
          self.current_layer_z = xyzf2[2]
      else:
        self.current_layer_z = float(xyzf2[2])

    if found_x: xyzf2[0] = float(found_x.group(1))
    if found_y: xyzf2[1] = float(found_y.group(1))
    if found_f: xyzf2[3] = float(found_f.group(1))

    time_estimate = 0.0
    # Assumption to simplify logic and calculations: Z component in a combined XYZ move has a
    # negligible time contribution compared to XY.
    if found_x or found_y:
      time_estimate = (math.hypot(xyzf2[0] - self.xyzf[0], xyzf2[1] - self.xyzf[1]) *
                       self.feed_factor / xyzf2[3])
    elif found_z:
      feedrate = min(xyzf2[3], self.feed_limit_z)
      time_estimate = abs(xyzf2[2] - self.xyzf[2]) * self.feed_factor / feedrate
    else:
      found_e = re.match(r"[^;]*E(-?\d*\.?\d+)(\s|;|$)", line)
      if found_e:  # retract move, luckily they're relative: no need to remember state
        time_estimate = abs(float(found_e.group(1))) * self.feed_factor / xyzf2[3]

    self.xyzf = xyzf2
    return time_estimate

  def _read_next_line(self, ahead=False):
    """Read one line from the file and return it.
    Internal state (layer height, vase mode print mode, fan speed, ...) will be updated
    according to whatever interesting things happened inside the line.
    If @ahead, the lines will be stored in buffer_ahead instead of the main buffer.
    If end of file is reached, raise EOFError. If END_MARKER is reached, raise EndOfPrint."""
    while True:
      line = self.in_file.readline()
      if not line:
        raise EOFError("End of file reached")
      line = line.rstrip("\r\n")
      if ahead:
        self.buffer_ahead.append(line)
      else:
        self.buffer.append(line)
        while len(self.buffer) > self.max_buffer:
          print >> self.output, self.buffer.popleft()
          self.buffer_times.popleft()

      if line.startswith(END_MARKER):
        self.end_of_print = True
        raise EndOfPrint("End of print code reached")

      if re.match(r"[^;]*G1(\s|;|$)", line):  # print or travel move
        time_estimate = self._update_print_state(line)
        if ahead:
          self.buffer_ahead_times.append(time_estimate)
        else:
          self.buffer_times.append(time_estimate)
        return line

      if ahead:
        self.buffer_ahead_times.append(0.0)
      else:
        self.buffer_times.append(0.0)
      # TODO: a G4 command can be directly converted to time (but should be rare in actual prints)
      # TODO: tool changes (should be assumed to take ridiculously long)

      # M107 is actually deprecated according to the RepRap wiki, but Slic3r still uses it.
      # Assumption: the S argument comes first (in Slic3r there is nothing except S anyway).
      fan_command = re.match(r"(M106|M107)(\s+S(\d*\.?\d+)|\s|;|$)", line)
      if fan_command:
        speed = 0.0
        # An M106 without S argument will be treated as M106 S0 or M107.
        if fan_command.group(1) == "M106" and fan_command.group(3):
          speed = float(fan_command.group(3))
        self.current_target_speed = speed
        return line

      if re.match(r"(M126|M127)(\s|;|$)", line):
        self.m126_7_found = True
      return line

  def _get_next_ahead(self):
    """Move the next line from buffer_ahead to the regular buffer, and return it."""
    line = self.buffer_ahead.popleft()
    self.buffer.append(line)
    self.buffer_times.append(self.buffer_ahead_times.popleft())
    return line

  def get_next_event(self, look_ahead=0):
    """Read lines from the file until something interesting is encountered. This can either be:
      - an M106 or M107 command
      - a layer change (in case of vase mode prints, treat Z increase of 0.2 as a layer change)
    Return value is an array with the interesting line and @look_ahead lines after it.
    NOTE: the state of this GCodeStreamer will be updated according to all read lines, hence the
      state will represent what was seen in the look_ahead lines. This helps with slicer quirks
      like placing fan commands right before layer changes, or placing two fan commands immedia-
      tely after each other. It also avoids confusing a Z-hop travel move with a layer change.
    If end of file is reached, raise EOFError. If END_MARKER is reached, raise EndOfPrint."""
    if self.end_of_print:
      raise EndOfPrint("End of print code reached")

    lines = []
    last_z = self.current_layer_z
    while True:
      line = self._get_next_ahead() if self.buffer_ahead else self._read_next_line()
      apparent_layer_change = (self.current_layer_z != last_z)
      if line.startswith(("M106", "M107")) or apparent_layer_change:
        # Something interesting happened!
        lines.append(line)
        try:
          # Top up buffer_ahead if necessary
          for _ in xrange(look_ahead - len(self.buffer_ahead)):
            lines.append(self._read_next_line(True))
        except (EOFError, EndOfPrint):
          pass
        # Avoid treating Z-hop as an event: check whether Z was not reverted during look_ahead
        if look_ahead and apparent_layer_change and self.current_layer_z == last_z:
          continue
        return lines

  def pop(self, offset=0):
    """Removes the last line from the buffer (i.e. the first one returned by the last
    invocation of get_next_event), and returns it."""
    self.buffer_times.pop()
    return self.buffer.pop()

  def append_buffer(self, lines):
    """Append the @lines at the end of the main buffer."""
    self.buffer.extend(lines)
    self.buffer_times.extend([0.0 for _ in range(len(lines))])

  def insert_buffer(self, pos, lines):
    """Insert extra @lines before the existing line at index @pos."""
    # This would probably be a one-liner (per buffer) in python 3.6
    new_buffer = deque()
    new_buffer_times = deque()
    for _ in range(pos):
      new_buffer.append(self.buffer.popleft())
      new_buffer_times.append(self.buffer_times.popleft())
    new_buffer.extend(lines)
    new_buffer_times.extend([0.0 for _ in range(len(lines))])
    new_buffer.extend(self.buffer)
    new_buffer_times.extend(self.buffer_times)
    self.buffer = new_buffer
    self.buffer_times = new_buffer_times

  def drop_ahead_commands(self, commands):
    """Remove lines starting with any of the given command(s) in the lookahead buffer.
    @commands may be a string or a tuple of strings."""
    # It is simpler to just copy the non-deleted lines to a new deque, and if more than one
    # line is to be deleted, it is more efficient as well.
    cleaned = deque()
    cleaned_times = deque()
    for line, t in itertools.izip(self.buffer_ahead, self.buffer_ahead_times):
      if not line.startswith(commands):
        cleaned.append(line)
        cleaned_times.append(t)
    self.buffer_ahead = cleaned
    self.buffer_ahead_times = cleaned_times


def usage():
  print """Usage: $0 [-hd] inputFile
  -d: debug mode (extra spam on stderr)"
  -h: usage information"""

def print_debug(message):
  if debug:
    print >> sys.stderr, message

def print_error(message):
  print >> sys.stderr, "ERROR: {}".format(message)

def print_warning(message):
  print >> sys.stderr, "WARNING: {}".format(message)

def speed_quantized(speed):
  """Convert a speed in the 0-255 range to a quantized value that can be represented by a beep
  sequence."""
  return int(round(float(speed) / 255 * (4**SEQUENCE_LENGTH - 1)))

def speed_to_beep_sequence(speed):
  """Return a list with the indices of the beep frequencies that represent the given speed."""
  value = speed_quantized(speed)
  sequence = deque()
  while value:
    quad = value % 4
    sequence.appendleft(quad)
    value = (value - quad) / 4
  while len(sequence) < SEQUENCE_LENGTH:
    sequence.appendleft(0)
  return list(sequence)

def speed_to_M300_commands(speed, scale=1.0, max_speed=255.0, skip_repeat=True):
  """Return a list with the commands to play a sequence that can be detected by beepdetect.py.
  @speed is a float value between 0.0 and 255.0.
  @scale will be applied to @speed before generating the sequence.
  Speed will be clipped to @max_speed.i
  If @skip_repeat, return empty list if the sequence is the same as in previous invocation."""
  global last_sequence

  s_speed = speed * scale
  clipped = ""
  if s_speed > max_speed:
    s_speed = max_speed
    clipped = ", clipped"
  sequence = speed_to_beep_sequence(s_speed)
  if sequence == last_sequence and skip_repeat:
    return []
  last_sequence = sequence

  if s_speed:
    scaled = " scaled {:.3f}".format(scale) if scale < 1.0 else ""
    comment = "fan PWM {}{}{} = {:.2f}%".format(speed, scaled, clipped, s_speed / 2.55)
  else:
    comment = "fan off"
  commands = ["M300 S0 P200; {} -> sequence {}".format(
              comment, "".join([str(i) for i in sequence]))]
  for i in xrange(len(sequence)):
    commands.append("M300 S{} P20".format(SIGNAL_FREQS[sequence[i]]))
    if i < len(sequence) - 1:
      commands.append("M300 S0 P100")
  commands.append("M300 S0 P200; end sequence")
  return commands

def ramp_up_scale(z):
  return min(1.0, z * (1.0 - RAMP_UP_SCALE0) / RAMP_UP_ZMAX + RAMP_UP_SCALE0)

def inject_beep_sequence(gcode, scale, lead_time=0.0):
  """Insert the beep sequence that matches the most recent fan speed seen in gcode, scaled
  by the given factor, back into the gcode. The position of the sequence will be chosen
  such that it leads the original moment of the fan speed command by an approximate
  @lead_time seconds."""
  commands = speed_to_M300_commands(gcode.current_target_speed, scale)
  if not commands:
    return False

  t_elapsed = 0.0
  t_next = 0.0  # actually previous, since we're going backwards...
  if debug:
    assert(len(gcode.buffer) == len(gcode.buffer_times))
  position = len(gcode.buffer_times)
  previous_sequence = False
  for t in reversed(gcode.buffer_times):
    position -= 1
    t_next = t_elapsed
    t_elapsed += t
    if t_elapsed >= lead_time:
      break
    elif gcode.buffer[position] == "M300 S0 P200; end sequence":
      # Ensure not to jump across previously inserted sequence: swapping commands would be bad!
      previous_sequence = True
      break
  if previous_sequence:
    print_debug("  Cannot backtrack more than {:.3f}s due to previous sequence".format(t_next))
    position += 1
  elif position == 0:
    print_debug(
      "Buffer too short to backtrack to lead time of {:.3f}s, it will only be {:.3f}s".format(
      lead_time, t_elapsed))
  else:
    print_debug("  Backtrack: {:.3f} ~ {:.3f}; position {}".format(t_next, t_elapsed, position))
    # Try to pick a reasonable position between existing print moves
    if t_elapsed > 1.33 * lead_time:
      if t_next >= 0.75 * lead_time:
        position += 1
        print_debug("    Picked {:.3f}: OK :)".format(t_next))
      elif t_elapsed <= 2.0 * lead_time:
        print_debug("    Picked {:.3f}: meh :/".format(t_elapsed))
      else:
        # TODO: we could split up the print move to get a good lead time! This must be optional
        # though, because it could cause a hiccup that may be visible in the print. This should
        # also only really be done when speeding up the fan, because it is more crucial to have
        # enough cooling in time.
        position += 1
        print_debug("    Picked {:.3f}: too short :(".format(t_next))
    else:
      print_debug("    Picked {:.3f}: good :D".format(t_elapsed))

  if position < len(gcode.buffer) - 1:
    gcode.insert_buffer(position, commands)
  else:
    gcode.append_buffer(commands)
  return True


parser = argparse.ArgumentParser(
  description='Post-processing script to convert M106 fan speed commands into beep sequences that can be detected by beepdetect.py, to obtain variable fan speed on 3D printers that lack a PWM fan output.',
  formatter_class=argparse.ArgumentDefaultsHelpFormatter,
  argument_default=argparse.SUPPRESS)
# SUPPRESS hides useless defaults in help text, the downside is needing to use hasattr().
parser.add_argument('in_file', type=argparse.FileType('r'),
                    help='file to process')
parser.add_argument('-d', '--debug', action='store_true',
                    help='enable debug output on stderr')
parser.add_argument('-z', '--zmax', type=float,
                    help='Z coordinate below which fan speed will be linearly ramped up',
                    default=RAMP_UP_ZMAX)
parser.add_argument('-s', '--scale0', type=float,
                    help='Scale factor for linear fan ramp-up curve at Z = 0',
                    default=RAMP_UP_SCALE0)
parser.add_argument('-t', '--lead_time', type=float,
                    help='Number of seconds (approximately) to advance beep commands',
                    default=LEAD_TIME)
parser.add_argument('-f', '--feed_factor', type=float,
                    help='Factor between speed in mm/s and feedrate',
                    default=FEED_FACTOR)
parser.add_argument('-l', '--feed_limit_z', type=float,
                    help='Maximum feedrate for the Z axis',
                    default=FEED_LIMIT_Z)
parser.add_argument('-o', '--out_file', type=argparse.FileType('w'),
                    help='optional file to write to (default is to print to standard output)')

args = parser.parse_args()

if hasattr(args, 'debug'):
  debug = True

print_debug("Debug output enabled, prepare to be spammed")

output = args.out_file if hasattr(args, 'out_file') else sys.stdout
gcode = GCodeStreamer(args.in_file, output, args.feed_factor, args.feed_limit_z)
try:
  # Assumption: anything before the end of the start G-code will only contain 'fan off'
  # instructions, either using M107, or M106 S0.
  gcode.start(("M106", "M107"), speed_to_M300_commands(0.0))
except EOFError as err:
  print_error(err)
  sys.exit(1)

print_debug("=== End of start G-code reached, now beginning actual processing ===")

last_z = 0
layers_with_fan = 0
current_fan_speed = None  # Actual scaled and clipped speed
while True:
  try:
    # look_ahead must be at least:
    #   2 to ignore Z-hop travel moves,
    #   1 to ignore duplicate M106 commands,
    #   2 to already be aware of changed Z in case of fan command followed by layer change,
    #   3 to combine the previous two cases.
    # This is again Slic3r-specific, it inserts M106 before changing the layer.
    lines = gcode.get_next_event(3)
  except EOFError:
    print_error("Unexpected end of file reached!")
    sys.exit(1)
  except EndOfPrint:
    if current_fan_speed:
      gcode.append_buffer(speed_to_M300_commands(0.0))
    break
  print_debug("Interesting line: {}".format(lines[0]))

  what = None
  if lines[0].startswith(("M106", "M107")):
    gcode.pop()  # Get rid of this invalid Sailfish command
    what = "Fan command"
  elif gcode.current_target_speed is not None:
    # Layer change: check if we need to update fan speed
    what = "Layer change {}".format(gcode.current_layer_z)

  if what:
    # If there are multiple fan commands very close to each other, it is pointless to execute them
    # all. The last one seen in the lookahead buffer determines the speed, the rest is dropped.
    gcode.drop_ahead_commands(("M106", "M107"))
    scale = ramp_up_scale(gcode.current_layer_z)
    new_fan_speed = gcode.current_target_speed * scale
    if new_fan_speed != current_fan_speed:
      scaled = " scaled by {:.2f} = {:.2f}".format(scale, new_fan_speed) if scale < 1.0 else ""
      print_debug("  {} -> set fan speed to {:.2f}{}".format(
                  what, gcode.current_target_speed, scaled))
      if not inject_beep_sequence(gcode, scale, args.lead_time):
        print_debug("    but sequence is same as before, hence skip")
      current_fan_speed = new_fan_speed
    else:
      print_debug("  {} -> already at required speed {}".format(
                  what, current_fan_speed))

gcode.stop()

if gcode.m126_7_found:
  print_warning("M126 and/or M127 command(s) were found inside the body of the G-code. Most likely, your fan will not work for this print. Are you sure your slicer is outputting G-code with M106 commands (e.g. RepRap G-code flavor)?")
