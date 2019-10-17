#!/usr/bin/env python3
"""
A basic server that allows to control the PWM output pin through HTTP requests, also offering
  a crude web interface to manually control the PWM.
The server can be switched to manual override mode, which requires every control request to
  have a 'manual=1' argument.

Alexander Thomas a.k.a. DrLex, https://www.dr-lex.be/
Released under Creative Commons Attribution 4.0 International license.
"""

import argparse
import os
import random
import string
import subprocess
import sys
import time

import cherrypy
from cherrypy.lib import auth_digest
import RPi.GPIO as rpi_gpio


#### Defaults ####
# The preferred way to override these is to define them in the defaults file at DEFAULTS_PATH
# (see below). Anything defined in that file has priority over the values defined below,
# but command-line arguments have the highest priority.

PWM_SERVER_PORT = 8081
STATIC_CONTENT_DIR = "/home/pi/pwm_server"

# Optional authentication for the server. Empty username or password disables authentication.
# The root of the server is always accessible but only allows viewing basic status.
# If authentication is enabled, access to the /api path requires login.
PWM_USER = ''
PWM_PASS = ''

# The GPIO pin to use.
# The software PWM in RPi.GPIO seems decent enough for controlling a fan because minor jitter
# doesn't matter. However if it does prove to be troublesome, a solution that uses hardware PWM
# will be needed instead. For this reason it is recommended to use GPIO pin 12 so you wouldn't
# need to open up things and re-plug cables if this change is made.
# (Another good reason for pin 12 is that it is practical, it is next to GND pin 14.)
PWM_PIN = 12
# My fan doesn't like high PWM frequencies. 200Hz works very well and helps with low duty cycles.
# You may be able to reduce noise by carefully choosing this value.
PWM_FREQ = 200
# Lowest allowed duty cycle (%), meaning the lowest duty cycle where the fan won't stall. This
# value will override any nonzero duty cycle below it.
# Believe it or not, my fan still runs at 1% DC.
PWM_MIN_DC = 1.0

# PWM kickstart parameters. Kickstart always works at 100% duty cycle, only the duration of the
#   'kick' varies.
# The time to kick when we start from zero, it must be enough to bring the fan above stall speed.
PWM_KICK_LAUNCH = 0.20
# The duration of a kick is calculated as the difference in duty cycles (percentages) multiplied
# by this factor. This should be tuned such that the fan just doesn't overshoot (or only slightly
# overshoots) the target speed.
PWM_KICK_FACTOR = 0.01

# In case you're running this on something else than a Pi
MACHINE_NAME = "Raspberry Pi"

#### End of defaults section ####


#### Configuration section for fixed values ####

# Path to the defaults configuration file
DEFAULTS_PATH = "/etc/default/mightyvariablefan"

# Path to the lock file of beepdetect.py, will be used to show a warning if it isn't running.
DETECTOR_LOCK_FILE = "/run/lock/beepdetect.lock"

#### End of configuration section ####


def html(title, body):
    """Wrap the body HTML in a mobile-friendly HTML5 page with given title and CSS file
    'style.css' from the static content directory."""
    return """<!DOCTYPE html>
<HTML>
<HEAD>
<TITLE>{}</TITLE>
<META name="viewport" content="width=device-width, initial-scale=1.0">
<LINK rel="stylesheet" href="/style.css" type="text/css">
</HEAD>
<BODY>
{}
</BODY>
</HTML>
""".format(title, body)


