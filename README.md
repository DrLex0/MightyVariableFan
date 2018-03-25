# MightyVariableFan

*Variable fan speed workaround for 3D printers based on the MightyBoard, like the FlashForge Creator Pro*<br>
by Alexander Thomas, aka Dr. Lex

## What is it?

The MightyBoard, as used in printers like the FlashForge Creator Pro, has one annoying limitation, and it is lack of **PWM ability for the cooling fan.** The fan is connected to the ‘EXTRA’ output on the board, and this output is only a binary toggle, meaning the fan can only be off or at full throttle. This makes it impossible to get optimal results with filaments that require a small or moderate amount of cooling, and there are many other reasons why one would want to vary the speed of the fan.

It is possible to recompile Sailfish firmware with a software PWM implementation on the EXTRA output, but this is only a static control that cannot be changed mid-print, and it cannot be controlled through G-code commands either. Installing a simple analog PWM controller is actually a better option because that will at least allow to change the PWM at any time, but still it needs to be done manually. I have done this for a few months until I got tired of having to babysit every print. I wanted fully automatic fan control from within the X3G file itself.

I have looked for sensible ways to implement PWM for the fan, in such a way that it could be controlled from within G-code, preferably with manual override. Ideally, the printer should be able to send the desired fan speed to a separate device like an Arduino or Raspberry Pi, which does the actual PWM. I found no sensible solution however, so I went for something *less sensible* instead. This solution relies on the **buzzer,** which from my experiments has proven to be a reliable one-way communication channel. Not only does it play tones with accurate timings, it also plays them exactly where they occur inside the X3G code, unlike the M126 and M127 commands that will toggle the EXTRA output at a rather unpredictable time before the command is expected to be executed.

### The setup is as follows:

1. A post-processing script takes G-code with fan speed commands (M106) as input, and outputs the same code with the commands replaced with M300 beep commands that play very specific sequences of 3 high-pitched blips using 4 specific frequencies. This allows to encode 64 levels, which is plenty for controlling a fan. This script also manipulates the speeds to optimize them (see advantages below).
2. A Raspberry Pi runs a Python script called *beepdetect.py*, that continuously listens to audio input from a simple USB sound card. A microphone attached to this card, is placed directly above the printer's buzzer. The script uses an FFT to detect the specific frequencies of the blips.
3. When beepdetect.py detects a sequence, it performs a HTTP call to a simple Python-based web server that also runs on the Pi, this is the *pwm\_server.py* script based on CherryPi.
4. The pwm\_server reacts to incoming calls that request a speed change, by manipulating PWM output on a GPIO pin of the Pi, this is done through the RPi.GPIO Python module (it only implements software PWM, but this is good enough for controlling a fan at low frequency).
5. The GPIO pin controls a simple MOSFET break-out board that is powered by the 24V of the main printer PSU. The fan is connected to this MOSFET's output.

### Advantages
This approach, crazy as it may seem, has quite a few advantages:
* No need to make any destructive modifications to your printer. Although some soldering may be needed to put together the cables for the microphone and GPIO, you do not need to solder anything on your printer. The only requirements are glueing in some 3D printed mounts for the Pi and MOSFET, re-wiring the fan, and mounting a power supply for the Pi. These can all fit inside the spacious insides of an FFCP.
* You do not need to recompile anything. If you would have attempted to do all this stuff inside a heavily hacked Sailfish build, next to that build itself you would also have needed to hack and rebuild the GPX program, and possibly your slicer, because otherwise you would have no way of getting the fan speed arguments in an X3G file.
* Decoupling the PWM controller from the printer's firmware allows to add all kinds of advanced control, without having to modify the printer or recompiling firmware. For instance, the PWM server controller has a kickstart feature to allow the fan to start at very low speeds, and to more quickly reach intermediate speeds.
* The post-processing script can optimize fan speeds and timings. It can ensure the fan will have spun up at the right time, and it can gradually ramp up fan speed between the first layers and higher ones. The latter is very important with most cooling duct designs, because at the lower layers the cooling may otherwise be excessive due to air being forced in between the platform and the extruders.
* If your Pi has a WiFi connection, the fan PWM server can be reached through any portable device like your smartphone, and you can manipulate fan speed at any time.
* The PWM server offers temporary override of fan behavior, or a permanent manual override, useful for experimenting or in case you suddenly notice you have misconfigured the cooling for a print that would be a pity to abort.
* Fan duty cycles as low as 1% can be used.
* If you already have installed a Raspberry Pi in your printer to have a camera feed or control other parts of the printer, you only need to install a USB sound card with microphone, an extra MOSFET, and some wires.

### Disadvantages
There is only one obvious disadvantage:
* The printer will be making a bit of extra noises as if R2D2 is trying to subtly get your attention. These blips are very short though, and don't really stand out above the usual printer noises. If you don't care about the buzzer, you can completely mute the beeps by desoldering the buzzer, and making a direct electrical connection between the ‘BUZZ BUZZ’ output on the MightyBoard and your USB sound card input. If you want to do this, you should use a decoupling capacitor, and you might need to add a resistor divider to attenuate the signal in case it is too strong.

Another possible disadvantage is that if your printer is placed next to a pen with pigs that can squeal extremely loud at the exact same frequencies as the blips, detection might fail. Please don't combine 3D printing with pigs.


## Instructions

Coming soon!

If you think this is awesome and you already want to get started collecting the required bits, make sure you have:
* a recent Raspberry Pi (a 3B is certainly fine, not sure about the older ones),
* a USB sound card (I used one of those ridiculously cheap ‘3D sound’ cards, they are crap but good enough for this),
* a MOSFET break-out board (I bought a cheap IRF520 board on eBay),
* some wires and plugs that fit the GPIO pins, try to find the shortest ones you can find because space is tight.

You can also already print a [minimized case for the sound card](https://www.thingiverse.com/thing:2822474), which will be quite essential to make it fit inside the printer.