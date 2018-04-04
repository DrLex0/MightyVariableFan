; Beep calibration file generated with 'pwm_postprocessor.py -c'.
; Convert this into X3G using the GPX program with "-m fcp" argument.
; This file is made for the standard set of signal frequencies: 5988, 6452, 6944, 7407 Hz.
; See the README for instructions.
T0
G21; mm
G90; absolute positioning
M320; acceleration
G92 X118 Y72.5 Z0 E0 B0; reference point

M71; Press OK to start   beep calibration    sequences

; This message isn't here just for fun. An M71 must be followed by an M70, or else the prompt
; sticks (at least in my firmware), and can make the printer unusable until power cycled!
M70 P1; Go go go!
M73 P0; progress

M300 S0 P200; sequence 0-1-2
M300 S5988 P20
M300 S0 P100
M300 S6452 P20
M300 S0 P100
M300 S6944 P20
M300 S0 P200; end sequence

M73 P8; progress
G4 P1500; wait

M300 S0 P200; sequence 3-0-1
M300 S7407 P20
M300 S0 P100
M300 S5988 P20
M300 S0 P100
M300 S6452 P20
M300 S0 P200; end sequence

M73 P17; progress
G4 P1500; wait

M300 S0 P200; sequence 2-3-0
M300 S6944 P20
M300 S0 P100
M300 S7407 P20
M300 S0 P100
M300 S5988 P20
M300 S0 P200; end sequence

M73 P25; progress
G4 P1500; wait

M300 S0 P200; sequence 1-2-3
M300 S6452 P20
M300 S0 P100
M300 S6944 P20
M300 S0 P100
M300 S7407 P20
M300 S0 P200; end sequence

M73 P33; progress
G4 P1500; wait

M300 S0 P200; sequence 0-1-2
M300 S5988 P20
M300 S0 P100
M300 S6452 P20
M300 S0 P100
M300 S6944 P20
M300 S0 P200; end sequence

M73 P42; progress
G4 P1500; wait

M300 S0 P200; sequence 3-0-1
M300 S7407 P20
M300 S0 P100
M300 S5988 P20
M300 S0 P100
M300 S6452 P20
M300 S0 P200; end sequence

M73 P50; progress
G4 P1500; wait

M300 S0 P200; sequence 2-3-0
M300 S6944 P20
M300 S0 P100
M300 S7407 P20
M300 S0 P100
M300 S5988 P20
M300 S0 P200; end sequence

M73 P58; progress
G4 P1500; wait

M300 S0 P200; sequence 1-2-3
M300 S6452 P20
M300 S0 P100
M300 S6944 P20
M300 S0 P100
M300 S7407 P20
M300 S0 P200; end sequence

M73 P67; progress
G4 P1500; wait

M300 S0 P200; sequence 0-1-2
M300 S5988 P20
M300 S0 P100
M300 S6452 P20
M300 S0 P100
M300 S6944 P20
M300 S0 P200; end sequence

M73 P75; progress
G4 P1500; wait

M300 S0 P200; sequence 3-0-1
M300 S7407 P20
M300 S0 P100
M300 S5988 P20
M300 S0 P100
M300 S6452 P20
M300 S0 P200; end sequence

M73 P83; progress
G4 P1500; wait

M300 S0 P200; sequence 2-3-0
M300 S6944 P20
M300 S0 P100
M300 S7407 P20
M300 S0 P100
M300 S5988 P20
M300 S0 P200; end sequence

M73 P92; progress
G4 P1500; wait

M300 S0 P200; sequence 1-2-3
M300 S6452 P20
M300 S0 P100
M300 S6944 P20
M300 S0 P100
M300 S7407 P20
M300 S0 P200; end sequence

M71; Make sure to stop   beepdetect.py now.  Then press OK to end
; See comment at start
M70 P2; Making some noise...

; In case the PWM server is still active
M300 S0 P200; sequence 0-0-0 (off)
M300 S5988 P20; beep
M300 S0 P100; rest
M300 S5988 P20; beep
M300 S0 P100; rest
M300 S5988 P20; beep
M300 S0 P200; end sequence

G4 P0; flush pipeline
M73 P99; progress
; A terribly annoying property of Sailfish is that it will send the printer back to the main menu
; as soon as the last bit of the file has been read into the buffer, even if those commands still
; need to be executed. Therefore stuff the buffer with useless junk to prevent this possibly
; confusing situation.
; The following small moves are always safe unless in the unlikely situation that the carriage has
; been pushed all the way to the left, but even then it won't do any real harm.
G1 F3000
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X115
G1 X114.5
G1 X118
; This noise should produce clipping if the input level is well-configured.
M300 S2000 P100; beep
M300 S1000 P50; beep
M300 S950 P50; beep
M300 S900 P50; beep
M300 S850 P50; beep
M300 S800 P50; beep
M300 S750 P50; beep
M300 S700 P50; beep
M300 S650 P50; beep
M300 S600 P50; beep
M300 S550 P50; beep
M300 S500 P50; beep
M300 S450 P50; beep
M300 S400 P50; beep
M300 S350 P50; beep
M300 S300 P50; beep
M300 S250 P50; beep
M300 S200 P50; beep
M300 S150 P50; beep
M300 S100 P50; beep
M300 S90 P100; beep
M300 S100 P50; beep
M300 S150 P50; beep
M300 S200 P50; beep
M300 S250 P50; beep

M73 P100; end build progress
M18; disable steppers
G4 P0; flush pipeline
