#!/usr/bin/env python
# A basic server that allows to control the PWM output pin through HTTP requests,
# also offering a crude web interface to manually control the PWM.
# The server can be switched to manual override mode, which requires every control
# request to have a 'manual=1' argument.

import cherrypy
import os
import random
import string
import subprocess
import sys
import time
import RPi.GPIO as GPIO


SERVER_PORT = 8080
STATIC_CONTENT_DIR = "/home/pi/pwm_server"

# The GPIO pin to use.
# The software PWM in RPi.GPIO seems decent enough for controlling a fan because minor jitter doesn't matter. However,
# if it does prove to be troublesome, a solution that uses hardware PWM will be needed instead. For this reason, it is
# recommended to use GPIO pin 12 so you wouldn't need to open up things and re-plug cables if this change is made.
# (Another good reason for pin 12 is that it is practical, it is next to GND pin 14.)
PWM_PIN = 12
# My fan doesn't like high PWM frequencies. 200Hz works very well.
PWM_FREQ = 200

# PWM kickstart parameters. Kickstart always works at 100% duty cycle, only the duration of the 'kick' varies.
# The time to kick when we start from zero. This must be enough to bring the fan above stall speed.
KICK_LAUNCH = 0.25
# The duration of a kick is calculated as the difference in duty cycles (percentages) multiplied by this factor.
# This should be tuned such that the fan just doesn't overshoot (or only slightly overshoots) the target speed.
KICK_FACTOR = 0.01

# In case you're running this on something else than a Pi
MACHINE_NAME = "Raspberry Pi"


class PWMController(object):
  """Allows to control a GPIO pin on the Raspberry Pi with PWM output, with support for 'kickstarting' the output
  to help with starting at low target speeds, and faster transitioning to higher speeds."""
  def __init__(self, pin, freq, kickstart=True):
    self.kickstart = kickstart
    self.duty = 0.0
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(pin, GPIO.OUT)
    self.pwm_out = GPIO.PWM(pin, freq)
    self.pwm_out.start(0)  # clear any leftover state
    self.pwm_out.stop()

  def __del__(self):
    self.pwm_out.stop()

  def setDuty(self, duty, kick_override=None):
    """Sets the duty cycle of the output. Global kickstart behavior can be overridden
    by passing a boolean in kick_override."""
    if duty:
      do_kickstart = kick_override if kick_override != None else self.kickstart
      # Don't bother with kickstart if the target DC is near 1 anyway
      if do_kickstart and duty > self.duty and duty < 95.0:
        kick_duration = (duty - self.duty) * KICK_FACTOR
        if self.duty == 0 and kick_duration < KICK_LAUNCH:
          kick_duration = KICK_LAUNCH
        if not self.duty:
          self.pwm_out.start(100)
        else:
          self.pwm_out.ChangeDutyCycle(100)
        # Ideally this should be handled asynchronously, but due to the short times I deem it too much hassle for what
        # it's worth.
        time.sleep(kick_duration)
      if not self.duty:
        self.pwm_out.start(duty)
      else:
        self.pwm_out.ChangeDutyCycle(duty)
    else:
      self.pwm_out.stop()
    self.duty = duty

  def getDuty(self):
    return self.duty

  @staticmethod
  def shutdown():
    GPIO.cleanup()


