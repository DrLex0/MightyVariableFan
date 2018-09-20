# MightyVariableFan

*Variable fan speed workaround for 3D printers based on the MightyBoard, like the FlashForge Creator Pro*<br>
by Alexander Thomas, aka Dr. Lex


## What is it?

This adds *full variable print cooling fan speed* capability to 3D printers based on the MightyBoard design like the FlashForge Creator Pro, by means of a kind of *modem connection* between the printer and a Raspberry Pi.

To make this work, you need some minimal understanding of how to use Linux on a Raspberry Pi and at least basic electronics skills. Being able to solder might not be essential but will make it much easier than trying to find ready-made cables that will fit. While working on the internals of your printer, take the usual precautions for working on delicate electronics: avoid static discharges and short-circuits.

This has so far only been tested with *Slic3r,* and quite likely the post-processing script that generates the beep sequences will only work with Slic3r-generated G-code at this moment. Of course the main reason why I have published this on *GitHub* is to make it easy for anyone to modify the code and submit pull requests to make it work with other slicer programs.

Mind that this is still in an **experimental stage.** However, the current version is pretty usable already. See the *Current Issues* section at the bottom for caveats. This has only been tested with prints from the SD card, because I don't print through USB. I don't expect problems with USB prints though.


### Motivation

The MightyBoard has one annoying limitation, being lack of **PWM ability for the cooling fan.** The fan is connected to the ‘EXTRA’ output on the board and this output is only a binary toggle, meaning the fan can only be either at full throttle or off. This makes it impossible to get optimal results with filaments that require a small or moderate amount of cooling, and there are many other reasons why one would want to vary the speed of the fan. Programs like Slic3r offer advanced fan speed control but it is worthless with only a binary toggle. What we need is a way to control the fan speed from within G-code, and preferably there should be a manual override as well. 

It is possible to recompile the Sailfish firmware with a software PWM implementation on the EXTRA output, but it is only a static setting that cannot be changed mid-print and it still only reacts to on/off commands without a speed value. A simple manual hardware PWM controller is probably a better option because it will at least allow to change the PWM at any time, but it remains cumbersome. I have done this for a few months until I got tired of having to babysit every print. I wanted fully automatic fan control from within the X3G file itself.

Recently a pull request has been proposed for Sailfish to make the software PWM respond to speed arguments in the G-code, but this still has problems: the commands are still executed with horribly bad timing and there is no manual override (even if it would be added, manipulating it mid-print via the LCD menu is very kludgy). Also, GPX needs to be patched and recompiled to output X3G files with fan speed values included, and this will only work if you have somehow managed to let your slicer generate G-code that is suitable for Sailfish *and* includes variable fan speeds.

The ideal way to tackle this problem is to completely decouple fan speed handling from the firmware. The printer's job is to print, not to accurately control fans. The printer should send fan speed commands to a separate device that does the actual fan PWM in a smart way. I found no straightforward way to send commands from within a print to an external device however. Most infuriatingly, the MightyBoard has a second UART1 serial interface that was originally intended exactly to send commands to external devices, but this feature was dropped for reasons unknown. At best the UART1 port can now be used as an alternative to the regular USB serial port. The ideal solution would be to restore the ability to send arbitrary serial commands through UART1 but this would require extensive changes to the Sailfish firmware code, introducing new G-code commands that would need to be supported by GPX as well. This was the last thing I wanted to try, so I looked for alternative solutions first and I found one even if it isn't the most straightforward.
From experiments, the **buzzer** has proven to be a reliable one-way communication channel. Not only does it play tones with accurate timings, it also plays them exactly where they occur inside the print code, unlike the M126 and M127 commands that toggle the EXTRA output at a rather unpredictable time before the command is expected to be executed. I did some experiments and the concept of using the buzzer as a communication channel proved very viable. So in a nutshell, I have used the buzzer to set up a *modem connection* to the external fan controller.


### How It Works

1. A **post-processing script** called *pwm_postprocessor.py* on your local computer takes G-code with M106 fan speed commands as input, and outputs the same code with those commands replaced by M300 beep commands. These play very specific sequences of 3 high-pitched blips using 4 fixed frequencies. This allows to encode 64 levels, which is plenty for controlling a fan. This script also manipulates the speeds to optimize them (see ‘advantages’ below), and can shift the speed changes to obtain very accurate timings. For this to work, you need to let your slicer program generate G-code that contains M106 commands, meaning you will need to output for *RepRap* instead of Sailfish.
2. A **Raspberry Pi** mounted inside the printer runs a Python daemon called *beepdetect.py* that continuously listens to audio input from a simple USB sound card. A **microphone** attached to this card is placed directly above the printer's buzzer. The script uses an FFT to detect the specific frequencies of the blips.
3. When beepdetect.py detects a sequence, it performs an HTTP call to a simple Python-based web server that also runs on the Pi, this is the *pwm\_server.py* script based on CherryPy.
4. The pwm\_server reacts to incoming calls that request a speed change, by manipulating PWM output on a GPIO pin of the Pi. This is done through the RPi.GPIO Python module (which only implements software PWM, but this is good enough for controlling a fan at low frequency).
5. The GPIO pin controls a simple **MOSFET** break-out board that switches the 24V of the main printer PSU. The fan is connected to this MOSFET's output.


