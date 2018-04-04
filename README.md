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


# Installing

## Step 1: gather the required hardware

You need:
* A **Raspberry Pi** that has sufficient oomph to run the beep detector. I am not sure whether a Pi 2 suffices, but a 3B certainly does. These things are not expensive anyway, so if you already have an old Pi inside your printer, this might be a good time to upgrade it. There is a pretty good place to mount the Pi in the underside of the printer, by using 3D printed mounts that can be glued in place, I will publish these soon (**TODO**).
* A small **5V power supply** for your Pi. Ideally, it should be small enough to fit inside your printer. I used a simple off-the shelf supply ([this one](https://www.conrad.be/p/raspberry-pi-netvoeding-sp-5c-zwart-raspberry-pi-3-b-1462834) to be exact) that I could tuck under a bundle of wires inside the printer. I connected it to the mains contacts of the printer's PSU, so the Pi is toggled together with the rest of the printer. This is where you'll have to be a bit creative with the parts you can find in local stores. You could take apart a USB power supply to more easily connect it to the mains, or you could plug the supply into half an extension cord, like I did. Whatever you do, do not create dubious and dangerous constructions that expose mains voltage. If you have a choice between multiple supplies that fit, pick the one with the most flexible cable and the smallest microUSB plug.
* A **USB sound card.** It will be much easier if it is as small as possible. I recommend to buy one of those extremely cheap ‘3D sound’ cards with a yellow and green 3.5mm jack. These are rather crappy, but are very compact, well supported in Linux, and for the purpose of this application they are good enough. The plastic case of these cards is unnecessarily large; to make it much easier to fit inside your printer, you can print [this custom case](https://www.thingiverse.com/thing:2822474) if it fits your model of sound card (if not, modifying the model should be easy).
* A tiny **microphone** that can be mounted very close (within 10 mm) to the buzzer. I recommend to build your own microphone with a standard electret capsule (9.7mm diameter), a bit of shielded cable, and a 3.5mm plug. This can be mounted perfectly onto the buzzer with a 3D printed part I will publish shortly (**TODO**).
* A **24V MOSFET** break-out board. There is a very common one for the IRF520, which is very easy to mount inside the printer with yet another printed part I will publish shortly (**TODO**).
* A **cable** to connect the GPIO pins on the Pi to the MOSFET. It is best to solder your own, using low-profile or angled plugs, because space may be tight depending on where you will mount the Pi.


## Step 2: prepare the Raspberry Pi

On your Pi, assuming you are running Raspbian Stretch or newer, you need to ensure the following Debian packages are installed. You can run `sudo aptitude` to install them in a console UI, or simply do `sudo apt-get install` followed by a space-separated list of the package names:
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

After logging in to the Pi and placing those files (and only those) in a folder ‘stuff’, you can get them in the right place with these commands:
```
cd stuff
chmod a+x *
sudo mv * /usr/local/bin/
```
You also need to copy the *pwm_server* directory (with the CSS file inside it) into `/home/pi/`.

Finally, add the following line before the “`exit 0`” line in `/etc/rc.local`. You need root permissions for this, for instance use: `sudo nano /etc/rc.local` or: `sudo vim.tiny /etc/rc.local` depending on your preferred editor.
```
/usr/local/bin/startpwmservices
```
Before mounting the Pi in your printer, you should also configure everything else to your likings, for instance configure the WiFi connection, SSH access with public key, change the hostname, … You should look up the relevant how-tos for this yourself.


## Step 3: create the required parts

You need two **cables:** one for the microphone, and one for the MOSFET. The microphone cable will have a 3.5 mm stereo plug on one side, and be directly connected to the electret capsule at the other end (unless you want to add some kind of plug, your choice). The MOSFET cable simply needs two header sockets. At one end, it should be a socket with 2 contacts, and at the other end 3 contacts, with the middle contact left open.

If you're going to install the Pi and MOSFET in the same places as I did (see the ‘mount’ section below), the cable going to the microphone must be 31 cm long (3.5mm jack included), and the cable going to the MOSFET should be about 35 cm. If you already mounted the Pi elsewhere, you will have to figure out how long the cables need to be and how to route them. The microphone must reach  Important: use either some kind of shielded cable for the microphone connection, or a twisted-pair cable. Don't just use any two-wire cable because it will most likely act as an antenna for noise. For the GPIO connection the cable is less crucial, but a twisted pair cable is still preferred.

If you use a standard electret capsule as microphone, solder the shield connection (the long part on the 3.5 mm plug) to the negative pole of the microphone (connected to the outside of the capsule), and the other wire to any of the two ‘live’ contacts of a 3.5 mm stereo jack plug. It doesn't matter which one: on the simple ‘3D sound’ cards both contacts are the same anyway. Do not short one of those contacts to ground, just leave it open.

Next, you need some 3D printed parts to mount the components:
* a mount for the Raspberry Pi. If you mount it in the position I recommend, [this mount](https://TODO.todo) should be optimal. Otherwise, you will need to design your own, or use any other mount that works.
* a mount for the MOSFET. If you use the ubiquitous IRF520 break-out board, [this Thing](https://TODO.todo) contains a suitable mount that uses 2.2mm self-tapping screws.
* An adaptor to mount the microphone onto the buzzer of the printer's main board. If you're using a standard 9.7 mm electret capsule, [this Thing](https://TODO.todo) offers two models: a half-open model and a closed one. As described above, the closed model can be used to mute most of the sound of the buzzer, in case you don't want to hear the beep sequences and don't care that other beeps like the start-up song will be muted as well. If you want to keep the buzzer sounds as they are, use the half-open model. It is recommended to print this adaptor in a flexible filament, to dampen vibrations. If you are using a different model of microphone, again you will need to figure out something on your own, but make sure the microphone is as close as possible to the buzzer and there is minimal contact with anything that vibrates.


## Step 4: sanity check

Before mounting everything, it is a good idea to do a sanity check of the whole system outside the printer. The nice thing about the typical IRF520 board, is that it has an LED on it that will light up if there is a signal on its GPIO input, even if nothing else is connected. Plug the sound card into the Pi and connect the MOSFET to the GPIO. The GPIO pins #12 (GPIO 18) and #14 (ground) must be connected to respectively the ‘SIG’ and ‘GND’ contacts of the MOSFET board ([here](https://www.raspberrypi.org/documentation/usage/gpio/) is documentation about the GPIO pins). Next, power up the Pi. After a few seconds, you should see the LED on the IRF520 board light up momentarily. If you connect to the Pi's IP address on port 8080, you should see the interface of the PWM server, and be able to change the intensity of the LED by choosing different duty cycles. Next, log into the Pi through SSH and run `sudo stoppwmservices`, then: `arecord arecord -V mono -D hw:1,0 -f S16_LE -r 44100 test.wav`. Then say something into the microphone and stop the arecord process with ctrl-C. If you then open the resulting test.wav file on your computer, you should hear what you have recorded. If not, check the connections and alsamixer settings for the USB sound card (see the ‘calibrating’ section). If the recording has an awful lot of very low-frequency noise, then the cable going to the microphone is poorly shielded and picking up WiFi interference. (You might get away with this, but it is better to look for a better cable.)


## Step 5: mount and connect the parts

If you haven't yet mounted a Raspberry Pi in your printer, and you do not want to optimize its position for attaching the RPi camera with its rather short flatcable, I recommend to mount it in the front side of the bottom chamber next to the power supply, as shown in the overview image. This position ensures the WiFi antenna will have good signal reception and not be near any other circuit of the printer, moreover it offers easy routing of the microphone, MOSFET and USB power cables. Of course, feel free to mount the Pi anywhere else, but as always you're on your own then.

![Overview](images/overview.jpg | width=640)

If you are using the 3D printed mount, you can stick it to the printer's housing with epoxy glue. You should first make sure there is enough room for cables and plugs, before fixing the position of the mount. By all means you should glue the mount with the Pi inside it and the sound card plugged into the Pi, otherwise there is no guarantee it will fit. This is also why you shouldn't use cyanoacrylate (super glue): it won't give you time to correct the position if you got it wrong.

How you can mount the 5V power supply, will depend on its shape. In the overview photo you can see that my supply was small enough to simply tuck under a bundle of wires. I connected it to the mains voltage pins of the main supply through part of an extension cable. This ensured a safe connection, as opposed to my first stupid idea of trying to wrap something around the plugs and covering them with shrink-wrap tubing, which I quickly abandoned when trying it in practice. If your idea seems vaguely dangerous, it most likely is and must not be attempted. In case of doubt, either ask assistance from someone more experienced with electronics, or keep the power supply outside your printer and plug it into a regular power socket.

Now you can mount the MOSFET. Before fixing its mount in place, connect all the wires. Disconnect the fan wires from the EXTRA output socket, and connect them to the V+ (red) and V- (black) terminals of the board. Next, you need to get 24V from somewhere. You can either use a long wire directly to one of the unused 24V terminals of the power supply, or you can use a short wire to the ‘FAN’ output on the corner of the board, which is hard-wired to the 24V as well. Make sure to get the polarity right! GND is negative (usually black wire), VIN is positive (usually red wire). Don't forget to connect the GPIO as described at the start of this section. Once everything is connected, find a good place to glue the mount.



## Step 6: calibrating

When everything has been installed and set up, the last step is to calibrate the beep detector. This is crucial, because if it is misconfigured, some fan speed changes may be missed and prints may get cooled at too high or low speed.

When you power on the printer and Pi, after a few seconds you should see the fan spinning up for a few seconds. If this doesn't happen, it could mean the pwm_server.py script was not properly installed, or one of the electrical connections is incorrect.

The most important thing to configure is the *input gain* of the USB sound card. You can do this with `alsamixer`. Inside the alsamixer UI, press *F6* to select the USB sound card (most likely the last one in the list). Next, press *F5* to show all controls. The cheap ‘3D sound’ card only has one ‘capture’ control, select it with left/right arrow keys. If it doesn't read ‘CAPTURE’ in red below the indicator bar, press the space bar. The gain is adjusted with arrow up and down keys. Start out by cranking up the gain to the maximum (100).

With alsamixer still open, run `sudo stoppwmservices` in another console. This stops normal operation and allows to run the detector in calibration mode: run `beepdetect.py -c`. It will probably spew many warnings which you may ignore, as long as in the end you see “Calibration mode” appear. Now, load the BeepCalibration.x3g file on your printer and print it. It will play a set of beep sequences that cover all possible signals.

You should see “WARNING: clipping detected” appear. Now, reduce the gain in alsamixer (down arrow) just to the point where these warnings no longer appear while the beep sequences are playing. It is OK if the warnings still show up when other sounds are being played, like the boot-up chime or the noise at the end of the BeepCalibration file. If you do it this way, then most likely you won't need to change the SENSITIVITY value in beepdetect.py. If you do not see the clipping warning even with gain at maximum, there may be something wrong with your microphone or audio setup.

Once you have tuned the gain just below the point where no more clipping occurs, exit alsamixer and run `sudo alsactl store`. This will preserve the setting across reboots. Stop the beepdetect process by pressing ctrl-C.

The next thing to check, is whether the detector is listening to the optimal frequencies. This step is optional and should not be needed unless there are considerable deviations between your printer and microphone compared to mine. However, it is an easy check anyway. Run `beepdetect -c` again. Then, play the BeepCalibration file until the end of the beep sequences, then press ctrl-C again. Now take a look at the final lines in the output:
* if they all state “Bin … looks good”, then you're good to go.
* If one or more lines state “Bin *x* appears to be better than bin *y*,” then it may be a good idea to edit the `SIG_BINS` values in beepdetect.py, and replace value(s) *y* with value(s) *x*. You could then repeat the calibration, but if the better bin indices keep on drifting away from their theoretically ideal positions, something is definitely wrong.

Once you're done, either reboot the Pi or run `sudo startpwmservices` to resume normal operation.



# Using

*TODO!*



## Disclaimer

This software, instruction guide, and 3D models, are provided as-is with no guarantees of any kind. Performing this modification to your printer is entirely at your own risk. The author(s) claim no responsibility for any possible damage or harm caused by attempting to follow these instructions or using any of the provided resources.
