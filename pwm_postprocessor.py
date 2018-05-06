#!/usr/bin/env python3
"""
Post-processing script that converts M106 commands into sequences of M300 beep commands that can
  be detected by the beepdetect.py running on a Raspberry Pi.
It can also handle scaling of the PWM values depending on Z coordinate, to compensate for the
  effect of exhaust air 'bouncing' against the bed and causing more cooling than expected.
Because M300 commands are timed pretty accurately (unlike M126 and M127 whose timing is sloppily
  anticipated based on the number of commands and not their duration), the script will try to
  shift them forward in time to compensate for the duration of the beep sequence and the time
  needed to spin up the fan. This allows accurate cooling even of tiny overhangs.
This script assumes the fan commands are M106 (with an S argument) and M107. This will only be
  the case if you configure your slicer to output G-code for RepRap or another firmware that
  supports variable fan speed (I recommend to stick to RepRap because there are only minor
  differences in G-code output between it and Sailfish).
As with all my other post-processing scripts, extrusion coordinates must be relative (M83).

Alexander Thomas a.k.a. DrLex, https://www.dr-lex.be/
Released under Creative Commons Attribution 4.0 International license.
"""

import argparse
import logging
import math
import re
import sys
from collections import deque

#### Defaults, either pass custom values as command-line parameters, or edit these. ####

# Z coordinate (mm) below which fan speeds will be linearly scaled with increasing Z.
# The correct value for this depends heavily on the design of your fan duct and extruder assembly.
RAMP_UP_ZMAX = 4.0

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


VERSION = '0.2'

# Number of lines in the buffers. More allows to cope with more detailed and faster prints, but
# is slower and requires more memory to process. If you need more than 128, you're probably
# printing pointlessly detailed objects at a speed where all details drown in ringing artefacts
# anyway.
BUFFER_SIZE = 128
DEBUG = False

# Multiply exact value with a margin to cater for possible stretching of the played beeps, as well
# as the fact that we're not considering acceleration when estimating times.
SEQUENCE_DURATION = 1.2 * (0.4 + SEQUENCE_LENGTH * 0.02 + (SEQUENCE_LENGTH - 1) * 0.1)

LOG = logging.getLogger('pwm_postproc')
LOG.setLevel(logging.INFO)

class EndOfPrint(Exception):
    """Signifies that we have seen END_MARKER during a read."""
    pass


