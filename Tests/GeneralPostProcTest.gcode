; This is a basic test file for the pwm_postprocessor script, presenting it with situations that
; range between normal and excessive.
; This is not intended to be printed, only to compare input and processed output.

M107

;- - - Fake start G-code - - -
T0
G21; mm
G90; absolute positioning
M320; acceleration
M83; use relative E coordinates
G162 X Y F8400; home XY axes maximum
G92 X118 Y72.5 Z10 E0 B0; set (rough) reference point (also set E and B to make GPX happy).
G1 X110 Y60 F1000 ; initialize acceleration
M73 P1 ;@body (notify GPX body has started)
;- - - End fake start G-code - - -

G1 Z10.0 F1200 ; Ensure the script has a Z value to work with, and set it above RAMP_UP_ZMAX.
G1 X100 Y60 F8400
G1 X0 Y0 F8400

; Use a feedrate that results in 1 second per 10 mm
G1 F600
G1 X10 Y0 E1.0
; Start with normal situation, fan commands nicely spaced apart.
M106 S127
G1 F600 ; Slic3r also repeats the feedrate after changing fan speed
G1 X20 Y0 E1.0
G1 X10 Y0 E1.0
M106 S20
G1 F600
G1 X20 Y0 E1.0
G1 X40 Y0 E1.0
M106 S0
G1 X20 Y0 E1.0


; Now do many high-low commands in close succession. This should result in the fan remaining at
; high the during the whole period, because it is more important to turn on the fan at the exact
; right moments than to turn it off.
G1 X0 Y0 E1.0

M106 S127
G1 F600
G1 X1.0 Y0 E0.1
M106 S25
G1 F600
G1 X3.0 Y0 E0.3
M106 S127
G1 X4.0 Y0 E0.1
M106 S25
G1 X5.0 Y0 E0.1
G1 X6.0 Y0 E0.1
G1 X7.0 Y0 E0.1
M106 S127
G1 X10.0 Y0 E0.3
G1 X20.0 Y0 E2.0
G1 X40.0 Y0 E2.0
G1 X80.0 Y0 E2.0
G1 X100.0 Y0 E2.0
M106 S25

; And now just way too many different commands in quick succession. This should not happen during
; any sensible print, as the worst case will normally be quick toggling between high and slow (off)
; speeds as shown above. However, we must be able to handle this 'crisis' situation in such a way
; that the final state is consistent, even though the intermediate state may be a mess with badly
; timed and missing commands.
G1 F600
G1 X13.0 Y0 E3.14159
G1 X12.0 Y0 E0.1
G1 X11.0 Y0 E0.1
G1 X10.0 Y0 E0.1
G1 X0 Y0 E2.71828
M106 S127
G1 F600
G1 X1.0 Y0 E0.1
M106 S128
G1 X2.0 Y0 E0.1
M106 S129
G1 X3.0 Y0 E0.1
M106 S130
G1 X4.0 Y0 E0.1
M106 S29
G1 X5.0 Y0 E0.1
M106 S144
G1 X6.0 Y0 E0.1
M106 S64
G1 X7.0 Y0 E0.1
M106 S72
G1 X8.0 Y0 E0.1
G1 X9.0 Y0 E0.1
M106 S32
G1 X10.0 Y0 E0.1
G1 X12.0 Y0 E0.2
G1 X14.0 Y0 E0.2
G1 X15.0 Y0 E0.1
G1 X20.0 Y0 E0.111
G1 X30.0 Y0 E1
G1 X40.0 Y0 E2
G1 X10.0 Y0 E3


; Some dummy commands to let everything settle at the correct speed before the final
; 'off' command, and test the disabling of lead time for the final M107.
M70 P1; Blargh
G1 F1200
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1
G1 X0
G1 X1

M107
; Some lines must be allowed here
; Yadda yadda

;- - - Custom finish printing G-code for FlashForge Creator Pro - - -
M73 P100; end build progress
M18; disable steppers
G4 P0; flush pipeline
M70 P3; We <3 Making Things!
M72 P1; Play Ta-Da song
