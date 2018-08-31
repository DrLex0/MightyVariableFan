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
import time

import cherrypy
import RPi.GPIO as GPIO


#### Defaults, either pass custom values as command-line parameters, or edit these. ####

SERVER_PORT = 8080
STATIC_CONTENT_DIR = "/home/pi/pwm_server"

# The GPIO pin to use.
# The software PWM in RPi.GPIO seems decent enough for controlling a fan because minor jitter
# doesn't matter. However, if it does prove to be troublesome, a solution that uses hardware PWM
# will be needed instead. For this reason, it is recommended to use GPIO pin 12 so you wouldn't
# need to open up things and re-plug cables if this change is made.
# (Another good reason for pin 12 is that it is practical, it is next to GND pin 14.)
PWM_PIN = 12
# My fan doesn't like high PWM frequencies. 200Hz works very well, and helps with low duty cycles.
# You may be able to reduce noise by carefully choosing this value.
PWM_FREQ = 200
# Lowest allowed duty cycle (%), meaning the lowest duty cycle where the fan won't stall. This
# value will override any nonzero duty cycle below it.
# Believe it or not, my fan still runs at 1% DC.
PWM_MIN_DC = 1.0

# PWM kickstart parameters. Kickstart always works at 100% duty cycle, only the duration of the
#   'kick' varies.
# The time to kick when we start from zero, it must be enough to bring the fan above stall speed.
KICK_LAUNCH = 0.25
# The duration of a kick is calculated as the difference in duty cycles (percentages) multiplied
# by this factor. This should be tuned such that the fan just doesn't overshoot (or only slightly
# overshoots) the target speed.
KICK_FACTOR = 0.01

# In case you're running this on something else than a Pi
MACHINE_NAME = "Raspberry Pi"

#### End of defaults section ####

#### Configuration section for fixed values ####

# Path to the lock file of beepdetect.py, will be used to show a warning if it isn't running.
DETECTOR_LOCK_FILE = "/run/lock/beepdetect.lock"

#### End of configuration section ####


class PWMController:
    """Allows to control a GPIO pin on the Raspberry Pi with PWM output, with support for
    'kickstarting' the output to help with starting at low target speeds, and faster
    transitioning to higher speeds."""

    def __init__(self, config):
        """Create a new PWMController.
        @config must be an ArgumentParser arguments object."""
        self.kick_launch = config.kick_launch
        self.kick_factor = config.kick_factor
        self.kickstart = bool(config.kick_launch or config.kick_factor)
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(config.pin, GPIO.OUT)
        self.pwm_out = GPIO.PWM(config.pin, config.frequency)
        self.duty = 0.0
        self.pwm_out.start(self.duty)  # clear any leftover state
        self.pwm_out.stop()

    def __del__(self):
        self.pwm_out.stop()

    def set_duty(self, duty, kick_override=None):
        """Sets the duty cycle of the output. Global kickstart behavior can be overridden
        by passing a boolean in kick_override."""
        if duty:
            do_kickstart = kick_override if kick_override is not None else self.kickstart
            # Don't bother with kickstart if the target DC is near 1 anyway
            if do_kickstart and duty > self.duty and duty < 95.0:
                kick_duration = (duty - self.duty) * self.kick_factor
                if self.duty == 0 and kick_duration < self.kick_launch:
                    kick_duration = self.kick_launch
                if not self.duty:
                    self.pwm_out.start(100)
                else:
                    self.pwm_out.ChangeDutyCycle(100)
                # Ideally this should be handled asynchronously, but due to the short times
                # I deem it too much hassle for what it's worth.
                time.sleep(kick_duration)
            if not self.duty:
                self.pwm_out.start(duty)
            else:
                self.pwm_out.ChangeDutyCycle(duty)
        else:
            self.pwm_out.stop()
        self.duty = duty

    def get_duty(self):
        """Return current duty cycle."""
        return self.duty

    @staticmethod
    def shutdown():
        """To be invoked when about to stop the server."""
        GPIO.cleanup()


