# MightyVariableFan

*Variable fan speed workaround for 3D printers based on the MightyBoard, like the FlashForge Creator Pro*<br>
by Alexander Thomas, aka Dr. Lex

*Instructions are unfinished at the moment!*

## What is it?

The MightyBoard, as used in printers like the FlashForge Creator Pro, has one annoying limitation, and it is lack of **PWM ability for the cooling fan.** The fan is connected to the ‘EXTRA’ output on the board, and this output is only a binary toggle, meaning the fan can only be off or at full throttle. This makes it impossible to get optimal results with filaments that require a small or moderate amount of cooling, and there are many other reasons why one would want to vary the speed of the fan.

It is possible to recompile Sailfish firmware with a software PWM implementation on the EXTRA output, but this is only a static control that cannot be changed mid-print, and it cannot be controlled through G-code commands either. Installing a simple manual hardware PWM controller is actually a better option because that will at least allow to change the PWM at any time, but it remains cumbersome. I have done this for a few months until I got tired of having to babysit every print. I wanted fully automatic fan control from within the X3G file itself.

I have looked for sensible ways to implement PWM for the fan, in such a way that it could be controlled from within G-code, preferably with manual override. Ideally, the printer should be able to send the desired fan speed to a separate device like an Arduino or Raspberry Pi, which does the actual PWM. I found no sensible solution however, so I went for something *less sensible* instead. This solution relies on the **buzzer,** which from my experiments has proven to be a reliable one-way communication channel. Not only does it play tones with accurate timings, it also plays them exactly where they occur inside the X3G code, unlike the M126 and M127 commands that will toggle the EXTRA output at a rather unpredictable time before the command is expected to be executed.

To make all this work, you need some minimal understanding of Linux, and at least basic electronics skills. Being able to solder might not be essential, but will make it much easier than trying to find ready-made components that will fit. Also, this has only been tested with Slic3r, and it is quite likely the post-processing script that generates the buzzer beep sequences will only work with Slic3r-generated G-code at this moment. Of course, the main reason why I have published this on GitHub is to make it easy for anyone to modify the code and commit updates to make it work with other slicer programs.

### How It Works

1. A **post-processing script** called *pwm_postprocessor.py* takes G-code with M106 fan speed commands as input, and outputs the same code with the commands replaced by M300 beep commands that play very specific sequences of 3 high-pitched blips using 4 specific frequencies. This allows to encode 64 levels, which is plenty for controlling a fan. This script also manipulates the speeds to optimize them (see ‘advantages’ below). For this to work, you need to let your slicer program generate G-code that contains M106 commands, meaning you will need to output for *RepRap* instead of Sailfish.
2. A **Raspberry Pi** mounted inside the printer runs a Python script called *beepdetect.py*, that continuously listens to audio input from a simple USB sound card. A **microphone** attached to this card, is placed directly above the printer's buzzer. The script uses an FFT to detect the specific frequencies of the blips.
3. When beepdetect.py detects a sequence, it performs a HTTP call to a simple Python-based web server that also runs on the Pi, this is the *pwm\_server.py* script based on CherryPy.
4. The pwm\_server reacts to incoming calls that request a speed change, by manipulating PWM output on a GPIO pin of the Pi, this is done through the RPi.GPIO Python module (which only implements software PWM, but this is good enough for controlling a fan at low frequency).
5. The GPIO pin controls a simple **MOSFET** break-out board that switches the 24V of the main printer PSU. The fan is connected to this MOSFET's output.

### Advantages

This approach, crazy as it may seem, has quite a few advantages:
* No irreversible modifications to your printer are needed. Although some soldering may be needed to construct the cables for the microphone and GPIO, you do not need to solder anything on your printer. The only requirements are glueing in some 3D printed mounts for the Pi and MOSFET, re-wiring the fan, and mounting a power supply for the Pi. These can all fit inside the spacious insides of an FFCP.
* You do not need to recompile anything. If you would have attempted to do all this stuff inside a heavily hacked Sailfish build, next to that build itself you would also have needed to hack and rebuild the GPX program, and possibly your slicer, because otherwise you would have no way of getting the fan speed arguments in an X3G file.
* Decoupling the PWM controller from the printer's firmware allows to add all kinds of advanced control, without having to modify the printer or recompiling firmware. For instance, the PWM server controller has a kickstart feature to allow the fan to start at very low speeds, and to more quickly reach intermediate speeds.
* The post-processing script can optimize fan speeds and timings. It can ensure the fan will have spun up at the right time, and it can gradually ramp up fan speed between the first layers and higher ones. The latter is very important with most cooling duct designs, because at the lower layers the cooling may otherwise be excessive due to air being forced in between the platform and the extruders.
* If your Pi has a WiFi connection, the fan PWM server can be reached through any portable device like your smartphone, and you can manipulate fan speed at any time.
* The PWM server offers temporary override of fan behavior, or a permanent manual override, useful for experimenting or in case you suddenly notice you have misconfigured the cooling for a print that would be a pity to abort.
* Fan duty cycles as low as 1% can be used even from standstill, thanks to the kickstart.
* If you already have installed a Raspberry Pi in your printer to have a camera feed or control other parts of the printer, you only need to install a USB sound card with microphone, an extra MOSFET, and some wires.
* If someone finds a less crazy way of making the printer communicate with the Pi, only the beepdetect and pwm_postprocessor scripts need to be updated.

### Disadvantages

