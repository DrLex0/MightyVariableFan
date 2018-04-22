#!/usr/bin/env python3
"""
Post-processing script that converts M106 commands into sequences of M300 beep commands that can
  be detected by the beepdetect.py running on a Raspberry Pi.
It can also handle scaling of the PWM values depending on Z coordinate, to compensate for the
  effect of exhaust air 'bouncing' against the bed and causing more cooling than expected.
Because M300 commands are timed pretty accurately (unlike M126 and M127 whose timing is sloppily
  anticipated based on the number of commands and not their duration), the script will try to
  shift them forward in time to compensate for the duration of the beep sequence and the time
  needed to spin up the fan.
This script assumes the fan commands are M106 (with an S argument) and M107. This will only be
  the case if you configure your slicer to output G-code for RepRap or another firmware that
  supports variable fan speed (I recommend to stick to RepRap because there are only minor
  differences in G-code output between it and Sailfish).
As with all my other post-processing scripts, extrusion coordinates must be relative (M83).

Alexander Thomas a.k.a. DrLex, https://www.dr-lex.be/
Released under Creative Commons Attribution 4.0 International license.
"""

# TODO: prevent look_ahead from skipping fan speed changes for single-line bridge moves!
# TODO: if there are too many successive fan speed commands in too short time, then some should be
#   skipped in a smart manner. For instance, trying to reduce speed during only a second or less,
#   is pointless due to inertia of the fan. (The last command in a 'burst' must always remain
#   intact.)

import argparse
import math
import re
import sys
from collections import deque

#### Defaults, either pass custom values as command-line parameters, or edit these. ####

# Z coordinate (mm) below which fan speeds will be linearly scaled with increasing Z.
# The correct value for this depends heavily on the design of your fan duct and extruder assembly.
RAMP_UP_ZMAX = 5.0

# The scale factor at Z=0. In other words, the linear scaling curve is a line between the points
# (0.0, RAMP_UP_SCALE0) and (RAMP_UP_ZMAX, 1.0) on a (Z, scale) graph.
RAMP_UP_SCALE0 = 0.05

# The number of seconds to shift fan commands forward in time, to compensate for time needed to
# play and decode the sequence, and spin up the fan. This will only be approximate, because time
# granularity depends on duration of print moves, moreover this script does not consider
# acceleration when estimating the duration of moves. Acceleration will make actual time slightly
# longer than what you configure here.
LEAD_TIME = 1.3

# Multiplier between speed in mm/s and feedrate numbers for your printer. For the FFCP this should
# be 60, and be the same for both X, Y, Z, and even E.
FEED_FACTOR = 60.0

# Even though the Z axis has the same feed factor as the X and Y axes, its top speed is much
# lower. On the FFCP the maximum Z feedrate should be 1170.
FEED_LIMIT_Z = 1170.0

#### End of defaults section ####

#### Configuration section for fixed values ####

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

#### End of configuration section ####


VERSION = '0.1'
debug = False


class EndOfPrint(Exception):
    pass


