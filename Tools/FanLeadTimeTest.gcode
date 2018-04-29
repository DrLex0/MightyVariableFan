; This simulates a situation where a short segment needs to be printed with the fan at 50% speed,
; while the fan is off for the rest of the print. Nothing is actually printed and heaters are not
; enabled. It is safe to run this test while filament is loaded.
; This can be used to fine-tune the lead time setting in pwm_postprocessor.py. For this test you
; must enable the -a option to allow splitting up long moves, even if you don't plan to use this
; option for real prints. The best way to verify whether your lead time is OK, is to feel at the
; exhaust of your fan duct whether air flows at the moment the short segment at the front is
; being 'printed'. The test will make three loops, with an increasingly short segment each time.
; The pre-made X3G file was generated with the default lead time of 1.3 seconds.

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

G1 Z10.0 F1100 ; Ensure the script has a Z value to work with, and set it above RAMP_UP_ZMAX.
G1 X40 Y40 F8400

M71; Press OK to start   test

; This message isn't here just for fun. An M71 must be followed by an M70, or else the prompt
; sticks (at least in my firmware), and can make the printer unusable until power cycled!
M73 P0; progress

M70 P2; Print 3cm line at   50% fan speed
G1 F1200
G1 X40 Y-40 E1.2
M106 S127
G1 F1200 ; Slic3r also repeats the feedrate after changing fan speed
G1 X70 Y-40 E0.6
M106 S0
G1 F1200
G1 X70 Y40 E1.2
G1 X40 Y40 E0.6
G1 Z10.2 F1100 ; fake layer change
G1 Z10.0 F1100

M70 P2; Print 2cm line at   50% fan speed
G1 F1200
G1 X40 Y-40 E1.2
M106 S127
G1 F1200 ; Slic3r also repeats the feedrate after changing fan speed
G1 X60 Y-40 E0.4
M106 S0
G1 F1200
G1 X60 Y40 E1.2
G1 X40 Y40 E0.4
G1 Z10.2 F1100 ; fake layer change
G1 Z10.0 F1100

M70 P2; Print 1cm line at   50% fan speed
G1 F1200
G1 X40 Y-40 E1.2
M106 S127
G1 F1200 ; Slic3r also repeats the feedrate after changing fan speed
G1 X50 Y-40 E0.4
M106 S0
G1 F1200
G1 X50 Y40 E1.2
G1 X40 Y40 E0.4
G1 Z10.2 F1100 ; fake layer change
G1 Z10.0 F1100

; The usual buffer stuffing to avoid weirdness at the end of the print...
M70 P1; Wiggle wiggle
G1 F3000
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5
G1 X50
G1 X50.5

;- - - Custom finish printing G-code for FlashForge Creator Pro - - -
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