class PWMController:
    """Allows to control a GPIO pin on the Raspberry Pi with PWM output, with support for
    'kickstarting' the output to help with starting at low target speeds, and faster
    transitioning to higher speeds."""

    def __init__(self, config):
        """Create a new PWMController.
        @config must be an ArgumentParser arguments object."""
        self.pwm_min_dc = config.minimum_dc
        self.kick_launch = config.kick_launch
        self.kick_factor = config.kick_factor
        self.kickstart = bool(config.kick_launch or config.kick_factor)
        rpi_gpio.setmode(rpi_gpio.BOARD)
        rpi_gpio.setup(config.pin, rpi_gpio.OUT)
        self.pwm_out = rpi_gpio.PWM(config.pin, config.frequency)
        self.active = False
        self.duty_in = 0.0  # The unscaled last requested duty cycle
        self.duty = 0.0  # Actual set duty cycle
        self.scale = 1.0

        self.pwm_out.start(self.duty)  # clear any leftover state
        self.pwm_out.stop()

    def __del__(self):
        self.shutdown()

    def scale_duty(self, duty):
        """Return effective duty cycle according to scale and minimum duty cycle."""
        eff_duty = duty * self.scale
        if eff_duty > 100.0:
            return 100.0
        if eff_duty and eff_duty < self.pwm_min_dc:
            return self.pwm_min_dc
        return eff_duty

    def set_duty(self, duty, kick_override=None):
        """Sets the duty cycle of the output.
        The actual duty cycle will be determined by scale and minimum duty cycle.
        Global kickstart behavior can be overridden by passing a boolean in @kick_override."""
        current_duty = self.duty
        self.duty_in = duty
        self.duty = self.scale_duty(duty) if self.active else 0.0
        if self.duty:
            do_kickstart = kick_override if kick_override is not None else self.kickstart
            # Don't bother with kickstart if the target DC is near 1 anyway
            if do_kickstart and self.duty > current_duty and self.duty < 95.0:
                kick_duration = (self.duty - current_duty) * self.kick_factor
                if current_duty == 0 and kick_duration < self.kick_launch:
                    kick_duration = self.kick_launch
                if not current_duty:
                    self.pwm_out.start(100)
                else:
                    self.pwm_out.ChangeDutyCycle(100)
                # Ideally this should be handled asynchronously, but due to the short times
                # I deem it too much hassle for what it's worth.
                time.sleep(kick_duration)
            if not current_duty:
                self.pwm_out.start(self.duty)
            else:
                self.pwm_out.ChangeDutyCycle(self.duty)
        else:
            self.pwm_out.stop()

    def set_scale(self, scale):
        """Change the scale factor and update the PWM output accordingly."""
        self.scale = scale
        self.set_duty(self.duty_in)

    def activate(self, active=True):
        """Sets the enabled state of the PWM output. If disabled, the output remains off
        regardless of what other commands are given. If re-enabled, the PWM output is
        restored according to the last requested parameters."""
        self.active = active
        self.set_duty(self.duty_in)

    def ramp_up_test(self):
        """Sweeps the PWM from zero to max over 3 seconds, then returns to previous level."""
        if not self.duty:
            self.pwm_out.start(0.0)
        for i in range(0, 101, 5):
            self.pwm_out.ChangeDutyCycle(i)
            time.sleep(0.15)
        self.set_duty(self.duty_in)

    def shutdown(self):
        """To be invoked when about to stop the server."""
        if self.pwm_out is not None:
            self.pwm_out.stop()
            self.pwm_out = None
            rpi_gpio.cleanup()


class GpioDisplay:  # pylint: disable=too-few-public-methods
    """Basic page that shows current PWM state and offers access to the API page."""

    def __init__(self, pwm):
        self.pwm = pwm

    @cherrypy.expose
    def index(self):
        """Display the main (and only) page."""
        active = "active" if self.pwm.active else "<span class='warn'>inactive</span>"
        duty_raw = "Requested duty cycle = <b>{:.2f}</b>".format(self.pwm.duty_in)
        duty = "Actual duty cycle = <b>{:.2f}</b>".format(self.pwm.duty)
        detector_warning = ""
        if not os.path.exists(DETECTOR_LOCK_FILE):
            detector_warning = "<br><span class='warn'>Warning: beepdetect is not running!</span>"
        links = "<p><a href='/'>Refresh</a></p>\n<p><a href='/api/'>Go to interface page</a></p>"

        cherrypy.response.headers["Cache-Control"] = "max-age=0, max-stale=0"
        return html(
            "PWM Server",
            "PWM status: {}<br>{}<br>{}{}{}".format(
                active, duty_raw, duty, detector_warning, links))


