; Start by waiting: by the time this is done, the printer should have processed
; the entire file and already have gone through the context switch between
; print mode and LCD menu, which has a risk of messing up the precise timing
; of the M300 commands.
M70 P2; Playing sequence fordisabling the fan...
G4 P1000
M300 S0 P200; sequence 0-0-0 (off)
M300 S5988 P20; beep
M300 S0 P100; rest
M300 S5988 P20; beep
M300 S0 P100; rest
M300 S5988 P20; beep
M300 S0 P200; end sequence
G4 P1000; wait