class GCodeStreamer(object):
    """Class for reading a GCode file without having to shove it entirely in memory, by only
    keeping a buffer of the last read lines. When a new line is read and the buffer exceeds a
    certain size, the oldest line(s) will be popped from the buffer and sent to output."""

    def __init__(self, config, output, max_buffer=96):
        """@max_buffer is the largest number of lines that will be kept in memory before
        sending the oldest ones to output while reading new lines."""
        self.in_file = config.in_file
        self.feed_factor = config.feed_factor
        self.feed_limit_z = config.feed_limit_z
        self.timings = hasattr(config, 'timings')

        self.output = output
        self.max_buffer = max_buffer

        self.buffer = deque()
        self.buffer_ahead = deque()
        # We're only interested in the duration of a small set of moves, but calculate the
        # duration of all moves anyway. Inefficient, but much saner than having to implement
        # a backtracking algorithm.
        self.buffer_times = deque()
        self.buffer_ahead_times = deque()
        # Start out with just any values. The fan should not be enabled anywhere near the first
        # few commands in the print body anyway, hence time estimates are irrelevant there.
        self.xyzf = [0.0, 0.0, 0.0, 1.0]
        self.end_of_print = False
        self.current_layer_z = None  # Not necessarily the same as xyzf[2]
        self.current_target_speed = None  # Speed as seen in self.buffer
        self.ahead_target_speed = None  # Speed as seen in buffer_ahead
        self.last_sequence = []
        self.m126_7_found = False

    def start(self, replace_commands=None, replace_lines=None, replace_once=True):
        """Read the file and immediately output every line, until the end of the start G-code
        has been reached. We use the same '@body' marker as the GPX program to detect this line.
        If @replace_commands is a string or tuple of strings, any lines starting with them,
        will be replaced with @replace_lines (must be a list), or removed if it is None.
        If @replace_once, only the first match will be replaced, the rest will be removed.
        Return value is the number of lines replaced or removed."""
        replaced = 0
        while True:
            line = self.in_file.readline()
            if not line:
                raise EOFError("Unexpected end of file while looking for end of start G-code")
            if replace_commands and line.startswith(replace_commands):
                if replace_lines and (not replace_once or not replaced):
                    print("\n".join(replace_lines), file=self.output)
                replaced += 1
            else:
                print(line.rstrip("\r\n"), file=self.output)
            if re.search(r";\s*@body(\s+|$)", line):
                break
        return replaced

    def stop(self):
        """Output the rest of the buffers, and the rest of the file."""
        if self.timings:
            for line, tval in zip(self.buffer, self.buffer_times):
                print(("{}; {:.3f}".format(line, tval) if tval else line), file=self.output)
            for line, tval in zip(self.buffer_ahead, self.buffer_ahead_times):
                print(("{}; {:.3f}".format(line, tval) if tval else line), file=self.output)
        else:
            for line in self.buffer:
                print(line, file=self.output)
            for line in self.buffer_ahead:
                print(line, file=self.output)
        self.buffer.clear()
        self.buffer_times.clear()
        self.buffer_ahead.clear()
        self.buffer_ahead_times.clear()
        while True:
            line = self.in_file.readline()
            if not line:
                return
            print(line.rstrip("\r\n"), file=self.output)

    def _update_print_state(self, line):
        """Update the state of print position and feedrate, and return an estimate of how
        long this move takes. The estimate does not consider acceleration."""
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
                # TODO: strictly spoken we should read the layer height from the file's parameter
                # section and use that as the threshold.
                if xyzf2[2] >= self.current_layer_z + 0.2:
                    self.current_layer_z = xyzf2[2]
            else:
                self.current_layer_z = float(xyzf2[2])

        if found_x:
            xyzf2[0] = float(found_x.group(1))
        if found_y:
            xyzf2[1] = float(found_y.group(1))
        if found_f:
            xyzf2[3] = float(found_f.group(1))

        time_estimate = 0.0
        # Assumption to simplify logic and calculations: Z component in a combined XYZ move has
        # a negligible time contribution compared to XY.
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
        line = self.in_file.readline()
        if not line:
            raise EOFError("End of file reached")
        line = line.rstrip("\r\n")
        if ahead:
            self.buffer_ahead.append(line)
        else:
            self.buffer.append(line)
            if self.timings:
                while len(self.buffer) > self.max_buffer:
                    old_l = self.buffer.popleft()
                    old_t = self.buffer_times.popleft()
                    print(("{}; {:.3f}".format(old_l, old_t) if old_t else old_l),
                          file=self.output)
            else:
                while len(self.buffer) > self.max_buffer:
                    print(self.buffer.popleft(), file=self.output)
                    self.buffer_times.popleft()

        times = self.buffer_ahead_times if ahead else self.buffer_times
        if line.startswith(END_MARKER):
            self.end_of_print = True
            times.append(0.0)
            raise EndOfPrint("End of print code reached")

        if re.match(r"[^;]*G1(\s|;|$)", line):  # print or travel move
            times.append(self._update_print_state(line))
            return line

        times.append(0.0)
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
            self.ahead_target_speed = speed
            if not ahead:
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
        if line.startswith(("M106", "M107")):
            self.current_target_speed = self.ahead_target_speed
        return line

    def get_next_event(self, look_ahead=0):
        """Read lines from the file until something interesting is encountered. This can be:
        - an M106 or M107 command
        - a layer change (in case of vase mode prints, treat Z increase of 0.2 as layer change).
        Return value is the interesting line.
        NOTE: the state of this GCodeStreamer will be updated according to all read lines, hence
          the state will include what was seen in the @look_ahead lines. This helps with slicer
          quirks like placing fan commands right before layer changes, or placing two fan
          commands immediately after each other. It also avoids confusing a Z-hop travel move
          with a layer change.
        If end of file is reached, raise EOFError. If END_MARKER is reached, raise EndOfPrint."""
        if self.end_of_print:
            raise EndOfPrint("End of print code reached")

        last_z = self.current_layer_z
        while True:
            line = self._get_next_ahead() if self.buffer_ahead else self._read_next_line()
            apparent_layer_change = (self.current_layer_z != last_z)
            if line.startswith(("M106", "M107")) or apparent_layer_change:
                # Something interesting happened!
                try:
                    # Top up buffer_ahead if necessary
                    for _ in range(look_ahead - len(self.buffer_ahead)):
                        self._read_next_line(True)
                except (EOFError, EndOfPrint):
                    pass
                # Avoid treating Z-hop as event: check whether Z wasn't reverted during look_ahead
                if look_ahead and apparent_layer_change and self.current_layer_z == last_z:
                    continue
                return line

    def pop(self):
        """Removes the last line from the buffer (i.e. the first one returned by the last
        invocation of get_next_event), and returns it."""
        self.buffer_times.pop()
        return self.buffer.pop()

    def append_buffer(self, lines):
        """Append the @lines at the end of the main buffer."""
        self.buffer.extend(lines)
        self.buffer_times.extend([0.0 for _ in range(len(lines))])

    def insert_buffer(self, pos, lines, replace=False):
        """Insert extra @lines before, or replace the existing line at index @pos."""
        if len(lines) == 1:
            if replace:
                self.buffer[pos] = lines[0]
                self.buffer_times[pos] = 0.0
            else:
                self.buffer.insert(pos, lines[0])
                self.buffer_times.insert(pos, 0.0)
            return

        new_buffer = deque()
        new_buffer_times = deque()
        for _ in range(pos):
            new_buffer.append(self.buffer.popleft())
            new_buffer_times.append(self.buffer_times.popleft())
        if replace:
            self.buffer.popleft()
            self.buffer_times.popleft()
        new_buffer.extend(lines)
        new_buffer_times.extend([0.0 for _ in range(len(lines))])
        new_buffer.extend(self.buffer)
        new_buffer_times.extend(self.buffer_times)
        self.buffer = new_buffer
        self.buffer_times = new_buffer_times

    @staticmethod
    def parse_xy(line):
        """Return X, Y components of a G1 command as a tuple. Absent components will be None."""
        found_x = re.match(r"[^;]*X(-?\d*\.?\d+)(\s|;|$)", line)
        found_y = re.match(r"[^;]*Y(-?\d*\.?\d+)(\s|;|$)", line)
        x, y = None, None
        if found_x:
            x = float(found_x.group(1))
        if found_y:
            y = float(found_y.group(1))
        return x, y

    @staticmethod
    def parse_xyzefc(line):
        """Return X, Y, Z, E, F components and comment string of a command line as an array.
        Absent components will be None, or empty string for the comment."""
        found_x = re.match(r"[^;]*X(-?\d*\.?\d+)(\s|;|$)", line)
        found_y = re.match(r"[^;]*Y(-?\d*\.?\d+)(\s|;|$)", line)
        found_z = re.match(r"[^;]*Z(\d*\.?\d+)(\s|;|$)", line)
        found_e = re.match(r"[^;]*E(-?\d*\.?\d+)(\s|;|$)", line)
        found_f = re.match(r"[^;]*F(\d*\.?\d+)(\s|;|$)", line)
        result = [None, None, None, None, None, line.partition(";")[2]]
        if found_x:
            result[0] = float(found_x.group(1))
        if found_y:
            result[1] = float(found_y.group(1))
        if found_z:
            result[2] = float(found_z.group(1))
        if found_e:
            result[3] = float(found_e.group(1))
        if found_f:
            result[4] = float(found_f.group(1))
        return result

    def find_previous_xy(self, position):
        """Backtrack in the buffer before @position, and return the previous X and Y coordinates
        as a list, or None if X or Y could not be found. This is an inefficient operation and
        should only be used when strictly necessary."""
        found_x, found_y = None, None
        for i in reversed(list(range(position))):
            x, y = GCodeStreamer.parse_xy(self.buffer[i])
            if x is not None:
                found_x = x
                if found_y is not None:
                    break
            if y is not None:
                found_y = y
                if found_x is not None:
                    break
        if found_x is None or found_y is None:
            return None
        return [found_x, found_y]

    def split_move(self, position, time2):
        """Try to split up the move at @position such that the second part takes approximately
        @time2 seconds.
        The return value is a boolean indicating whether the move could be split. Splitting
        fails if the starting coordinates for this move could not be found before @position."""
        # Inefficient but acceptable because it should only be done a few times. The alternative
        # would be to keep track of previous X, Y for every line in the buffer, which would make
        # the script much slower overall.
        start_xy = self.find_previous_xy(position)
        if not start_xy:
            return False

        fraction = 1.0 - (time2 / self.buffer_times[position])
        if debug:
            assert fraction > 0
        time1 = fraction * self.buffer_times[position]

        end_xyzefc = GCodeStreamer.parse_xyzefc(self.buffer[position])
        if end_xyzefc[0] is None and end_xyzefc[1] is None:
            return False
        if end_xyzefc[0] is None:
            end_xyzefc[0] = start_xy[0]
        elif end_xyzefc[1] is None:
            end_xyzefc[1] = start_xy[1]

        # Strictly spoken Z should also be split for vase mode moves, but a print would need to be
        # pretty pathological to have a move so long that the Z increase would need to be split to
        # avoid a visible artefact. Given that we had to split the move, I assume most of the Z
        # increase will be in the first part.
        move_x, move_y = end_xyzefc[0] - start_xy[0], end_xyzefc[1] - start_xy[1]
        mid_x, mid_y = start_xy[0] + fraction * move_x, start_xy[1] + fraction * move_y
        if end_xyzefc[3]:
            mid_e = " E{:.5f}".format(fraction * end_xyzefc[3])
            end_e = " E{:.5f}".format((1.0 - fraction) * end_xyzefc[3])
        else:
            mid_e, end_e = "", ""
        zed = "" if end_xyzefc[2] is None else " Z{}".format(end_xyzefc[2])  # Zed's dead, baby.
        feed = "" if end_xyzefc[4] is None else " F{}".format(end_xyzefc[4])
        comment = " ;{}".format(end_xyzefc[5]) if end_xyzefc[5] else ""
        new_lines = ["G1{} X{:.3f} Y{:.3f}{}{}{}".format(zed, mid_x, mid_y, mid_e, feed, comment),
                     "G1 X{:.3f} Y{:.3f}{} ; split move for {:.2f}s extra lead time".format(
                         end_xyzefc[0], end_xyzefc[1], end_e, time2)]
        self.insert_buffer(position, new_lines, True)
        self.buffer_times[position] = time1
        self.buffer_times[position + 1] = time2
        return True

    def ahead_time_until(self, commands):
        """Return the total time estimate for commands in the ahead buffer until any of the
        given command(s) is encountered. If none is encountered, return -1.0."""
        time_total = 0.0
        found = False
        for line, tval in zip(self.buffer_ahead, self.buffer_ahead_times):
            if line.startswith(commands):
                found = True
                break
            time_total += tval
        return time_total if found else -1.0

    def drop_ahead_commands(self, commands):
        """Remove lines starting with any of the given command(s) in the lookahead buffer.
        @commands may be a string or a tuple of strings."""
        # It is simpler to just copy the non-deleted lines to a new deque, and if more than one
        # line is to be deleted, it is more efficient as well.
        cleaned = deque()
        cleaned_times = deque()
        for line, tval in zip(self.buffer_ahead, self.buffer_ahead_times):
            if not line.startswith(commands):
                cleaned.append(line)
                cleaned_times.append(tval)
        self.buffer_ahead = cleaned
        self.buffer_ahead_times = cleaned_times

    @staticmethod
    def speed_to_beep_sequence(speed):
        """Return a list with the indices of the beep frequencies that represent
        the given speed."""
        quantized = int(round(speed / 255.0 * (4**SEQUENCE_LENGTH - 1)))
        sequence = deque()
        while quantized:
            quad = quantized % 4
            sequence.appendleft(quad)
            quantized = (quantized - quad) // 4
        while len(sequence) < SEQUENCE_LENGTH:
            sequence.appendleft(0)
        return list(sequence)

    def speed_to_m300_commands(self, speed, comment="", skip_repeat=True):
        """Return a list with commands to play a sequence that can be detected by beepdetect.py.
        @speed is a float value between 0.0 and 255.0.
        @comment will be inserted with the commands.
        If @skip_repeat, return an empty list if the sequence is the same as in the previous
        invocation with @skip_repeat."""
        sequence = GCodeStreamer.speed_to_beep_sequence(speed)
        if skip_repeat:
            if sequence == self.last_sequence:
                return []
            self.last_sequence = sequence

        commands = ["M300 S0 P200; {} -> sequence {}".format(
            comment, "".join([str(i) for i in sequence]))]
        for i, freq_index in enumerate(sequence):
            commands.append("M300 S{} P20".format(SIGNAL_FREQS[freq_index]))
            if i < len(sequence) - 1:
                commands.append("M300 S0 P100")
        return commands + ["M300 S0 P200; end sequence"]

    def optimize_lead_time(self, lead_time, position, t_elapsed, t_next, allow_split):
        """Try to pick the position between existing print moves to approximate lead_time as
        well as possible, given that @t_elapsed >= lead_time and @t_next < lead_time.
        If @allow_split, a move at @position may be split up to improve lead time."""
        print_debug("    Backtrack: {:.3f} ~ {:.3f}; position {}".format(
            t_next, t_elapsed, position))
        # TODO: could discern between speeding up and slowing down. For slow-down it is
        #   more acceptable to be too late than for speed-up.
        if t_elapsed > 1.25 * lead_time:
            found = False
            if t_next >= 0.75 * lead_time:
                position += 1
                print_debug("    Picked {:.3f}: OK :)".format(t_next))
                found = True
            elif allow_split:
                # Split the move such that the second part gives us the last bit of time
                # needed to reach lead_time
                found = self.split_move(position, lead_time - t_next)
                if found:
                    position += 1
                    print_debug(
                        "    Split move because neither {:.3f} nor {:.3f} is acceptable :P".format(
                            t_elapsed, t_next))
                else:
                    print_debug("    Cannot split the move! :\\")
            if not found:
                if t_elapsed <= 2.0 * lead_time:
                    print_debug("    Picked {:.3f}: meh :/".format(t_elapsed))
                else:
                    position += 1
                    print_debug("    Picked {:.3f}: too late :(".format(t_next))
        else:
            print_debug("    Picked {:.3f}: good :D".format(t_elapsed))
        return position

    def inject_beep_sequence(self, speed, comment="", lead_time=0.0, allow_split=False):
        """Insert the beep sequence representing @speed into the gcode, with @comment added.
        The position of the sequence will be chosen such that it leads the last line in the
        buffer by an approximate @lead_time seconds.
        If @allow_split, long moves may be split to obtain a more accurate lead_time."""
        commands = self.speed_to_m300_commands(speed, comment)
        if not commands:
            return False

        if not lead_time:
            self.append_buffer(commands)
            return True

        t_elapsed = 0.0
        # next in the file but previous in the algorithm, since we're going backwards...
        t_next = 0.0
        if debug:
            assert len(self.buffer) == len(self.buffer_times)
        position = len(self.buffer_times)
        previous_sequence = False
        for t in reversed(self.buffer_times):
            position -= 1
            t_next = t_elapsed
            t_elapsed += t
            if t_elapsed >= lead_time:
                break
            elif self.buffer[position] == "M300 S0 P200; end sequence":
                # Ensure not to jump across previously inserted sequence: swapping commands
                # would be bad!
                previous_sequence = True
                break
        if previous_sequence:
            print_debug(
                "  Cannot backtrack more than {:.3f}s due to previous sequence".format(t_next))
            position += 1
        elif position == 0:
            print_debug(
                "Buffer too short to backtrack to lead time of {:.3f}s, it will only be {:.3f}s".format(
                    lead_time, t_elapsed))
        else:
            position = self.optimize_lead_time(lead_time, position, t_elapsed, t_next, allow_split)

        if position < len(self.buffer):
            self.insert_buffer(position, commands)
        else:
            self.append_buffer(commands)
        return True