class GpioAPI:
    """Handles HTTP requests to control the PWM output."""

    def __init__(self, pwm, config):
        """Create a new server.
        @config must be an ArgumentParser arguments object."""
        self.pwm = pwm
        self.override = False
        self.machine_name = config.name
        self.has_auth = bool(config.user and config.password)
        self.shutdown_token = None

    def shutdown_machine(self):
        """Initiate a shutdown of the machine this script runs on."""
        # Prevent the beep detector from reviving the PWM (even though that would mean you're
        # shutting down the Pi while the printer is still working).
        self.override = True
        # If I don't do this, RPi.GPIO will hang and postpone the shutdown.
        self.pwm.shutdown()
        # Instead of invoking shutdown directly, do it via a script that forks and then invokes
        # shutdown after a few seconds, so we still have time to return a response and do not
        # need to try something awkward to make CherryPy commit seppuku.
        subprocess.Popen(["/usr/local/bin/shutdownpi"], cwd="/")
        # I tried invoking cherrypy.engine.exit() here. Bad idea: somehow it delays the stopping
        # of the server compared to just waiting for the SIGHUP or SIGKILL.
        return html(
            "Shutdown initiated",
            ("<p>The {} will now shut down.</p>".format(self.machine_name) +
             "<p>Wait at least 15 seconds before pulling the power!</p>" +
             "<p><a href='/'>Main page (in case you power on again)</a></p>"))

    def server_status(self, basic=None):
        """This is the main page that will be returned upon every normal successful request.
        If @basic evaluates to True, any extras will be disabled and the response will be minimal.
        This should be be a simple page that can be used to control all basic functions of the
        server from a smallish touch display.
        TODO: create a much nicer UI that always stretches itself across small screens."""
        cherrypy.response.headers["Cache-Control"] = "max-age=0, max-stale=0"

        active = "active" if self.pwm.active else "<span class='warn'>inactive</span>"
        scale = self.pwm.scale
        if basic:
            scaled = ", scale {:.2f}".format(scale) if scale != 1.0 else ""
            return html(
                "PWM Server",
                "PWM status: {}, duty cycle = <b>{:.2f}</b>{}".format(
                    active, self.pwm.duty_in, scaled))

        if self.pwm.active:
            pwm_toggle = "<a href='disable?manual=1'>disable</a>"
        else:
            pwm_toggle = "<a href='enable?manual=1'>enable</a>"

        override = "<span class='warn'>ON</span>" if self.override else "off"
        if self.override:
            manual_toggle = "<a href='man_override?enable=0'>disable</a>"
        else:
            manual_toggle = "<a href='man_override?enable=1'>enable</a>"

        detector_warning = ""
        if not os.path.exists(DETECTOR_LOCK_FILE):
            detector_warning = "<br><span class='warn'>Warning: beepdetect is not running!</span>"

        # TODO: increment/decrement buttons next to presets, or replace presets with a slider
        pwm_presets = ["<a href='setduty?d={d}&manual=1'>[{d}%]</a>".format(d=duty)
                       for duty in [0, 10, 20, 25, 30, 35, 40, 50, 60, 70, 75, 80, 90, 100]]
        scaler = "1.000" if scale == 1.0 else "<b>{:.3f}</b>".format(scale)
        scaler = "Scale <b><a href='scale?factor=0.95238'>– –</a></b> {} "\
                 "<b><a href='scale?factor=1.05'>+ +</a></b>&nbsp;&nbsp; "\
                 "<a href='scale?reset=1'>(↺)</a><br>".format(scaler)
        effective_duty = self.pwm.scale_duty(self.pwm.duty_in)
        scaled = " ({:.2f} scaled)".format(effective_duty) if scale != 1.0 else ""

        shutdown = "<br><a href='/api/'>Refresh</a>&nbsp; <a href='shutdown'>Shutdown</a>"
        logout = "<br><a href='logout'>Logout</a>" if self.has_auth else ""

        return html(
            "PWM Server on {}".format(self.machine_name),
            ("PWM status: {} [{}]<br>".format(active, pwm_toggle) +
             "Manual override: {} [{}]<br>".format(override, manual_toggle) +
             "Duty cycle = <b>{:.2f}</b>{}<br>".format(self.pwm.duty_in, scaled) +
             "Set duty: {}<br>{}{}{}{}".format(" ".join(pwm_presets), scaler,
                                               detector_warning, shutdown, logout)))

    @staticmethod
    def needs_override():
        """Returns the page to be shown if a request was ignored due to manual override."""
        return html(
            "Manual override in effect",
            "Ignoring this request because the server is in manual override mode, and \
the request lacks the 'manual' parameter.<br><a href='/api/'>Back</a>")

    @cherrypy.expose
    def index(self, basic=None):
        """The main page."""
        return self.server_status(basic)

    @cherrypy.expose
    #pylint: disable=invalid-name
    def setduty(self, d, manual=None, basic=None):
        """Sets the PWM duty cycle.
        @d must be a number between 0.0 and 100.0, where 0 is off and 100 is full power.
        @manual means this request has manual override authority.
        If @basic, only a minimal status page is returned."""
        try:
            duty_value = float(d)
            if duty_value < 0 or duty_value > 100:
                raise ValueError("value out of range")
        except ValueError as err:
            # 422 was originally intended for WebDAV, but it has become a more general response
            # for 'invalid parameter value'.
            raise cherrypy.HTTPError(
                422,
                ("Invalid value '{}' for d parameter: ".format(d) +
                 "it must be a number between 0.0 and 100.0 ({})".format(err)))

        if self.override and not manual:
            return GpioAPI.needs_override()

        self.pwm.set_duty(duty_value)
        return self.server_status(basic)

    @cherrypy.expose
    def scale(self, factor=1.05, reset=None):
        """Multiplies the current scale multiplier by the given @factor with 0 < @factor < 100.
        Both the current duty cycle as well as any future incoming ones (manual or not)
        will be scaled by the resulting global multiplier.
        Because this is only meant for manual override, there is no 'manual' or
        'basic' argument.
        If @reset evaluates to True, @factor is ignored and the multiplier is reset to 1.0."""
        try:
            scale_value = float(factor)
            if scale_value <= 0 or scale_value >= 100:
                raise ValueError("value out of range")
        except ValueError as err:
            raise cherrypy.HTTPError(
                422,
                ("Invalid value '{}' for factor parameter: ".format(factor) +
                 "it must be a number larger than 0.0 and less than 100.0 ({})".format(err)))
        if reset:
            new_scale = 1.0
        else:
            new_scale = self.pwm.scale * scale_value
            if round(new_scale, 3) == 1.0:
                new_scale = 1.0
        self.pwm.set_scale(new_scale)
        return self.server_status()

    @cherrypy.expose
    def enable(self, manual=None, basic=None):
        """Enables the PWM output, resuming any previously set duty cycle."""
        if self.override and not manual:
            return GpioAPI.needs_override()
        self.pwm.activate()
        return self.server_status(basic)

    @cherrypy.expose
    def disable(self, manual=None, basic=None):
        """Disables the PWM output."""
        if self.override and not manual:
            return GpioAPI.needs_override()
        self.pwm.activate(False)
        return self.server_status(basic)

    @cherrypy.expose
    def man_override(self, enable):
        """Enables or disables the manual override mode."""
        # Why not just 'override' as path? Because there is already a member variable with
        # that name and this causes CherryPy to not recognize it as a path.
        self.override = True if enable and enable != "0" else False
        return self.server_status()

    @cherrypy.expose
    # pylint: disable=no-self-use
    def logout(self):
        """Trigger logout. There is no real way to log out from Digest auth, but some
        recent browsers will invalidate the auth when receiving a 401 response."""
        raise cherrypy.HTTPError(401, "LOGOUT")

    @staticmethod
    # pylint: disable=unused-argument
    def logged_out(status, message, traceback, version):
        """Handler for '401 Unauthorized' HTTPError."""
        if message == "LOGOUT":
            # HTML passed through message will be escaped, so we must generate it here.
            message = "<p>Depending on your browser, you might have been logged out. \
If not, you will need to quit and reopen your browser to force a logout.</p>\
<p><a href='/'>Return to main view</a></p>"
        return html(status, message)

    @cherrypy.expose
    def shutdown(self, token=None):
        """This is provided to allow shutting down the Pi cleanly from a web interface, which
        is better than just pulling the power. To minimize the risk of accidentally shutting
        down, e.g. because a browser tries to prefetch a page or reloads it from history, a
        token is generated when loading this URL, and only if the URL is reinvoked with this
        token, will the shutdown be initiated."""
        if self.shutdown_token == -1:
            return html(
                "Shutting down",
                ("<p>Shutdown already initiated!</p>" +
                 "<p><a href='/'>Main page (in case you power on again)</a></p>"))
        if token:
            if token == self.shutdown_token:
                self.shutdown_token = -1
                return self.shutdown_machine()
            return html(
                "Shutdown request ignored",
                ("<p>Invalid shutdown token. Your browser may be trying to reload an old page.</p>"
                 + "<p><a href='/api/'>Return to API page</a></p>"))

        self.shutdown_token = "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(16))
        return html(
            "Confirm shutdown",
            ("<p>Really shutdown the {}?</p>".format(self.machine_name) +
             "<p><a href='shutdown?token={}'>Yes</a>&nbsp; ".format(self.shutdown_token) +
             "<a href='/api/' class='big'>No!</a></p>"))