### Advantages

This approach, crazy as it may seem, has quite a few advantages:
* No irreversible modifications to your printer are needed. Although you may need to construct the cables for the microphone and GPIO, you do not need to solder anything on the printer itself. The only requirements are glueing in some 3D printed mounts for the Pi and MOSFET, re-wiring the fan, and mounting a power supply for the Pi. These can all fit inside the spacious insides of an FFCP.
* You do not need to recompile anything. If you would have attempted to do all this stuff inside a heavily hacked Sailfish build, next to that build itself you would also have needed to hack and rebuild the GPX program and possibly your slicer, because otherwise you would have no way of getting the fan speed arguments in an X3G file.
* Decoupling the PWM controller from the printer's firmware allows to add advanced features without having to modify the printer or recompiling firmware. For instance, the PWM server controller has a *kickstart* feature to allow the fan to start at very low speeds and to more quickly reach intermediate speeds.
* The post-processing script can optimize fan speeds and timings. It can ensure the fan will have spun up at the right time, and it can gradually ramp up fan speed between the first layers and higher ones. The latter is very important with most cooling duct designs because at lower layers the cooling may otherwise be excessive due to air being forced in between the platform and the extruders.
* Timing of fan speed changes can be very accurate, down to the level of one second. This allows to cool only sharp overhangs or bridges without risk of compromising layer adhesion elsewhere in the print.
* If your Pi is connected to a WiFi network, the fan PWM server can be reached through any portable device like your smartphone, and you can monitor and manipulate fan speed at any time without having to fiddle with the printer's LCD menu.
* The PWM server offers temporary or permanent manual override of fan behavior, useful for experimenting or in case you notice you have misconfigured the cooling for a print that would be a pity to abort. A scale factor can be applied to all incoming requests or the fan can be forced to a fixed speed.
* Fan duty cycles as low as 1% can be used even from standstill, thanks to the kickstart.
* If you already have installed a Raspberry Pi in your printer to have a camera feed or control other parts of the printer, you only need to install a USB sound card with microphone, an extra MOSFET, and some wires.
* The EXTRA output on your printer becomes available for something else should you find a use for it.
* Due to the modular design, if someone finds a less crazy way of making the printer communicate with the Pi, only the beepdetect and pwm_postprocessor scripts need to be updated.


### Disadvantages

* There is only one obvious disadvantage: the printer will be making some extra noises as if R2D2 is trying to subtly get your attention. These blips are very short though, and are played at frequencies the buzzer cannot play loud anyway. Thanks to these two design choices, the blips don't really stand out above the usual printer noises.
* A less likely problem is that if your printer is placed next to a pen with pigs that can squeal *extremely* loud at the exact same frequencies as the blips, detection might fail. Please don't combine 3D printing with pigs. More realistic sources of disturbances are nearby machines that make loud hissing noises, or a loud music system very near to the printer. In my setup however, I couldn't even trigger any false responses with the detector in debug mode without making unreasonably loud noises.

If you would suffer from one or both of these problems, they can be reduced or eliminated in several ways. The first way is to print a sealed adapter for attaching the microphone to the buzzer instead of a half-open one. This both attenuates the blips to a nearly inaudible level and reduces influence of external noises.

If you're not afraid of soldering on your printer's main board, you can omit the microphone and make a direct electrical connection between the buzzer contacts (marked ‘BUZZ BUZZ’) and the USB sound card. You should make the connection through a decoupling capacitor and possibly add a resistor divider to attenuate the signal if necessary. This eliminates the outside noise problem completely. Moreover, you can unsolder the buzzer as well if you want to mute it entirely. This ReadMe won't explain how to do all these things: I assume that if you are prepared to solder on your printer's main board, you also have sufficient knowledge how to make this kind of connection correctly.


### Is this really reliable? It can't be!

Yes it is. I have been using it for many months now and despite the few glitches mentioned in the ‘current issues’, it works incredibly well.


### Can I run something else on the same Raspberry Pi?