class GpioServer:
    """Handles HTTP requests to control the PWM output."""

    def __init__(self, config, ramp_up_test=False):
        """Create a new server.
        @config must be an ArgumentParser arguments object.
        If @ramp_up_test, the PWM will be sweeped from zero to max upon startup."""
        self.override = False
        self.duty = 0.0
        self.scale_mult = 1.0
        self.active = False
        self.pwm = PWMController(config)
        self.pwm_min_dc = config.minimum_dc
        self.machine_name = config.name
        self.shutdown_token = None

        if ramp_up_test:
            self.pwm_ramp_up_test()

    def update_pwm_duty(self):
        """Sets the actual PWM duty cycle according to server state, i.e. active state,
        the last set duty cycle, and the scale multiplier."""
        if not self.active:
            self.pwm.set_duty(0.0)
            return
        duty = self.duty * self.scale_mult
        self.pwm.set_duty(duty if duty < 100.0 else 100.0)

    def pwm_ramp_up_test(self):
        """Sweeps the PWM from zero to max over 3 seconds, then returns to previous level."""
        for i in range(0, 101, 5):
            self.pwm.set_duty(i, kick_override=False)
            time.sleep(0.15)
        self.update_pwm_duty()

    def shutdown_machine(self):
        """Initiate a shutdown of the machine this script runs on."""
        # Prevent the beep detector from reviving the PWM (even though that would mean you're
        # shutting down the Pi while the printer is still working).
        self.override = True
        # If I don't do this, something in RPi.GPIO hangs and causes a segfault in the end.
        self.pwm.set_duty(0)
        # Instead of invoking shutdown directly, do it via a script that forks and then invokes
        # shutdown after a few seconds, so we still have time to return a response and do not
        # need to try something awkward to make CherryPy commit seppuku.
        subprocess.Popen(["/usr/local/bin/shutdownpi"], cwd="/")
        # I tried invoking cherrypy.engine.exit() here. Bad idea: somehow it delays the stopping
        # of the server compared to just waiting for the SIGHUP or SIGKILL.
        return GpioServer.html(
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
        active = "active" if self.active else "<span class='warn'>inactive</span>"
        if basic:
            scaled = ", scale {:.2f}".format(self.scale_mult) if self.scale_mult != 1.0 else ""
            return GpioServer.html(
                "PWM Server",
                "PWM status: {}, duty cycle = <b>{:.2f}</b>{}".format(
                    active, self.duty, scaled))

        if self.active:
            pwm_toggle = "<a href='/disable?manual=1'>disable</a>"
        else:
            pwm_toggle = "<a href='/enable?manual=1'>enable</a>"

        override = "<span class='warn'>ON</span>" if self.override else "off"
        if self.override:
            manual_toggle = "<a href='/man_override?enable=0'>disable</a>"
        else:
            manual_toggle = "<a href='/man_override?enable=1'>enable</a>"

        detector_warning = ""
        if not os.path.exists(DETECTOR_LOCK_FILE):
            detector_warning = "<br><span class='warn'>Warning: beepdetect is not running!</span>"

        # TODO: increment/decrement buttons next to presets, or replace presets with a slider
        pwm_presets = ["<a href='/setduty?d={d}&manual=1'>[{d}%]</a>".format(d=duty)
                       for duty in [0, 10, 20, 25, 30, 35, 40, 50, 60, 70, 75, 80, 90, 100]]
        shutdown = "<br><a href='/'>Refresh</a>&nbsp; <a href='/shutdown'>Shutdown</a>"
        scaler = "1.000" if self.scale_mult == 1.0 else "<b>{:.3f}</b>".format(self.scale_mult)
        scaler = "Scale <b><a href='/scale?factor=0.95238'>– –</a></b> {} "\
                 "<b><a href='/scale?factor=1.05'>+ +</a></b>&nbsp;&nbsp; "\
                 "<a href='/scale?reset=1'>(↺)</a><br>".format(scaler)
        effective_duty = self.duty * self.scale_mult
        if effective_duty > 100.0:
            effective_duty = 100.0
        scaled = " ({:.2f} scaled)".format(effective_duty) if self.scale_mult != 1.0 else ""

        return GpioServer.html(
            "PWM Server on {}".format(self.machine_name),
            ("PWM status: {} [{}]<br>".format(active, pwm_toggle) +
             "Manual override: {} [{}]<br>".format(override, manual_toggle) +
             "Duty cycle = <b>{:.2f}</b>{}<br>".format(self.duty, scaled) +
             "Set duty: {}<br>{}{}{}".format(" ".join(pwm_presets), scaler,
                                             detector_warning, shutdown)))

    @staticmethod
    def needs_override():
        """Returns the page to be shown if a request was ignored due to manual override."""
        return GpioServer.html(
            "Manual override in effect",
            "Ignoring this request because the server is in manual override mode, and \
the request lacks the 'manual' parameter.<br><a href='/'>Back</a>")

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
            return GpioServer.needs_override()

        if 0 < duty_value < self.pwm_min_dc:
            duty_value = self.pwm_min_dc
        self.duty = duty_value
        self.update_pwm_duty()
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
            self.scale_mult = 1.0
        else:
            self.scale_mult *= scale_value
            if round(self.scale_mult, 3) == 1.0:
                self.scale_mult = 1.0
        self.update_pwm_duty()
        return self.server_status()

    @cherrypy.expose
    def enable(self, manual=None, basic=None):
        """Enables the PWM output, resuming any previously set duty cycle."""
        if self.override and not manual:
            return GpioServer.needs_override()
        self.active = True
        self.update_pwm_duty()
        return self.server_status(basic)

    @cherrypy.expose
    def disable(self, manual=None, basic=None):
        """Disables the PWM output."""
        if self.override and not manual:
            return GpioServer.needs_override()
        self.active = False
        self.update_pwm_duty()
        return self.server_status(basic)

    @cherrypy.expose
    def man_override(self, enable):
        """Enables or disables the manual override mode."""
        # Why not just 'override' as path? Because there is already a member variable with
        # that name and this causes CherryPy to not recognize it as a path.
        self.override = True if enable and enable != "0" else False
        return self.server_status()

    @cherrypy.expose
    def shutdown(self, token=None):
        """This is provided to allow shutting down the Pi cleanly from a web interface, which
        is better than just pulling the power. To minimize the risk of accidentally shutting
        down, e.g. because a browser tries to prefetch a page or reloads it from history, a
        token is generated when loading this URL, and only if the URL is reinvoked with this
        token, will the shutdown be initiated."""
        if self.shutdown_token == -1:
            return GpioServer.html(
                "Shutting down",
                ("<p>Shutdown already initiated!</p>" +
                 "<p><a href='/'>Main page (in case you power on again)</a></p>"))
        if token:
            if token == self.shutdown_token:
                self.shutdown_token = -1
                return self.shutdown_machine()
            return GpioServer.html(
                "Shutdown request ignored",
                ("<p>Invalid shutdown token. Your browser may be trying to reload an old page.</p>"
                 + "<p><a href='/'>Return to main page</a></p>"))

        self.shutdown_token = "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(16))
        return GpioServer.html(
            "Confirm shutdown",
            ("<p>Really shutdown the {}?</p>".format(self.machine_name) +
             "<p><a href='/shutdown?token={}'>Yes</a>&nbsp; ".format(self.shutdown_token) +
             "<a href='/' class='big'>No!</a></p>"))

    @staticmethod
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Simple server to control PWM on a GPIO output of a Raspberry Pi.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-p', '--port', type=int,
                        help='Port on which to serve',
                        default=SERVER_PORT)
    parser.add_argument('-s', '--static_dir',
                        help='Directory with static content like CSS files',
                        default=STATIC_CONTENT_DIR)
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
                        default=KICK_LAUNCH)
    parser.add_argument('-k', '--kick_factor', type=float, metavar='KF',
                        help='Duration of a kick versus difference in duty cycles',
                        default=KICK_FACTOR)
    parser.add_argument('-n', '--name',
                        help='Custom machine name to display',
                        default=MACHINE_NAME)

    args = parser.parse_args()

    # TODO: I might want to configure the server for production mode
    cherrypy.config.update({
        'global': {
            'server.socket_port': args.port,
            'server.socket_host': '0.0.0.0',
            # Useful to auto-reload the server when the script is overwritten with a new version.
            'engine.autoreload.on' : True
        }
    })
    pwm_app_config = {
        "/": {
            'tools.staticdir.on': True,
            'tools.staticdir.dir': args.static_dir
        }
    }
    # Override the default handle_SIGHUP, which will restart CherryPy if receiving a SIGUP while
    # running as daemon. Even though I spawn this script through nohup, somehow a SIGHUP still
    # ends up being received when the shutdownpi script is invoked from within. (You don't want to
    # know how much time I've wasted debugging this.)
    cherrypy.engine.signal_handler.handlers['SIGHUP'] = cherrypy.engine.signal_handler.bus.exit
    cherrypy.engine.subscribe('stop', PWMController.shutdown)
    cherrypy.quickstart(GpioServer(args, ramp_up_test=True), '/', pwm_app_config)