def print_debug(message):
    if debug:
        print(message, file=sys.stderr)

def print_error(message):
    print("ERROR: {}".format(message), file=sys.stderr)

def print_warning(message):
    print("WARNING: {}".format(message), file=sys.stderr)

def ramp_up_scale(z):
    return min(1.0, z * (1.0 - RAMP_UP_SCALE0) / RAMP_UP_ZMAX + RAMP_UP_SCALE0)


parser = argparse.ArgumentParser(
    description='Post-processing script to convert M106 fan speed commands into beep sequences \
that can be detected by beepdetect.py, to obtain variable fan speed on 3D printers that \
lack a PWM fan output.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    argument_default=argparse.SUPPRESS)
# SUPPRESS hides useless defaults in help text, the downside is needing to use hasattr().
parser.add_argument('in_file', type=argparse.FileType('r'),
                    help='file to process')
parser.add_argument('-o', '--out_file', type=argparse.FileType('w'),
                    help='optional file to write to (default is to print to standard output)')
parser.add_argument('-a', '--allow_split', action='store_true',
                    help='Allow splitting long moves to maintain correct lead time. This may cause visible seams.')
parser.add_argument('-d', '--debug', action='store_true',
                    help='enable debug output on stderr')
parser.add_argument('-i', '--timings', action='store_true',
                    help='Append a comment with estimated nonzero time to each line')
parser.add_argument('-P', '--no_process', action='store_true',
                    help='Output the file without doing fan command processing, useful in combination with --timings')
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

args = parser.parse_args()

debug = hasattr(args, 'debug')
allow_split = hasattr(args, 'allow_split')
no_process = hasattr(args, 'no_process')

print_debug("Debug output enabled, prepare to be spammed")

output = args.out_file if hasattr(args, 'out_file') else sys.stdout
gcode = GCodeStreamer(args, output)
off_commands = gcode.speed_to_m300_commands(0.0, "fan off", skip_repeat=False)
try:
    if no_process:
        gcode.start()
    else:
        # Assumption: anything before the end of the start G-code will only contain 'fan off'
        # instructions, either using M107, or M106 S0.
        if gcode.start(("M106", "M107"), off_commands):
            gcode.last_sequence = GCodeStreamer.speed_to_beep_sequence(0)
except EOFError as err:
    print_error(err)
    sys.exit(1)

if no_process:
    while True:
        try:
            line = gcode.get_next_event()
        except EOFError:
            print_error("Unexpected end of file reached!")
            sys.exit(1)
        except EndOfPrint:
            break
    gcode.stop()
    sys.exit(0)

args_dict = vars(args)
del(args_dict['in_file'], args_dict['out_file'])
args_dict['allow_split'] = allow_split
params = []
for arg, value in sorted(iter(args_dict.items())):
    params.append("{}={}".format(arg, value))
gcode.append_buffer(["; pwm_postprocessor.py version {}; parameters: {}".format(
    VERSION, ", ".join(params))])
print_debug("=== End of start G-code reached, now beginning actual processing ===")

current_fan_speed = 0.0  # Actual scaled speed. Assume fan always off at start.
while True:
    try:
        # look_ahead must be at least:
        #   2 to ignore Z-hop travel moves,
        #   1 to ignore duplicate M106 commands,
        #   2 to already be aware of changed Z in case of fan command followed by layer change,
        #   3 to combine the previous two cases.
        # This is again Slic3r-specific, it inserts M106 before changing the layer.
        line = gcode.get_next_event(3)
    except EOFError:
        print_error("Unexpected end of file reached!")
        sys.exit(1)
    except EndOfPrint:
        if current_fan_speed:
            print_debug("End of print reached while fan still active: inserting off sequence")
            gcode.append_buffer(off_commands)
        break
    print_debug("Interesting line: {}".format(line))

    layer_change = False
    if line.startswith(("M106", "M107")):
        gcode.pop()  # Get rid of this invalid Sailfish command
        print_debug("  -> Fan command")
    elif gcode.ahead_target_speed is not None:
        # Layer change while fan is active: check if we need to update fan speed
        print_debug("  -> Layer change {}".format(gcode.current_layer_z))
        layer_change = True
    else:
        print_debug("  -> Layer change {}, but fan is off".format(gcode.current_layer_z))
        continue

    # Determine both the speed we would need to set according to this event and any speed
    # command in the ahead buffer. Both will be scaled according to the (ahead) Z coordinate.
    scale = ramp_up_scale(gcode.current_layer_z)
    now_fan_speed = gcode.current_target_speed * scale
    ahead_fan_speed = gcode.ahead_target_speed * scale
    original_speed = gcode.current_target_speed

    if now_fan_speed != ahead_fan_speed:
        # Two commands (or layer change + command) very close to each other. See if we cannot
        # do anything smarter than what the slicer tries to make us do.
        t_ahead = gcode.ahead_time_until(("M106", "M107"))
        if debug:
            assert t_ahead >= 0
        if t_ahead < 0.1:
            # Either t == 0.0 because the slicer program suffered a fit of dementia and inserted
            # two speed changes with nothing in between them, or there is only one ridiculously
            # short move in between the commands and it is pointless to try to spin the fan up or
            # down just for that period. Immediately jump to the final speed.
            # Mind that only one short bridge move can cause this scenario: if there are more
            # moves, look_ahead won't see the ending M106 command even if the moves take less
            # than 0.1s. This is good because cooling a short bridge move is useless, but cooling
            # a sharp overhanging corner, even if it is tiny, is useful.
            print_debug("  Replacing this speed change with {} that follows within 0.1s".format(
                ahead_fan_speed))
            now_fan_speed = ahead_fan_speed
            original_speed = gcode.ahead_target_speed
            # optimization: avoid triggering another event for the command we already handled
            gcode.drop_ahead_commands(("M106", "M107"))
            gcode.current_target_speed = gcode.ahead_target_speed
        elif (now_fan_speed < current_fan_speed and now_fan_speed < ahead_fan_speed
              and t_ahead < 1.5):
            # It is pointless to try to spin down the fan for such a short time due to inertia.
            if ahead_fan_speed <= current_fan_speed:
                # Either we're maintaining speed or slowing down: set ahead as target speed
                print_debug(
                    "  No use slowing down for {:.2f}s, advance to upcoming speed {}".format(
                        t_ahead, ahead_fan_speed))
                now_fan_speed = ahead_fan_speed
                original_speed = gcode.ahead_target_speed
                gcode.drop_ahead_commands(("M106", "M107"))  # optimization
                gcode.current_target_speed = gcode.ahead_target_speed
            else:
                # We'll be speeding up: just maintain current speed and ignore this event entirely
                print_debug(
                    "  No use slowing down for {:.2f}s before upcoming speed-up, skip".format(
                        t_ahead))
                continue

    if now_fan_speed == current_fan_speed:
        print_debug("    -> already at required speed {}".format(current_fan_speed))
        continue

    if now_fan_speed:
        scaled = " scaled {:.3f}".format(scale) if scale < 1.0 else ""
        comment = "fan PWM {}{} = {:.2f}%".format(original_speed, scaled, now_fan_speed / 2.55)
    else:
        comment = "fan off"
    if layer_change:
        comment += " (layer change)"
    # Do not move the final M107 forward, to maximize cooling of spiky things. Again rely
    # on look_ahead (will fail if there is filament-specific end code).
    if gcode.end_of_print and not now_fan_speed:
        lead = 0.0
        comment += ", no backtrack"
    else:
        lead = args.lead_time
    print_debug("    -> set {}".format(comment))

    # No point in trying to get perfect timing on a fan speed update due to layer change.
    split_it = False if layer_change else allow_split
    if not gcode.inject_beep_sequence(now_fan_speed, comment, lead, split_it):
        print_debug("      but sequence is same as before, hence skip")
    current_fan_speed = now_fan_speed

gcode.stop()

if gcode.m126_7_found:
    print_warning("M126 and/or M127 command(s) were found inside the body of the G-code. \
Most likely, your fan will not work for this print. Are you sure your slicer is outputting \
G-code with M106 commands (e.g. RepRap G-code flavor)?")
