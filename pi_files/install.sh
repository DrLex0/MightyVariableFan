#!/bin/bash
# This is the installer script to deploy MightyVariableFan scripts and configuration on a Raspberry Pi.
# This needs to be executed with root permissions (sudo).
# Optional argument: custom port number for the PWM server.
#
# Alexander Thomas a.k.a. DrLex, https://www.dr-lex.be/
# Released under Creative Commons Attribution 4.0 International license.


# These packages are needed for the beepdetect and pwm_server scripts
REQUIRED_PACKAGES=(python3-scipy python3-pyaudio python3-cherrypy3 python3-requests-futures python3-rpi.gpio)

# Everything that belongs in /usr/local/bin
BINARIES=(beepdetect.py pwm_server.py shutdownpi startpwmservices stoppwmservices)

DEFAULTS='/etc/default/mightyvariablefan'
START_SCRIPT='/usr/local/bin/startpwmservices'
STOP_SCRIPT='/usr/local/bin/stoppwmservices'
BEEPDETECT='/usr/local/bin/beepdetect.py'


fatal() {
	echo -e "\033[0;31mERROR: $1\033[0m" 1>&2
	exit 1
}

write_defaults()
{
	# Ensure a key=value pair is in the defaults file
	local key=$1
	local value=$2

	if [ ! -e $DEFAULTS ]; then
		echo "# Enter custom parameters for the MightyVariableFan system in this file" > $DEFAULTS
		chown pi $DEFAULTS  # more convenient
	fi
	if grep -q "^${key} *=" $DEFAULTS; then
		sed -i "s/^${key} *=.*$/${key} = ${value}/" $DEFAULTS
	else
		# Ensure the file ends in a newline
		sed -i -e '$a\' $DEFAULTS
		# workaround for broken syntax highlighting due to ugly sed syntax'
		echo -e "\n${key} = ${value}" >> $DEFAULTS
	fi
}


# Echoing $@ would be problematic if arguments would need to be quoted,
# luckily this is not the case here.
[[ $EUID -ne 0 ]] && fatal "this script requires root privileges. Try 'sudo $0 $@' instead."


if [ -n "$1" ]; then
	if ! [[ "$1" =~ ^[0-9]+$ ]]; then
		fatal "Error: optional argument must be a port number, like 8081"
	fi
	SERVER_PORT=$1
fi


echo "=== Stopping any running instances of the daemons..."
[ -x $STOP_SCRIPT ] && $STOP_SCRIPT


echo "=== Installing required packages..."
apt-get install ${REQUIRED_PACKAGES[*]} || fatal "packages could not be installed."


echo "=== Installing scripts in /usr/local/bin..."
chmod 755 ${BINARIES[*]}
mkdir -p /usr/local/bin
cp ${BINARIES[*]} /usr/local/bin/


echo "=== Installing static web server files..."
chown -R pi:pi pwm_server
chmod -R go-w,a-x+X pwm_server  # sanitize permissions
cp -pr pwm_server /home/pi/


echo "=== Determining ALSA device for the microphone..."
# In my setup the device is hw:1,0.
alsa_device=$(arecord -l | grep -m 1 -oE '^card [0-9]: .*device [0-9]+' | sed -E 's/card ([0-9]+): .*device ([0-9]+)/hw:\1,\2/')
[ -n "${alsa_device}" ] || fatal "Could not determine sound card ID. Make sure the USB sound device is plugged in and try again."
echo "First audio input device found is '${alsa_device}'. Assuming this is the correct one for setting up asound.conf."
sed -i -E "s/pcm \S+\s+# must match the PCM.*/pcm \"${alsa_device}\"  # must match the PCM the microphone is attached to/" asound.conf

echo "=== Installing asound.conf..."
if [ -e /etc/asound.conf ]; then
	echo "Moving existing /etc/asound.conf to /etc/asound.conf.old. If you've modified this file yourself, you may want to merge it with the new file."
	mv /etc/asound.conf /etc/asound.conf.old
fi
chmod 644 asound.conf
cp asound.conf /etc/


echo "=== Ensuring the services are started in rc.local..."
if ! grep -q "^${START_SCRIPT}" /etc/rc.local; then
	sed -i.old "s|^exit 0|${START_SCRIPT}\n\nexit 0|" /etc/rc.local
fi


echo "=== Restarting alsa-utils to reload config..."
/etc/init.d/alsa-utils restart || fatal "failed to restart alsa-utils."


echo "=== Ensuring beepdetect.py is invoked with the correct ID for the 'micsnoop' device..."
# In my setup the ID is 4.
pcm_id=$($BEEPDETECT -L 2>/dev/null | grep -o 'Input Device id .*: micsnoop' | awk '{print substr($4, 1, length($4)-1)}')
[ -n "${pcm_id}" ] || fatal "could not determine microphone device ID."

echo "Setting microphone device ID to ${pcm_id}"
write_defaults 'AUDIO_DEVICE' $pcm_id


if [ -n "${SERVER_PORT}" ]; then
	echo "=== Setting custom server port ${SERVER_PORT}..."
	write_defaults 'PWM_SERVER_PORT' $SERVER_PORT
fi


echo "=== Everything ready, now starting services..."
$START_SCRIPT > /dev/null  # Mute the nohup output