* There is only one obvious disadvantage: the printer will be making a bit of extra noises, as if R2D2 is trying to subtly get your attention. These blips are very short though, and are played at frequencies the buzzer cannot play loud anyway. Thanks to these two choices, the blips don't really stand out above the usual printer noises.
* A less likely problem is that if your printer is placed next to a pen with pigs that can squeal *extremely* loud at the exact same frequencies as the blips, detection might fail. Please don't combine 3D printing with pigs. More realistic sources of disturbances are nearby machines that make loud hissing noises, or a loud music system very near to the printer. In my setup however, I couldn't even trigger any responses with the detector in debug mode without making unreasonably loud noises.

If you would suffer from one or both of these problems, they can be reduced or eliminated in several ways. The first way is to print a sealed adapter for attaching the microphone to the buzzer instead of a half-open one. This both attenuates the blips to a nearly inaudible level, and reduces influence of external noises.<br>
If you're not afraid of soldering on your printer's main board, you can omit the microphone and make a direct electrical connection between the buzzer contacts (marked ‘BUZZ BUZZ’) and the USB sound card. You should make the connection through a decoupling capacitor, and possibly add a resistor divider to attenuate the signal if necessary. This eliminates the outside noise problem completely. Moreover, you can unsolder the buzzer as well if you want to mute it entirely.


# Instructions

## Step 1: gather the required hardware

You need:
* A Raspberry Pi that has sufficient oomph to run the beep detector. I am not sure whether a Pi 2 suffices, but a 3B certainly does. These things are not expensive anyway, so if you already have an old Pi inside your printer, this might be a good time to upgrade it. There is a pretty good place to mount the Pi in the underside of the printer, by using 3D printed mounts that can be glued in place, I will publish these soon (**TODO**).
* A small 5V power supply for your Pi. Ideally, it should be small enough to fit inside your printer. I used a simple off-the shelf supply ([this one](https://www.conrad.be/p/raspberry-pi-netvoeding-sp-5c-zwart-raspberry-pi-3-b-1462834) to be exact) that I could tuck under a bundle of wires inside the printer. I connected it to the mains contacts of the printer's PSU, so the Pi is toggled together with the rest of the printer. This is where you'll have to be a bit creative with the parts you can find in local stores. You could take apart a USB power supply to more easily connect it to the mains, or you could plug the supply into half an extension cord, like I did. Whatever you do, do not create dubious and dangerous constructions that expose mains voltage.
* A USB sound card. It will be much easier if it is as small as possible. I recommend to buy one of those extremely cheap ‘3D sound’ cards with a yellow and green 3.5mm jack. These are rather crappy, but are very compact, well supported in Linux, and for the purpose of this application they are good enough. The plastic case of these cards is unnecessarily large; to make it much easier to fit inside your printer, you can print [this custom case](https://www.thingiverse.com/thing:2822474) if it fits your model of sound card (if not, modifying the model should be easy).
* A tiny microphone that can be mounted very close (within 10 mm) to the buzzer. I recommend to build your own microphone with a standard electret capsule (9.7mm diameter), a bit of shielded cable, and a 3.5mm plug. This can be mounted perfectly onto the buzzer with a 3D printed part I will publish shortly (**TODO**).
* A 24V MOSFET break-out board. There is a very common one for the IRF520, which is very easy to mount inside the printer with yet another printed part I will publish shortly (**TODO**).
* A cable to connect the GPIO pins on the Pi to the MOSFET. It is best to solder your own, using low-profile or angled plugs, because space may be tight depending on where you will mount the Pi.

## Step 2: prepare the Raspberry Pi

On your Pi, you need to ensure the following Debian packages are installed. You can run `sudo aptitude` to install them in a console UI, or simply do `sudo apt-get install` followed by a space-separated list of the package names:
* numpy
* scipy
* pyaudio
* python-cherrypy3
* python-requests-futures
* python-rpi.gpio

Next, copy the following files from this project to `/usr/local/bin/` on your Pi, and make them all executable:
* beepdetect.py
* pwm_server.py
* shutdownpi
* startpwmservices
* stoppwmservices

After logging in to the Pi and placing those files in a folder ‘stuff’ (which has nothing else in it), you can get them in the right place with these commands:
```
cd stuff
chmod a+x *
sudo mv * /usr/local/bin/
```
You also need to copy the *pwm_server* directory (with the CSS file inside it) in `/home/pi/`.

Finally, add this line before the “`exit 0`” line in `/etc/rc.local`:
```
/usr/local/bin/startpwmservices
``` 
You will need to do this as root, for instance use: `sudo nano /etc/rc.local` or: `sudo vim.tiny /etc/rc.local` depending on your preferred editor.

## Step 3: create the required parts

You need two **wires:** one for the microphone, and one for the MOSFET. The microphone cable will have a 3.5 mm stereo plug on one side, and be directly connected to the electret capsule at the other end (unless you want to add some kind of plug, your choice). The MOSFET cable simply needs two header plugs. At one end, it should be a plug with 2 contacts, and at the other end 3 contacts, with the middle contact left open.

If you're going to install the Pi in the same spot as I did, being in the front side of the bottom chamber next to the power supply, the cable going to the microphone, 3.5mm jack included, must be 31 cm long, and the cable going to the MOSFET board should be about 35 cm. Important: use either some kind of shielded cable for the microphone connection, or a twisted-pair cable. Don't just use any two-wire cable because it will most likely act as an antenna for noise and WiFi, and mess up the signal. For the GPIO connection, the cable is less crucial, but a twisted pair cable is still preferred.

If you use a standard electret capsule as the microphone, solder the shield connection (the long part on the 3.5 mm plug) to the negative pole of the microphone (connected to the outside of the capsule), and the other wire to any of the two ‘live’ contacts of a 3.5 mm stereo jack plug. It doesn't matter which one: on the simple ‘3D sound’ cards both contacts are the same anyway. Do not short one of those contacts to ground, just leave it open.

## Step 4: coming soon

Work in progress…