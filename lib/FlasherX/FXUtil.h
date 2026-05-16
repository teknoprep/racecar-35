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

// Per-line ACK variant. After successfully processing each Intel HEX line,
// emits 'A\n' on `ack`. The sender (dash) waits for the ACK before sending
// the next line, providing reliable flow control regardless of how long
// flash sector erases stall the read loop. On any parse/write error, emits
// 'FW,ERR,<reason>\n' on `ack` and returns without committing. On success,
// emits 'FW,COMMITTING\n' just before flash_move() (which doesn't return).
void update_firmware_acked( Stream *in, Stream *out, Stream *ack,
			uint32_t buffer_addr, uint32_t buffer_size );

#endif