class GCodeStreamer(object):
    """Class for reading a GCode file without having to shove it entirely in memory, by only
    keeping a buffer of the last read lines. When a new line is read and the buffer exceeds a
    certain size, the oldest line(s) will be popped from the buffer and sent to output."""

    def __init__(self, config, out_stream, max_buffer=BUFFER_SIZE):
        """@max_buffer is the largest number of lines that will be kept in memory before
        sending the oldest ones to @out_stream while reading new lines."""
        self.in_file = config.in_file
        self.feed_factor = config.feed_factor
        self.feed_limit_z = config.feed_limit_z
        self.print_times = hasattr(config, 'timings')

        self.output = out_stream
        self.max_buffer = max_buffer

        # Buffers contain tuples (line, z, fan_speed, time_estimate)
        # We're only interested in the duration of a small set of moves, but calculate the
        # duration of all moves anyway. Inefficient, but much saner than having to implement
        # a backtracking algorithm that calculates times on-the-fly.
        self.buffer = deque()
        self.buffer_ahead = deque()
        # Represents the printer state seen in the last read line.
        # f is feedrate, d is fan duty cycle.
        self.xyzfd = [0.0, 0.0, 0.0, 1.0, 0.0]
        self.end_of_print = False
        self.m126_7_found = False
        self.fan_override = None

        self.sequences_busy = 0
        self.sequence_time_left = 0.0
        self.seq_postponed = False

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
        if self.print_times:
            for data in self.buffer:
                line = data[0]
                tval = data[3]
                print(("{}; {:.3f}".format(line, tval) if tval else line), file=self.output)
            for data in self.buffer_ahead:
                line = data[0]
                tval = data[3]
                print(("{}; {:.3f}".format(line, tval) if tval else line), file=self.output)
        else:
            for data in self.buffer:
                print(data[0], file=self.output)
            for data in self.buffer_ahead:
                print(data[0], file=self.output)
        self.buffer.clear()
        self.buffer_ahead.clear()
        while True:
            line = self.in_file.readline()
            if not line:
                return
            print(line.rstrip("\r\n"), file=self.output)

    def _update_print_state(self, line):
        """Update the xyzfd state (except the d element), and return an estimate of how
        long this move takes. The estimate does not consider acceleration."""
        # I could try to capture this in one big awful regex, but doing it separately avoids being
        # locked into the output format of a specific slicer. I even allow "Z1.2 F321 X0.0 G1".
        found_x = re.match(r"[^;]*X(-?\d*\.?\d+)(\s|;|$)", line)
        found_y = re.match(r"[^;]*Y(-?\d*\.?\d+)(\s|;|$)", line)
        found_z = re.match(r"[^;]*Z(\d*\.?\d+)(\s|;|$)", line)
        found_f = re.match(r"[^;]*F(\d*\.?\d+)(\s|;|$)", line)

        xyzfd2 = list(self.xyzfd)  # copy values, not reference
        if found_z:
            if found_x or found_y:
                # Only vase mode print moves should combine X or Y move with Z change.
                # TODO: strictly spoken we should read the layer height from the file's parameter
                # section and use that as the threshold.
                new_z = float(found_z.group(1))
                if new_z >= xyzfd2[2] + 0.2:
                    xyzfd2[2] = float(new_z)
            else:
                xyzfd2[2] = float(found_z.group(1))

        if found_x:
            xyzfd2[0] = float(found_x.group(1))
        if found_y:
            xyzfd2[1] = float(found_y.group(1))
        if found_f:
            xyzfd2[3] = float(found_f.group(1))

        # TODO: G4 command can be directly converted to time (but should be rare in actual prints)
        # TODO: tool changes (should be assumed to take ridiculously long)
        time_estimate = 0.0
        # Assumption to simplify logic and calculations: Z component in a combined XYZ move has
        # a negligible time contribution compared to XY.
        if found_x or found_y:
            time_estimate = (math.hypot(xyzfd2[0] - self.xyzfd[0], xyzfd2[1] - self.xyzfd[1]) *
                             self.feed_factor / xyzfd2[3])
        elif found_z:
            feedrate = min(xyzfd2[3], self.feed_limit_z)
            time_estimate = abs(xyzfd2[2] - self.xyzfd[2]) * self.feed_factor / feedrate
        else:
            found_e = re.match(r"[^;]*E(-?\d*\.?\d+)(\s|;|$)", line)
            if found_e:  # retract move, luckily they're relative: no need to remember state
                time_estimate = abs(float(found_e.group(1))) * self.feed_factor / xyzfd2[3]

        self.xyzfd = xyzfd2
        return time_estimate

    def _read_next_line(self, ahead=False):
        """Read one line from the file and append it to the main buffer,
        or buffer_ahead if @ahead.
        Each buffer item is a tuple (line, z, duty_cycle, time_estimate) with:
            z = the layer Z coordinate for the line,
            duty_cycle = the current fan duty cycle at the line as dictated by the slicer,
            time_estimate = an estimate of how long execution of the line will take.
        If end of file is reached, raise EOFError. If END_MARKER is reached, raise EndOfPrint."""
        if self.end_of_print:
            raise EndOfPrint("End of print code reached")

        line = self.in_file.readline()
        if not line:
            raise EOFError("End of file reached")
        line = line.rstrip("\r\n")

        time_estimate = 0.0
        duty_cycle = self.xyzfd[4]

        if re.match(r"[^;]*G1(\s|;|$)", line):  # print or travel move
            time_estimate = self._update_print_state(line)
        elif re.match(r"(M126|M127)(\s|;|$)", line):
            self.m126_7_found = True
        elif line.startswith(END_MARKER):
            self.end_of_print = True
        else:
            # M107 is actually deprecated according to the RepRap wiki, but Slic3r still uses it.
            # Assumption: the S argument comes first (in Slic3r there is nothing except S anyway).
            fan_command = re.match(r"(M106|M107)(\s+S(\d*\.?\d+)|\s|;|$)", line)
            if fan_command:
                # An M106 without S argument will be treated as M106 S0 or M107.
                duty_cycle = 0.0
                if fan_command.group(1) == "M106" and fan_command.group(3):
                    duty_cycle = float(fan_command.group(3))
            self.xyzfd[4] = duty_cycle

        if ahead:
            self.buffer_ahead.append((line, self.xyzfd[2], duty_cycle, time_estimate))
        else:
            self.buffer.append((line, self.xyzfd[2], duty_cycle, time_estimate))
            if self.print_times:
                while len(self.buffer) > self.max_buffer:
                    old_data = self.buffer.popleft()
                    old_line = old_data[0]
                    old_time = old_data[3]
                    print(("{}; {:.3f}".format(old_line, old_time) if old_time else old_line),
                          file=self.output)
            else:
                while len(self.buffer) > self.max_buffer:
                    print(self.buffer.popleft()[0], file=self.output)

        if self.end_of_print:
            raise EndOfPrint("End of print code reached")

    def _get_next_ahead(self):
        """Move the next line from buffer_ahead to the regular buffer.
        If end of print is reached, raise EndOfPrint."""
        self.buffer.append(self.buffer_ahead.popleft())
        if not self.buffer_ahead and self.end_of_print:
            # The line we just moved must be END_MARKER.
            LOG.trace("EOP in _get_next_ahead, buffers: %d, %d",
                      len(self.buffer), len(self.buffer_ahead))
            raise EndOfPrint("End of print code reached")

    def override_fan_speed(self, speed):
        """Tell get_next_event that the fan speed for the last read line is @speed,
        no matter what the buffer says."""
        self.fan_override = speed

    def get_next_event(self, look_ahead=0):
        """Read lines from the file until something interesting is encountered. This can be:
        - an M106 or M107 command
        - a layer change (in case of vase mode prints, treat Z increase of 0.2 as layer change).
        NOTE: the state of this GCodeStreamer will be updated according to all read lines, hence
          the state will include what was seen in the @look_ahead lines. This helps with slicer
          quirks like placing fan commands right before layer changes, or placing two fan
          commands immediately after each other. It also avoids confusing a Z-hop travel move
          with a layer change.
        If end of file is reached, raise EOFError. If END_MARKER is reached, raise EndOfPrint."""
        if self.buffer:
            last_z = self.buffer[-1][1]
            if self.fan_override is None:
                last_fan = self.buffer[-1][2]
            else:
                last_fan = self.fan_override
                self.fan_override = None
        else:
            last_z = 0.0
            last_fan = 0.0

        while True:
            if self.buffer_ahead:
                self._get_next_ahead()
            else:
                self._read_next_line()
            LOG.trace("BUFFER: %s", self.buffer[-1])

            fan_command = False
            if last_fan != self.buffer[-1][2]:
                fan_command = True
                if self.seq_postponed:
                    # A new fan speed change makes any pending postponed one obsolete
                    LOG.trace("  Dropping postponed event")
                    self.seq_postponed = False

            apparent_layer_change = (self.buffer[-1][1] != last_z)

            postponed_event = False
            if self.sequences_busy:
                self.sequence_time_left -= self.buffer[-1][3]
                if self.sequence_time_left <= 0:
                    self.sequences_busy -= 1
                    LOG.trace("  Sequence finished playing, left to play: %d", self.sequences_busy)
                    if self.sequences_busy:
                        self.sequence_time_left += SEQUENCE_DURATION
                    if self.seq_postponed:
                        LOG.trace("  Triggering postponed event")
                        postponed_event = True
                        self.seq_postponed = False
                        # Disable the continue check for Z-hop down below
                        apparent_layer_change = False

            if fan_command or apparent_layer_change or postponed_event:
                # Something interesting (may have) happened!
                LOG.trace("  Z last %g -> now %g -> apparentLC? %s", last_z, self.buffer[-1][1],
                          apparent_layer_change)
                LOG.trace("  FAN last %g -> now %g", last_fan, self.buffer[-1][2])
                try:
                    # Top up buffer_ahead if necessary
                    for _ in range(look_ahead - len(self.buffer_ahead)):
                        self._read_next_line(True)
                except (EOFError, EndOfPrint):
                    pass
                # Avoid treating Z-hop as event: check whether Z wasn't reverted
                # in look_ahead after a few moves
                if (apparent_layer_change and
                        len(self.buffer_ahead) > 2 and self.buffer_ahead[2][1] == last_z):
                    LOG.trace("  No layer change: Z-hop")
                    continue
                if postponed_event:
                    # Insert marker so the main program knows this is a postponed event. Clone
                    # Z and fan speed values from the current line to allow reusing logic.
                    self.buffer.append(("POSTPONED",) + self.buffer[-1][1:3] + (0.0,))
                break

    def the_end_is_near(self, how_near=0):
        """Returns whether END_MARKER is in the first @how_near lines of the ahead buffer.
        If @how_near is zero, it defaults to BUFFER_SIZE/8."""
        if not self.end_of_print:
            # We haven't even read the line yet!
            return False
        if self.buffer and self.buffer[-1][0].startswith(END_MARKER):
            return True

        if not how_near:
            how_near = BUFFER_SIZE // 8
        for _, data in zip(range(how_near), self.buffer_ahead):
            if data[0].startswith(END_MARKER):
                LOG.trace("  The End Is Near!")
                return True
        return False

    def current_line(self):
        """Returns the most recent line in the main buffer."""
        return self.buffer[-1][0] if self.buffer else None

    def pop(self):
        """Removes the last line from the main buffer, and returns it."""
        return self.buffer.pop()[0]

    def append_buffer(self, lines, times=None):
        """Append the @lines at the end of the main buffer.
        The z and duty_cycle values will be set to None, the time_estimate values will
        be set to @times if defined, or 0.0."""
        if not times:
            times = [0.0 for _ in range(len(lines))]
        if DEBUG:
            assert len(lines) == len(times)

        last_z, last_s = (self.buffer[-1][1], self.buffer[-1][2]) if self.buffer else (0.0, 0.0)
        self.buffer.extend([(line, last_z, last_s, tval) for line, tval in zip(lines, times)])

    def insert_buffer(self, pos, lines, times=None, replace=False):
        """Insert extra @lines before, or replace the existing line at index @pos.
        The z and duty_cycle values will be set to those of the preceding line,
        the time_estimate values will be set to @times if defined, or 0.0."""
        if not self.buffer or pos >= len(self.buffer):
            self.append_buffer(lines, times)
            return

        if not times:
            times = [0.0 for _ in range(len(lines))]
        if DEBUG:
            assert len(lines) == len(times)

        if len(lines) == 1:
            previous = self.buffer[pos] if (replace or pos == 0) else self.buffer[pos - 1]
            data = [(line, previous[1], previous[2], tval) for line, tval in zip(lines, times)]
            if replace:
                self.buffer[pos] = data[0]
            else:
                self.buffer.insert(pos, data[0])
            return

        new_buffer = deque()
        for _ in range(pos):
            new_buffer.append(self.buffer.popleft())
        previous = new_buffer[-1] if new_buffer else self.buffer[0]
        if replace:
            self.buffer.popleft()
        new_buffer.extend([(line, previous[1], previous[2], tval)
                           for line, tval in zip(lines, times)])
        new_buffer.extend(self.buffer)
        self.buffer = new_buffer

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
            x, y = GCodeStreamer.parse_xy(self.buffer[i][0])
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

        data = self.buffer[position]
        fraction = 1.0 - (time2 / data[3])
        if DEBUG:
            assert fraction > 0
        time1 = fraction * data[3]

        end_xyzefc = GCodeStreamer.parse_xyzefc(data[0])
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
        self.insert_buffer(position, new_lines, [time1, time2], True)
        return True

    @staticmethod
    def speed_to_sequence(speed):
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

    @staticmethod
    def sequence_to_m300_commands(sequence, comment=""):
        """Return a list with commands to play a sequence that can be detected by beepdetect.py.
        @sequence is a list with indices in the SIGNAL_FREQS array.
        @comment will be inserted with the commands."""
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
        If @allow_split, a move at @position may be split up to improve lead time.
        Returns a tuple of the best index in the buffer and the chosen lead time."""
        LOG.debug("    Backtrack: %.3f ~ %.3f; position %d", t_next, t_elapsed, position)
        # Could discern between speeding up and slowing down. For slow-down it is
        #   more acceptable to be too late than for speed-up.
        if t_elapsed > 1.25 * lead_time:
            found = False
            if t_next >= 0.75 * lead_time:
                position += 1
                LOG.debug("    Picked %.3f: OK :)", t_next)
                t_elapsed = t_next
                found = True
            elif allow_split:
                # Split the move such that the second part gives us the last bit of time
                # needed to reach lead_time
                found = self.split_move(position, lead_time - t_next)
                if found:
                    position += 1
                    LOG.debug("    Split move because neither %.3f nor %.3f is acceptable :P",
                              t_elapsed, t_next)
                    t_elapsed = t_next + lead_time
                else:
                    LOG.debug("    Cannot split the move! :\\")

            if not found:
                if t_elapsed <= 2.0 * lead_time:
                    LOG.debug("    Picked %.3f: meh :/", t_elapsed)
                else:
                    position += 1
                    LOG.debug("    Picked %.3f: too late :(", t_next)
                    t_elapsed = t_next
        else:
            LOG.debug("    Picked %.3f: good :D", t_elapsed)

        return position, t_elapsed

    def inject_beep_sequence(self, sequence, comment="", lead_time=0.0, allow_split=False):
        """Insert the beep @sequence into the gcode, with @comment added.
        The position of the sequence will be chosen such that it leads the last line in the
        buffer by an approximate @lead_time seconds.
        Return value is the actual lead time that could be achieved.
        If @allow_split, long moves may be split to obtain a more accurate lead_time."""
        commands = GCodeStreamer.sequence_to_m300_commands(sequence, comment)
        if not lead_time:
            self.append_buffer(commands)
            return 0.0

        t_elapsed = 0.0
        # next in the file but previous in the algorithm, since we're going backwards...
        t_next = 0.0
        position = len(self.buffer)
        previous_sequence = False

        for data in reversed(self.buffer):
            if data[0] == "M300 S0 P200; end sequence":
                # Ensure not to jump across previously inserted sequence: swapping commands
                # would be bad!
                previous_sequence = True
                break
            position -= 1
            t_next = t_elapsed
            t_elapsed += data[3]
            if t_elapsed >= lead_time:
                break

        actual_time = t_elapsed
        if previous_sequence:
            LOG.debug("  Cannot backtrack more than %.3fs due to previous sequence", t_elapsed)
        elif position == 0:
            LOG.debug("Buffer too short to backtrack to lead time of %.3fs, it will only be %.3fs",
                      lead_time, t_elapsed)
        else:
            position, actual_time = self.optimize_lead_time(lead_time, position, t_elapsed,
                                                            t_next, allow_split)
        self.insert_buffer(position, commands)
        return actual_time


def ramp_up_scale(layer_z, config):
    """Calculate scale factor for fan speed at the lowest print layers."""
    return min(1.0, layer_z * (1.0 - config.scale0) / config.zmax + config.scale0)


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
                    help=('Allow splitting long moves to maintain correct lead time. ' +
                          'This may cause visible seams.'))
parser.add_argument('-d', '--debug', action='count',
                    help='enable debug output on stderr, repeat for trace level output')
parser.add_argument('-i', '--timings', action='store_true',
                    help='Append a comment with estimated nonzero time to each line')
parser.add_argument('-P', '--no_process', action='store_true',
                    help=('Output the file without doing fan command processing, useful ' +
                          'in combination with --timings'))
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

DEBUG = hasattr(args, 'debug')
TRACE = DEBUG and args.debug > 1
allow_split = hasattr(args, 'allow_split')
no_process = hasattr(args, 'no_process')

logging.TRACE = 9
logging.addLevelName(logging.TRACE, "TRACE")
def trace(self, message, *arguments, **kws):
    """Log at trace level: even more verbose than debug."""
    if self.isEnabledFor(logging.TRACE):
        #pylint: disable=redefined-outer-name,protected-access
        self._log(logging.TRACE, message, arguments, **kws)
logging.Logger.trace = trace

LOG_HANDLER = logging.StreamHandler(sys.stderr)
LOG_LEVEL = None
if TRACE:
    LOG_LEVEL = logging.TRACE
elif DEBUG:
    LOG_LEVEL = logging.DEBUG
if LOG_LEVEL is not None:
    LOG_HANDLER.setLevel(LOG_LEVEL)
    LOG.setLevel(LOG_LEVEL)
LOG_HANDLER.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
LOG.addHandler(LOG_HANDLER)
LOG.debug("Debug output enabled, prepare to be spammed")
LOG.trace("Trace output enabled, prepare to be thoroughly spammed")

output = args.out_file if hasattr(args, 'out_file') else sys.stdout
gcode = GCodeStreamer(args, output)
off_sequence = GCodeStreamer.speed_to_sequence(0.0)
off_commands = GCodeStreamer.sequence_to_m300_commands(off_sequence, "fan off")
last_sequence = []

try:
    if no_process:
        gcode.start()
    else:
        # Assumption: anything before the end of the start G-code will only contain 'fan off'
        # instructions, either using M107, or M106 S0.
        if gcode.start(("M106", "M107"), off_commands):
            last_sequence = off_sequence
except EOFError as err:
    LOG.error(err)
    sys.exit(1)

if no_process:
    while True:
        try:
            gcode.get_next_event()
        except EOFError:
            LOG.error("Unexpected end of file reached!")
            sys.exit(1)
        except EndOfPrint:
            break
    gcode.stop()
    sys.exit(0)

args_dict = vars(args)
del args_dict['in_file']
if 'out_file' in args_dict:
    del args_dict['out_file']
args_dict['allow_split'] = allow_split
params = []
for arg, value in sorted(iter(args_dict.items())):
    params.append("{}={}".format(arg, value))
print("; pwm_postprocessor.py version {}; parameters: {}".format(VERSION, ", ".join(params)),
      file=output)
LOG.debug("=== End of start G-code reached, now beginning actual processing ===")

set_fan_speed = 0.0  # Actual scaled speed. Assume fan always off at start.
current_layer_z = 0.0
while True:
    try:
        gcode.get_next_event(BUFFER_SIZE)
    except EOFError:
        LOG.error("Unexpected end of file reached!")
        sys.exit(1)
    except EndOfPrint:
        if set_fan_speed:
            LOG.debug("End of print reached while fan still active: inserting off sequence")
            gcode.append_buffer(off_commands)
        break
    LOG.debug("Interesting line: %s", gcode.current_line())

    layer_change = False
    is_postponed = False
    current_data = gcode.buffer[-1]
    original_speed = current_data[2]

    # ahead_layer_z is used for ramp-up scale. Look ahead a few lines because a layer change
    # may follow immediately after a fan command.
    ahead_layer_z = gcode.buffer_ahead[2][1] if len(gcode.buffer_ahead) > 2 else current_data[1]

    if current_data[0] == "POSTPONED":
        LOG.debug("  -> Postponed fan speed change")
        is_postponed = True
        gcode.pop()
        # The postponed command may have caused a layer change to be ignored. Ensure our state is
        # up-to-date (take minimum value to avoid being fooled by Z-hop).
        current_layer_z = min(current_data[1], ahead_layer_z)
        # Note: if we're unlucky, ahead_layer_z may have been picked on a Z-hop. The probability
        # of a postponed event is small to begin with, the risk of then being on a Z-hop is tiny,
        # and the consequences are minor. Therefore I won't waste CPU and sanity on it.
    elif current_layer_z == current_data[1]:
        # Must be a fan speed command
        if DEBUG:
            assert current_data[0].startswith(("M106", "M107"))
        LOG.debug("  -> Fan command")
        gcode.pop()  # Get rid of this invalid Sailfish command
        # get_next_event relies on the last line to detect fan speed changes, but we've just
        # wiped it, therefore override.
        gcode.override_fan_speed(original_speed)
    else:
        # Layer change
        current_layer_z = current_data[1]
        if current_data[2]:
            # Layer change while fan is active: we'll see if fan speed needs change
            LOG.debug("  -> Layer change %g", current_layer_z)
        else:
            LOG.debug("  -> Layer change %g, but fan is off", current_layer_z)
            continue
        layer_change = True

    # Determine both the speed we would need to set according to this event, and any speed
    # command in the ahead buffer. Both will be scaled according to the (ahead) Z coordinate.
    scale = ramp_up_scale(ahead_layer_z, args)
    now_fan_speed = original_speed * scale
    ahead_fan_time = 0.0
    ahead_fan_speed = now_fan_speed
    original_ahead_speed = original_speed

    # No point looking ahead when we already know we're going to skip this command/Z change event
    # because we're already at the required speed.
    if now_fan_speed != set_fan_speed:
        for data in gcode.buffer_ahead:
            # Timing of layer-related fan speed changes is not important, therefore do not look
            # for them.
            if data[2] != original_speed:
                next_scale = scale if data[1] == ahead_layer_z else ramp_up_scale(data[1], args)
                ahead_fan_speed = data[2] * next_scale
                original_ahead_speed = data[2]  # only for logging
                break
            ahead_fan_time += data[3]
            if ahead_fan_time > 1.5:
                # No use in looking further, 1.5s is enough to play any queued sequences, and
                # too long to skip anything due to inertia of the fan.
                break

    LOG.trace("Ahead fan time = %.3f", ahead_fan_time)
    if now_fan_speed != ahead_fan_speed:
        # Two commands (or layer change + command) very close to each other. See if we cannot
        # do anything smarter than what the slicer tries to make us do.
        if ahead_fan_time < 0.04:
            # Either t == 0.0 because the slicer program suffered a fit of dementia and inserted
            # two speed changes with nothing in between them, or there is only one ridiculously
            # short move in between the commands and it is pointless to try to spin the fan up or
            # down just for that period. Immediately jump to the final speed.
            # Considering a period of 40ms may seem overkill, but within that little time a sharp
            # overhanging corner may be printed, and cooling certainly is useful for those.
            LOG.debug("  Replacing this speed change with %g that follows within 40ms",
                      ahead_fan_speed)
            now_fan_speed = ahead_fan_speed
            original_speed = original_ahead_speed  # for logging
            # I could drop the ahead command here, but it is probably more efficient to just
            # stay within the flow of the algorithm.
        elif (now_fan_speed < set_fan_speed or now_fan_speed < ahead_fan_speed
              and ahead_fan_time < 1.5):
            # It is pointless to try to spin down the fan for such a short time due to inertia,
            # also going to an intermediate speed for such a short time is overkill.
            if ahead_fan_speed <= set_fan_speed or now_fan_speed <= ahead_fan_speed:
                # If next speed is the same or lower as previous, or higher than the wanted speed,
                # immediately go to ahead speed.
                LOG.debug(
                    "  Slower speed for %.3fs not useful, advance to upcoming speed %g",
                    ahead_fan_time, ahead_fan_speed)
                now_fan_speed = ahead_fan_speed
                original_speed = original_ahead_speed  # for logging
            else:
                # Either we'll be speeding up or slowing down in two stages: just maintain
                # current speed and ignore this event entirely.
                LOG.debug("  No use slowing down a bit for %.3fs, skip", ahead_fan_time)
                continue

    if now_fan_speed == set_fan_speed:
        LOG.debug("    -> already at required speed %g", set_fan_speed)
        continue
    now_sequence = GCodeStreamer.speed_to_sequence(now_fan_speed)
    if now_sequence == last_sequence:
        LOG.debug("    -> sequence for new speed %g is same as before, skip", set_fan_speed)
        continue

    if gcode.sequences_busy >= 2:
        LOG.debug("    -> !!! Too many sequences queued. Postponing.")
        gcode.seq_postponed = True
        continue

    if now_fan_speed:
        scaled = " scaled {:.3f}".format(scale) if scale < 1.0 else ""
        comment = "fan PWM {}{} = {:.2f}%".format(original_speed, scaled, now_fan_speed / 2.55)
    else:
        comment = "fan off"
    if layer_change:
        comment += " (layer change)"
    # When we're near the end of the print, no longer move fan off commands forward, to maximize
    # cooling of spiky things. This is especially true for the final M107 command right before
    # the end marker.
    if gcode.the_end_is_near(16) and not now_fan_speed:
        lead = 0.0
        comment += ", no backtrack"
    elif is_postponed:
        # Counteract the allowed margin on lead_time such that the sequence cannot start playing
        # sooner than necessary to offer enough space in the tune buffer.
        lead = args.lead_time / 2
    else:
        lead = args.lead_time
    LOG.debug("    -> set %s", comment)

    # No point in trying to get perfect timing on a fan speed update due to layer change.
    split_it = False if layer_change else allow_split
    actual_lead_time = gcode.inject_beep_sequence(now_sequence, comment, lead, split_it)

    set_fan_speed = now_fan_speed
    last_sequence = now_sequence
    if not gcode.sequences_busy:
        gcode.sequence_time_left = SEQUENCE_DURATION + (lead - actual_lead_time)
    gcode.sequences_busy += 1

gcode.stop()

if gcode.m126_7_found:
    # I might offer a fallback mode that treats those commands as M106 with a default speed.
    # This should be explicitly enabled via a CLI argument, otherwise this warning must appear.
    LOG.warning("WARNING: M126 and/or M127 command(s) were found inside the body of the G-code. \
Most likely, your fan will not work for this print. Are you sure your slicer is outputting \
G-code with M106 commands (e.g. RepRap G-code flavor)?")
