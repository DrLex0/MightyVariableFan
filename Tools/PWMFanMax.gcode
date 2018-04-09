; Start by waiting: by the time this is done, the printer should have processed
; the entire file and already have gone through the context switch between
; print mode and LCD menu, which has a risk of messing up the precise timing
; of the M300 commands.
M70 P2; Playing sequence formaximum fan speed...
G4 P1000
M300 S0 P200; sequence 3-3-3 (100%)
M300 S7407 P20; beep
M300 S0 P100; rest
M300 S7407 P20; beep
M300 S0 P100; rest
M300 S7407 P20; beep
M300 S0 P200; end sequence
G4 P1000; wait