It depends. If the other process consumes a lot of CPU or I/O load, then it risks interfering with the real-time operation of the beep detection. So far someone has reported trying to run this system on the same Pi as Octoprint, and this *failed* because the beep detector couldn't reach the required performance. There was no obvious cause (the octoprint process did not cause an excessive load), but it was a pre-made custom Octoprint Raspbian image which might have contained modifications that interfere with the USB sound card. In other words your mileage may vary. Safest is to use a dedicated Raspberry Pi for the fan system, they are cheap anyway…


## Installing

### Step 1: gather the required hardware

You need:
* A **Raspberry Pi** that has sufficient oomph to run the beep detector, and also WiFi (not strictly essential, but recommended). I am not sure whether a Pi 2 suffices, but a 3B certainly does. These things are cheap anyway so if you already have an old Pi inside your printer, this might be a good time to upgrade it. You don't need a huge microSD card, 16GB is sufficient.
* A small **5V power supply** for your Pi. Ideally, it should be small enough to fit inside your printer. I used a simple off-the shelf supply ([this one](https://www.conrad.be/p/raspberry-pi-netvoeding-sp-5c-zwart-raspberry-pi-3-b-1462834) to be exact) that I could tuck under a bundle of wires inside the printer. I connected it to the mains contacts of the printer's PSU so the Pi is toggled together with the rest of the printer. If you have a choice between multiple supplies that fit, pick the one with the most flexible cable and the most compact microUSB plug. This is where you'll have to be a bit creative with parts you can find in local stores. You could take apart a USB power supply to more easily connect it to the mains, or you could plug the supply into half an extension cord like I did. Whatever you do, do not create dubious and dangerous constructions that expose mains voltage.
* A **USB sound card.** It will be much easier if it is as small as possible. I recommend to buy one of those extremely cheap ‘3D sound’ sticks with yellow and green 3.5mm jack. These are rather crappy but very compact, well supported in Linux, and for the purpose of this application they are good enough. The plastic case of these cards is unnecessarily large; to make it much easier to fit inside your printer you can print [this custom case](https://www.thingiverse.com/thing:2822474) if it fits your model of sound card (if not, modifying the 3D model should be easy).
* A tiny **microphone** that can be mounted very close (within 10 mm) to the buzzer. I recommend to build your own microphone with a standard electret capsule (9.7mm diameter), a bit of shielded cable, and a 3.5mm plug. This can be mounted perfectly onto the buzzer with a [3D printed part](https://www.thingiverse.com/thing:2852499).
* A **24V MOSFET** break-out board. There is a very common one for the IRF520, which is very easy to mount inside the printer with yet another [printed part](https://www.thingiverse.com/thing:2852499).
* A **cable** to connect the GPIO pins on the Pi to the MOSFET. It is best to solder your own using low-profile or angled plugs because space may be tight depending on where you will mount the Pi.


### Step 2: prepare the Raspberry Pi

The following assumes you are running the *Raspbian Stretch* or newer operating system on your Pi. Most likely you are, unless you installed it before July 2017. To verify, check whether the file */etc/debian_version* exists on the Pi and starts with at least *9.*<br>
(If you are running something different for a specific reason, chances are you're knowledgeable enough to adapt the installation procedure to your own needs.)

Installation is relatively simple. The required skills are opening a console or terminal on the Pi and inside it, navigating directories and executing commands. If you've attached a monitor and you are inside the desktop environment, you can launch the ‘Terminal’ program. If the Pi has a network connection and you enabled SSH (e.g. using raspi-config), you can also open an SSH connection from another computer. Tutorials about how to do these things should be easy to find.

On the Pi, download and unzip this project's files, or clone the repository with the `git` command. You only need the contents of the *pi_files* folder but simply downloading everything is easier. You can do this in any way and put the files anywhere you want, but you need to know where they are on the filesystem. Most practical is to put them directly in the home folder of the ‘pi’ user (*/home/pi*).

Upgrade the software on your Pi to the latest versions, especially if it was installed long ago and never updated since. You can do this in several ways, for instance use *Update* in `raspi-config`, or: `sudo apt-get update; sudo apt-get upgrade`. This can take a while if you haven't updated in a while. It is recommended to reboot afterwards.

Now make sure the USB audio device is plugged into the Pi. The next step is to run an installer script that puts all files in place. In your terminal or SSH console, go to the *pi_files* folder using the `cd` command and enter these commands:
```
chmod 755 install.sh
sudo ./install.sh
```

By default the PWM server will run on TCP port 8081. If you want to change this because something else is already running on that port, pass the desired port number as argument to the install command (e.g.: `sudo ./install.sh 8082`). The port number must be higher than 1023 because the server does not run with root privileges.

If this script stops with an ‘ERROR’ message and you cannot figure out how to fix it, you can contact me directly or file an issue on GitHub. If it says *“Everything ready, now starting services,”* then you're good to go.

Before mounting the Pi in your printer, you should also configure everything else to your likings, for instance the WiFi connection, SSH access with public key, hostname, … You should also disable everything you don't need, like the graphical X environment unless you really need it for an attached display. Anything that could produce an unpredictable burst of activity should be disabled to avoid interference with the beep detector.<br>
If you are really adamant on getting the best possible performance, you could install a real-time kernel. However, this seems overkill from my experiences so far (also, it won't help if you're simply attempting to run more than the Pi can handle).<br>
How to do those things is outside the scope of this guide. There is plenty of community support available for the Raspberry Pi!

Originally I planned to add some kind of display with buttons or maybe a touchscreen, to be able to view and manipulate the status of the PWM controller. However, the only good display I found was more expensive than the Pi itself and I realized that a smartphone or even a smartwatch also makes a fine wireless touch display, so I didn't bother.


### Step 3: create the required parts

You need two **cables:** one for the microphone, and one for the MOSFET. The microphone cable will have a 3.5 mm stereo plug on one side, and be directly connected to the electret capsule at the other end (unless you want to add some kind of plug, your choice). The MOSFET cable simply needs two header sockets. At one end, it should be a socket with 2 contacts, to be connected to GPIO pins 12 and 14 on the Pi. If you're using the IRF520 break-out board, at the other end the cable needs a 3-contact socket with the middle one left open. GPIO pin 12 on the Pi needs to be connected to the ‘SIG’ pin on this board, and GPIO pin 14 to its ‘GND’ pin; ignore the VCC pin.

If you're going to install the Pi and MOSFET in the same places as I did (see the ‘mount’ section below), the cable going to the microphone must be 31 cm long (3.5mm jack included), and the cable going to the MOSFET should be about 35 cm. If you already mounted the Pi elsewhere, you will have to figure out how long the cables need to be and how to route them. Important: use either some kind of shielded cable for the microphone connection, or a twisted-pair cable. Don't just use any two-wire cable because it will most likely act as an antenna for noise. For the GPIO connection the cable is less crucial, but a twisted pair cable is still preferred.

If you use a standard electret capsule as microphone, solder the ground/shield connection (the long sleeve on the 3.5 mm plug) to the negative pole of the microphone (which connects to the outside of the capsule), and the other wire to any of the two ‘live’ contacts of a 3.5 mm stereo jack plug. It doesn't matter which one: on the simple ‘3D sound’ cards both contacts are the same anyway. Do not short one of those contacts to ground, just leave it open.

![Cables](images/cables_small.jpg)<br>
[Cables (view larger image)](images/cables.jpg)

Next, you need some 3D printed parts to mount the components:
* a mount for the Raspberry Pi. If you mount it in the position I recommend below, [this mount](https://www.thingiverse.com/thing:2852432) should be optimal. Otherwise you will need to design your own or use any other mount that works.
* a mount for the MOSFET. If you use the ubiquitous IRF520 break-out board, [this Thing](https://www.thingiverse.com/thing:2852499) contains a suitable mount that uses the same 2.2mm self-tapping screws as the mount for the Pi.
* An adaptor to mount the microphone onto the buzzer of the printer's main board. [This Thing](https://www.thingiverse.com/thing:2852499) offers two models that fit a standard 9.7 mm electret capsule: a half-open model and a closed one. As described above, the closed model can be used to mute most of the sound of the buzzer in case you don't want to hear the beep sequences and don't care that other beeps like the start-up song will be muted as well. If you want to keep the buzzer sounds as they are, use the half-open model. It is recommended to print this adaptor in a flexible filament to dampen vibrations. If you are using a different model of microphone, again you will need to figure out something on your own, but make sure the mic is as close as possible to the buzzer and there is minimal contact with anything that vibrates.


### Step 4: sanity check

Before mounting everything, it is a good idea to do a sanity check of the whole system outside the printer. The nice thing about the typical IRF520 board is that it has an LED on it that will light up if there is a signal on its GPIO input, even if nothing else is connected. Plug the sound card into the Pi and connect the MOSFET to the GPIO. The GPIO pins #12 (GPIO 18) and #14 (ground) must be connected respectively to the ‘SIG’ and ‘GND’ contacts of the MOSFET board ([here](https://www.raspberrypi.org/documentation/usage/gpio/) is documentation about the GPIO pins).

Next, power up the Pi. After a few seconds you should see the LED on the IRF520 board light up momentarily. If in a browser you enter the Pi's IP address on port 8081 or the custom port number you configured (e.g. `http://192.168.12.34:8081/`), you should see the interface of the PWM server and be able to change the intensity of the LED by choosing different duty cycles.

Next, plug in the microphone, log into the Pi through SSH and run:
```
sudo stoppwmservices
arecord -V mono -D micsnoop -f S16_LE -r 44100 test.wav
```
Say something into the microphone and stop the arecord process with ctrl-C. If you then copy the resulting test.wav file to your computer and play it, you should hear what you have recorded. If not, check the connections and alsamixer settings for the USB sound card (see the ‘calibrating’ section). Also verify that the `hw:1,0` ALSA device is actually the microphone, if not then update the ‘pcm’ value in `/etc/asound.conf`. If the recording is OK but has an awful lot of low-frequency noise, then the cable going to the microphone is poorly shielded and picking up WiFi interference. You might get away with this, but it is better to use a proper shielded cable.


### Step 5: mount and connect the parts

If you haven't yet mounted a Raspberry Pi in your printer and you do not want to optimize its position for attaching a RPi camera with its rather short flatcable, I recommend to mount it in the front side of the bottom chamber next to the power supply, as shown in the overview image. This position ensures the WiFi antenna will have good signal reception without being near any other circuit of the printer, moreover it offers easy routing of the microphone, MOSFET and USB power cables. Of course, feel free to mount the Pi anywhere else, but as always you're on your own then.

![Overview](images/overview_small.jpg)<br>
[Overview (view larger image)](images/overview.jpg)

If you are using [my 3D printed mount](https://www.thingiverse.com/thing:2852432), you can stick it to the printer's housing with epoxy glue. You should first verify there is enough room for cables and plugs before fixing the position of the mount. By all means glue the mount with the Pi inside it and the sound card plugged into the Pi, otherwise there is no guarantee it will fit afterwards. This is also why you shouldn't use cyanoacrylate (super glue): it won't give you time to correct the position if you got it wrong.

How you can mount the 5V power supply, will depend on its shape. In the overview photo you can see that my supply was small enough to simply tuck under a bundle of wires. I connected it to the mains voltage terminals of the main supply through part of an extension cable. This ensured a safe connection, as opposed to my first stupid idea of trying to wrap something around the plugs and covering them with shrink-wrap tubing, which I quickly abandoned when trying it in practice. If your idea seems vaguely dangerous, it most likely is and must not be attempted. In case of doubt, either ask assistance from someone more experienced with electronics or keep the power supply outside your printer and plug it into a regular power socket.

Installing the microphone is straightforward: push it into the mount, and the mount onto the buzzer. Insert the 3.5 mm plug into the input jack of the sound card (yellow on ‘3D sound’). Ensure the plug is perfectly clean to avoid getting random bad contacts. Try to avoid that the microphone cable touches the motherboard fan (unless you disabled this fan because apparently it is not really necessary).

Now you can mount the MOSFET. Connect all the wires before fixing its mount in place. Disconnect the fan wires from the EXTRA output socket, and connect them to the V+ (red) and V- (black) terminals of the board. Next, you need to get 24V from somewhere. You can either use a long wire directly to one of the unused 24V terminals of the power supply, or you can use a short wire to the ‘FAN’ output on the corner of the board, which is hard-wired to the 24V as well. Make sure to get the polarity right! GND is negative (usually black wire), VIN is positive (usually red wire). Don't forget to connect the GPIO as described at the start of this section. Once everything is connected, find a good place to glue the mount.

When you power on the printer and Pi, after a few seconds you should see the fan spinning up for a few seconds. If this doesn't happen, it could mean the pwm_server.py script was not properly installed or one of the electrical connections is incorrect.


### Step 6: calibrating

When everything has been installed and set up, the last step is to calibrate the beep detector. This step is **crucial** because if the system is not properly configured, fan speed changes may be missed and prints may get cooled at too high or low speed or not at all.

Calibration mode performs a few tests regardless of what it detects and it also outputs statistics about any signal frequencies it has detected. To begin with, we'll do a sanity check to see if the script achieves the required **performance.** Before entering calibration mode, you must stop the regular detector: run `sudo stoppwmservices`. Then run `beepdetect.py -c`. It will probably start by spewing many warnings which you may ignore as long as in the end you see “Calibration mode” appear.

For the performance check, just leave it alone for at least 30 seconds and then press *ctrl-C* to stop it. Look for “PERFORMANCE CHECK” in the output. If it reads “Looks OK” then continue with the steps below. If it says “too slow” then try again but let it run for at least a minute. If it keeps on reporting “too slow” or even “WAY TOO SLOW,” then you're in trouble because the script cannot achieve the required speed. Check again whether you really have a recent Raspberry Pi (3B or newer). If you do, maybe something else running on it is consuming way too many CPU or I/O resources. If you cannot shut down any other processes, you only option is to install a separate Pi to run the PWM system.

If the performance test checks out, the next important thing is to configure the **input gain** of the USB sound card. Open a second terminal connection to the Pi and run `alsamixer`. Inside the alsamixer UI, press *F6* to select the USB sound card (most likely the last one in the list). Next, press *F5* to show all controls (or F4 for capture, but this doesn't always work). The cheap ‘3D sound’ card only has one ‘capture’ control, select it with left/right arrow keys. If it doesn't read ‘CAPTURE’ in red below the indicator bar, press the space bar. The gain is adjusted with arrow up and down keys. Start out by cranking up the gain to the maximum (100).

With alsamixer still open, again run `beepdetect.py -c` in the other console. Now load the *BeepCalibration.x3g* file on your printer and print it. It consists of two parts: the first plays a set of beep sequences that uniformly cover all signals, the second part plays noises. With the ALSA gain at maximum, you should see `“WARNING: Clipping detected”` appear. Your goal now is to reduce the gain in alsamixer to the highest level where clipping no longer occurs during the normal beep sequences. Gradually lower the mic gain (down arrow) and play the file again from the start, and repeat until you no longer see any clipping warnings during the first part while the normal beep sequences are playing. It is OK and actually desirable that there is still clipping during the noise in the second part of the BeepCalibration file. If you do not see the clipping warning even with gain at maximum, there may be something wrong with your microphone or audio setup.

Once you have tuned the gain just below the point where no more clipping occurs, exit alsamixer (Esc key) and run `sudo alsactl store`. This will preserve the setting across reboots. Stop the beepdetect process by pressing ctrl-C. If your setup is similar to mine and you tweak the gain like this, then it is likely the system will just work at this point, but it is always a good idea to **verify** this by performing the following steps.

Once the microphone gain has been set, you can fine-tune some **other parameters.** The beep detector and PWM server both read their **configuration** from a *defaults* file located at */etc/default/mightyvariablefan*. If any of the parameters need to be changed, modify this file using your favorite text editor. (Do not edit the scripts themselves, this will make it difficult to upgrade them.)<br>
Restart calibration mode with `beepdetect -c` and play the first part of the BeepCalibration file again, then press ctrl-C and look at the output. It will contain the following sections:
* **PERFORMANCE CHECK:** this has already been covered above. The chunks-per-second measure is only accurate if the script has been running for at least a few dozen seconds, but you may ignore the error about calibration time being too short if you previously verified that performance is OK.
* **SCALING FACTORS:** the responses to the different signal frequencies need different scale factors because the buzzer does not play each tone at the same amplitude and the microphone does not have a flat frequency response either. The script will show a set of suggested scale factors ‘`SIG_SCALEs`’. Physics dictate these values should normally be an increasing series. If this is not the case for the suggested values, something fishy may be going on. If it looks OK but the values deviate considerably from the current `SIG_SCALEs`, then you should copy the suggested lines to the *defaults* file. (These values may vary across different runs. For maximum accuracy, run calibration multiple times and take averages across runs.)
* **suggested sensitivity:** this is the threshold at which a tone is considered a potentially valid signal, obviously this is an extremely important parameter. If the suggested sensitivity deviates a lot from the current value, ensure there is a line in the *defaults* file that specifies “`SENSITIVITY = …`” with the suggested value.
* **DETECTION BINS:** this verifies whether the script is listening to the optimal FFT bins, which are configured as `SIG_BIN1` to `SIG_BIN4`. Again, due to the frequency response of your particular buzzer and microphone, it is possible that the theoretically optimal bins do not match the actual bins.
  * If the script says: *“Bin … looks good”* four times, then you're good to go.
  * If one or more lines state *“Bin x appears to be better than bin y,”* then it may be a good idea to add the suggested `SIG_BIN` definitions to the *defaults* file. You should then re-run calibration, but if the better bin indices keep on drifting away from their theoretically ideal positions, something is definitely wrong.

Once you're done, either reboot the Pi or run `sudo startpwmservices` to resume normal operation. You could then re-play the BeepCalibration file and check in */var/log/beepdetect.log* whether the series of sequences `012, 301, 230, 123` is detected three times (and the fan changes speed accordingly). This is not a fool-proof verification but at least a good indication that the system works correctly.


### Optional step: install custom firmware

This step is not strictly necessary. If you never change bed or extruder temperatures during a print and your extruder PID controllers are well-tuned and the printer never reenters heating mode while printing, then the standard firmware from FlashForge (or whomever your printer's manufacturer is) will do fine.

The reason why you might want to install a new Sailfish build is because recent versions play a tune each time all heaters have reached their target temperature. If this tune starts right in the middle of one of the beep sequences, that particular fan speed change will not be executed which could be disastrous in worst-case situations. An elegant solution is to extend Sailfish with an extra toggle to mute this tune during printing. I have already created a [pull request](https://github.com/jetty840/Sailfish-MightyBoardFirmware/pull/201) exactly for this. It may take a while before it is accepted and even then there is no guarantee that FlashForge (or your particular manufacturer) will pick this up anytime soon. For this reason I have created my own Sailfish build with these changes included.

You can [download this custom firmware build from my Sailfish branch](https://github.com/DrLex0/Sailfish-MightyBoardFirmware/releases/tag/20180505) (available for all other MightyBoard-based printers supported by Sailfish as well). It also includes other recent improvements that probably are not yet incorporated in official builds available from printer manufacturers, for that reason alone it could be worth upgrading to it.


## Using

Once you have the setup running, all that is left to be done is to generate your print files such that they contain beep sequences instead of the old M126 and M127 commands. The problem is that slicer programs are aware of the limited fan capability of MightyBoard-based printers, hence they only output those commands without any speed information if you ask them to output Sailfish-compatible G-code. The solution is to change your slicer profile to output G-code for *RepRap* instead which contains  M106 and M107 commands with speed arguments. To convert these commands to beep sequences, the G-code must be run through the *pwm_postprocessor.py* script. This script has so far only been tested with G-code produced by *Slic3r.* It might work with other slicers, but most likely the script will require a few modifications. (If you can make those modifications yourself, a pull request to merge the improvements would be welcome. If not, you can also send me a few G-code files and I can see how to make the script work for them.)

You need *Python 3.5* or newer on the machine where you'll be running the postprocessor script. Inside the script, you must make one important change: set `END_MARKER` to a line that indicates the print has ended. If you are using your own snippet of end G-code, just ensure it starts with a unique comment line and copy that exact line into the script. There are other adjustments you can make, these can also be passed as command-line arguments:

* `RAMP_UP_ZMAX` is the zone above the build plate within which fan speeds will be gradually scaled starting from a scale factor `RAMP_UP_SCALE0` at *Z* = 0, to 100% at *Z* = `RAMP_UP_ZMAX`. The reason why this is recommended, is because airflow from the cooling duct bounces off the bed at the lower layers, and it is also being forced in between the bed and the extruders. This causes more cooling than expected, and it can also cause extruder temperature to drop if the fan suddenly activates at high speed. The optimal values of these parameters will differ depending on what kind of cooling duct design you use. You will have to experiment. (Note: this feature works better than the similar one in Cura because it uses a fixed Z height instead of counting layers that might have different heights.)
* `LEAD_TIME` is the number of seconds by which beep sequences should be moved forward in time. A sequence takes about 0.7 seconds to be played and detected, and the time needed to spin up the fan must also be considered, hence a value around 1 second should be reasonable. In my case 1.2 seconds seems optimal. Mind that this is done on a best-effort basis. The time will not always be exact because the script doesn't consider acceleration, and granularity depends on duration of print moves. If the last move before an original M106 command takes more than twice `LEAD_TIME`, the script will not be able to anticipate the beep sequence. The script can split up long moves to obtain a good lead time, this is the `--allow_split` option which is off by default. It is possible that enabling this option can cause visible artefacts, so there is a bit of a trade-off between cooling performance and surface quality.
* `FEED_FACTOR` and `FEED_LIMIT_Z` are values specific to the FlashForge Creator Pro and it is unlikely you need to change them, only do so if you know what you are doing.

Once the script has been configured, you can either manually run it on every G-code file you want to print (run with `-h` for more information), or you can somehow automate it inside your workflow. How you can do the latter, depends on your slicing program. In case you use Slic3r with [my (DrLex) configuration](https://www.thingiverse.com/thing:2367215), the latest version of the `make_fcp_x3g` script can invoke the PWM post-processing script as well.

**Note:** even though you should reconfigure your slicer to output RepRap-flavor G-code, the GPX program to convert G-code into X3G must still be configured to output code for your specific printer model (e.g. `-m fcp` for the FlashForge Creator Pro)!

### Web interface
During a print you can observe the current PWM duty cycle and manipulate it if you wish, by opening the PWM web server interface on a computer, tablet or smartphone. The default address is `http://your.RPi.address:8081/`. (Port number may be different if you changed it.) The interface is currently still very crude but should be self-explanatory. Next to setting some fixed PWM preset values, you can also increase or decrease a **scale factor** in 5% steps. This factor will be applied to every incoming PWM value, which can be useful if you notice you have configured too low or high fan speeds for a print.

The interface also allows to *shut down the Pi cleanly.* This is not terribly important but recommended if the Pi's power supply is behind the same switch as the printer's. It is better to perform a clean shutdown than simply pulling the power. Wait at least 15 seconds for the Pi to shut down before disconnecting the mains.

If you want to make the interface accessible from an outside network where it is undesirable that anyone can manipulate the fan controller, you can set up simple **authentication** with a username and password. The basic status page can always be viewed without logging in but if both a username and password have been configured, login credentials will be asked when trying to access the interface page. To do this, edit the file */etc/default/mightyvariablefan* and add these lines (obviously use a nontrivial username and password):
```
PWM_USER = "someName"
PWM_PASS = "somePassword"
```
Reboot the Pi to apply these changes. There is a ‘logout’ link but this only works in certain browsers (like Chrome) due to limitations of the digest-based authentication method. In other browsers the only way to log out is to quit and reopen the browser. Mind that the server uses plain HTTP which means login credentials could theoretically be sniffed from network traffic. If you want to prevent this, I would advise to not rely on plain port forwarding to make this application available over the internet but instead place it behind a HTTPS reverse proxy like [Nginx](https://www.nginx.com/) (you can also use this to add extra protection like rate limiting).

### Tools and tweaks
In the Tools folder there are files `PWMFanOff.x3g` and `PWMFanMax.x3g` that play the sequences for disabling the fan and setting it to 100%. These are useful for several things:
* if you abort a print, the fan will remain at its last speed. You can use the ‘off’ file to stop the fan if you don't have direct access to the PWM web interface.
* as a quick routine test of the system after booting up your printer. This is especially recommended if you're about to do a print where correct cooling is crucial. So far I've had one occasion where the microphone didn't pick up any sound after I had taken the system apart and reassembled it (most likely a bad contact with the 3.5 mm plug, it was fixed after I had cleaned the plug and reinserted it).

Last but not least, if you previously neglected fan speed values in your slicer profiles (as you should have), now is the time to go through them again and try to enter sensible values. Optimal values will differ between each filament and also depend on what kind of extruder, nozzle, and cooling duct you are using. Be prepared to experiment and tweak! Also be prepared to be amazed at how much of a quality improvement proper cooling can provide.

This system is compatible with version 0.7 or newer of my [dualstrusion post-processing script](https://www.dr-lex.be/info-stuff/print3d-dualstrusion.html) to improve dual extrusion prints on the FFCP.


## Technical details

(This is only for interested nerds. If you just want to use the system, skip this section.)

Look in the source code comments for most of the particularities of how the system works.

The reason why we're recording from an ALSA *dsnoop* slave device is that this eliminates the issue of clock skew between the sound card and the host. When recording directly from the raw device, eventually enough drift between the clocks will build up such that a buffer overflow (if the sound card runs faster) or underrun (slower) occurs. This will lead respectively to either omission of a chunk of data, or repeating the same chunk twice. Both are obviously bad for a detection system which relies on short beep sounds. The dsnoop device does continuous resampling instead of abrupt corrections. The resampling quality is probably not audiophile-grade, but the FFT couldn't care less as long as there is a continuous flow of samples without gaps or repetitions.

The IRF520 MOSFET is theoretically capable of drawing up to 9 A of current. However, with the low gate voltage provided by the Pi, the maximum current will be much lower. For switching the cooling fan it is perfectly adequate but keep this in mind should you would want to switch heavier loads.


## Current Issues

There still is a very tiny risk that a detection may be missed (judging from my tests, the risk is perhaps 1 in 1000). I plan to redesign the detection logic to be more robust. I believe though that the printer may very rarely botch up playback of an M300 command as well. To avoid that a missed detection at an unfortunate moment could ruin a print, I plan to modify the post-processing script to repeat sequences at important locations.

If you would be running your own custom build based on the very latest Sailfish master branch, you will run into a problem caused by the ‘hammerfix’ commits by *dbavatar* from around July 2016: M300 commands (to play beeps) cause a significant pause in the print, and the sequences are played with sloppier timings that will cause beepdetect.py to miss them. I have made a [pull request](https://github.com/jetty840/Sailfish-MightyBoardFirmware/pull/202) that nearly eliminates the pauses by improving SD card reading efficiency. The sloppy beep playback is still a problem but I plan to rewrite the detection algorithm in beepdetect.py anyway such that it is more robust. For those who do want to try the hammer fix, ask and I'll make a build that includes it. You should first try [my other custom build](https://github.com/DrLex0/Sailfish-MightyBoardFirmware/releases/tag/20180505) however, perhaps the included SD card reading improvement will already provide the performance boost you're looking for.


### Disclaimer

This software, instruction guide, and 3D models, are provided as-is with no guarantees of any kind. Performing this modification to your printer is entirely at your own risk. The author(s) claim no responsibility for any possible damage or harm caused by attempting to follow these instructions or using any of the provided resources.