class GpioServer(object):
  def __init__(self, ramp_up_test=False):
    self.override = False
    self.duty = 0.0
    self.active = False
    self.pwm = PWMController(PWM_PIN, PWM_FREQ)
    if ramp_up_test:
      self.pwm_ramp_up_test()
    self.shutdown_token = None

  def pwm_ramp_up_test(self):
    """Sweeps the PWM from zero to max over 3 seconds, then returns to previous level."""
    for i in range(0, 101, 5):
      self.pwm.setDuty(i, kick_override=False)
      time.sleep(0.15)
    self.pwm.setDuty(self.duty)

  def shutdown_machine(self):
    self.pwm.setDuty(0)  # If I don't do this, something in RPi.GPIO hangs and causes a segfault in the end.
    pid = os.fork()
    if pid == 0:
      # Instead of invoking shutdown directly, do it via a script that forks and calls shutdown after a few seconds.
      # Double fork to ensure this process is entirely detached from the parent.
      subprocess.Popen(["/usr/local/bin/shutdownpi"], cwd="/")
      sys.exit(0)
    else:
      cherrypy.engine.exit()
      # For some reason, I must now kill CherryPy with extreme prejudice, otherwise it respawns itself and this delays
      # the shutdown for more than 1 minute. It is ugly but it works. Better solutions are welcome!
      time.sleep(.5)
      sys.exit(0)

  def server_status(self):
    """This is the main page that will be returned upon every normal successful request.
    This should be be a simple page that can be used to control all basic functions of the server from a
    smallish touch display."""
    pwm_toggle = "<a href='/disable?manual=1'>Disable PWM</a>" if self.active else "<a href='/enable?manual=1'>Enable PWM</a>"
    manual_toggle = "<a href='/man_override?enable=0'>Disable manual override</a>" if self.override else "<a href='/man_override?enable=1'>Enable manual override</a>"
    pwm_presets = ["<a href='/setduty?d={d}&manual=1'>[{d}%]</a>".format(d=duty) for duty in [0, 10, 20, 25, 30, 35, 40, 50, 65, 75, 100]]
    shutdown = "<br><a href='/shutdown'>Shutdown</a>"
    return GpioServer.html("PWM Server on {}".format(MACHINE_NAME),
      "PWM status: active = {}, duty cycle = <b>{:.2f}</b>, manual override = {}<br>{}<br>{}<br>Set duty: {}<br>{}".format(
        self.active, self.duty, self.override, pwm_toggle, manual_toggle, " ".join(pwm_presets), shutdown))

  def needs_override(self):
    return GpioServer.html("Manual override in effect",
      "Ignoring this request because the server is in manual override mode, and the request lacks the 'manual' parameter.<br><a href='/'>Back</a>")

  @cherrypy.expose
  def index(self):
    return self.server_status()

  @cherrypy.expose
  def setduty(self, d, manual=None):
    """@d must be a number between 0.0 and 100.0, where 0 is off and 100 is full power."""
    try:
      duty_value = float(d)
      if duty_value < 0 or duty_value > 100:
        raise ValueError("value out of range")
    except ValueError as err:
      # 422 was originally intended for WebDAV, but it has become a more general response for 'invalid parameter value'.
      raise cherrypy.HTTPError(422, "Invalid value '{}' for d parameter: it must be a number between 0.0 and 100.0 ({})".format(d, err))
    if self.override and not manual:
      return self.needs_override()

    self.duty = duty_value
    if self.active:
      self.pwm.setDuty(self.duty)
    return self.server_status()

  @cherrypy.expose
  def enable(self, manual=None):
    if self.override and not manual:
      return self.needs_override()
    if not self.active:
      self.pwm.setDuty(self.duty)
    self.active = True
    return self.server_status()

  @cherrypy.expose
  def disable(self, manual=None):
    if self.override and not manual:
      return self.needs_override()
    if self.active:
      self.pwm.setDuty(0)
    self.active = False
    return self.server_status()

  @cherrypy.expose
  def man_override(self, enable):
    # Weird: this does NOT work with plain 'override' as path. Apparently this is somehow hard-coded in CherryPy?
    self.override = True if enable and enable != "0" else False
    return self.server_status()

  @cherrypy.expose
  def shutdown(self, token=None):
    """This is provided to allow shutting down the Pi cleanly from a web interface, which is better than just pulling
    the power. To minimize the risk of accidentally shutting down, e.g. because some browser tries to prefetch a
    page or reloads it from history, a token is generated when loading this URL, and only if the URL is reinvoked
    with this token, will the shutdown be initiated."""
    if token:
      if token == self.shutdown_token:
        return self.shutdown_machine()
      else:
        return GpioServer.html("Shutdown request ignored",
          "Invalid shutdown token. Your browser is probably trying to reload an old page.<br><a href='/'>Return to main page.</a>")
    else:
      self.shutdown_token = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(16))
      return GpioServer.html("Confirm shutdown",
        "Really shutdown the {}?&nbsp <a href='/shutdown?token={}'>Yes</a> <a href='/'>No!</a>".format(
          MACHINE_NAME, self.shutdown_token))

  @staticmethod
  def html(title, body):
    """Wrap the body HTML in a mobile-friendly HTML5 page with given title and CSS file 'style.css' from
    the STATIC_CONTENT_DIR."""
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
  # TODO: command-line arguments
  # TODO: I might want to configure the server for production mode
  cherrypy.config.update({
    'server.socket_port': SERVER_PORT,
    'server.socket_host': '0.0.0.0'
  })
  pwm_app_config = {
    "/": {
      'tools.staticdir.on': True,
      'tools.staticdir.dir': STATIC_CONTENT_DIR
    }
  }
  cherrypy.engine.subscribe('stop', PWMController.shutdown)
  cherrypy.quickstart(GpioServer(ramp_up_test=True), '/', pwm_app_config)
