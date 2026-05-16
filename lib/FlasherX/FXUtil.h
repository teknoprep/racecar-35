//******************************************************************************
// FXUTIL.H -- FlasherX utility functions
//******************************************************************************
#ifndef FXUTIL_H_
#define FXUTIL_H_

void read_ascii_line( Stream *serial, char *line, int maxbytes );
void update_firmware( Stream *in, Stream *out,
			uint32_t buffer_addr, uint32_t buffer_size );
// racecar-35 addition: non-interactive variant for UART-driven OTA from the
// CrowPanel dash. The original update_firmware prompts the user on `out` for
// the line count to confirm the flash; that doesn't fit our wire protocol.
// This variant skips the prompt and calls flash_move() immediately on EOF.
// Diagnostic prints still go to `out` (USB Serial in our case).
void update_firmware_noprompt( Stream *in, Stream *out,
			uint32_t buffer_addr, uint32_t buffer_size );

#endif