def read_defaults():
    """If there is a defaults file, override allowed values if the file specifies them.
    Format of the file is Python-style variable definitions, comments starting with #."""
    # Explicitly test on limited set of keys to disallow overriding arbitrary things
    overridable_defaults = [
        'PWM_SERVER_PORT', 'STATIC_CONTENT_DIR', 'PWM_USER', 'PWM_PASS', 'PWM_PIN',
        'PWM_FREQ', 'PWM_MIN_DC', 'PWM_KICK_LAUNCH', 'PWM_KICK_FACTOR', 'MACHINE_NAME'
    ]
    if os.path.isfile(DEFAULTS_PATH):
        line_index = 0
        with open(DEFAULTS_PATH, 'r') as def_file:
            while True:
                line = def_file.readline()
                line_index += 1
                if not line:
                    break
                line = line.split('#', 1)[0].strip()
                try:
                    key, _ = line.split('=', 1)
                except ValueError:
                    continue
                key = key.strip()
                if not key in overridable_defaults:
                    continue
                try:
                    exec("global {}\n{}".format(key, line))
                except Exception as err:
                    print("ERROR: failed to parse line {} in {}: {}".format(
                        line_index, DEFAULTS_PATH, err), file=sys.stderr)
                    sys.exit(3)

if __name__ == '__main__':
    read_defaults()

    parser = argparse.ArgumentParser(
        description='Simple server to control PWM on a GPIO output of a Raspberry Pi.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-p', '--port', type=int,
                        help='Port on which to serve',
                        default=PWM_SERVER_PORT)
    parser.add_argument('-s', '--static_dir',
                        help='Directory with static content like CSS files',
                        default=STATIC_CONTENT_DIR)
    parser.add_argument('-u', '--user',
                        help='User name for server login (leave empty for no login)',
                        default=PWM_USER)
    parser.add_argument('-a', '--password',
                        help='Password for server login (leave empty for no login)',
                        default=PWM_PASS)
    parser.add_argument('-i', '--pin', type=int,
                        help='The GPIO pin to control',
                        default=PWM_PIN)
    parser.add_argument('-f', '--frequency', type=float,
                        help='Frequency for the PWM signal',
                        default=PWM_FREQ)
    parser.add_argument('-m', '--minimum_dc', type=float, metavar='DC',
                        help='Minimum allowed duty cycle percentage',
                        default=PWM_MIN_DC)
    parser.add_argument('-l', '--kick_launch', type=float, metavar='T',
                        help='The time (seconds) to kick when starting from zero',
                        default=PWM_KICK_LAUNCH)
    parser.add_argument('-k', '--kick_factor', type=float, metavar='KF',
                        help='Duration of a kick versus difference in duty cycles',
                        default=PWM_KICK_FACTOR)
    parser.add_argument('-n', '--name',
                        help='Custom machine name to display',
                        default=MACHINE_NAME)

    args = parser.parse_args()

    PWM = PWMController(args)
    PWM.ramp_up_test()

    # I might want to configure the server for production mode, although quickstart seems OK
    cherrypy.config.update({
        'global': {
            'server.socket_port': args.port,
            'server.socket_host': '0.0.0.0',
            # Useful to auto-reload the server when the script is overwritten with a new version.
            'engine.autoreload.on' : True,
            'error_page.401': GpioAPI.logged_out
        }
    })

    pwm_display_config = {
        '/': {
            'tools.staticdir.on': True,
            'tools.staticdir.dir': args.static_dir
        }
    }

    pwm_api_config = {'/': {}}
    if args.user and args.password:
        pwm_api_config['/'] = {
            'tools.auth_digest.on': True,
            'tools.auth_digest.realm': 'PWM API',
            'tools.auth_digest.get_ha1':
                auth_digest.get_ha1_dict_plain({args.user: args.password}),
            'tools.auth_digest.key': 'b2fb6f93353cc4c6'
        }

    # Bah! But if I don't do this, RPi.GPIO usually segfaults due to some conflict with CherryPy.
    # If I ever have the time, I may want to look for an alternative PWM library that gives a
    # less shoddy impression.
    time.sleep(1)

    # Override the default handle_SIGHUP, which will restart CherryPy if receiving a SIGUP while
    # running as daemon. Even though I spawn this script through nohup, somehow a SIGHUP still
    # ends up being received when the shutdownpi script is invoked from within. (You don't want
    # to know how much time I've wasted debugging this.)
    cherrypy.engine.signal_handler.handlers['SIGHUP'] = cherrypy.engine.signal_handler.bus.exit
    cherrypy.engine.subscribe('stop', PWM.shutdown)

    cherrypy.tree.mount(GpioAPI(PWM, args), '/api', pwm_api_config)
    cherrypy.quickstart(GpioDisplay(PWM), '/', pwm_display_config)
