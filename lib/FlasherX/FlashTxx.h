//******************************************************************************
// Flash write/erase functions (TLC/T3x/T4x/TMM), LMEM cache functions for T3.6
//******************************************************************************
// WARNING:  you can destroy your MCU with flash erase or write!
// This code may or may not protect you from that.
//
// Original by Niels A. Moseley, 2015.
// Modifications for OTA updates by Jon Zeeff, Deb Hollenback
// Paul Stoffregen's T4.x flash routines from Teensy4 core added by Jon Zeeff
// Frank Boesing's T3.x flash routines adapted for OTA by Joe Pasquariello
// This code is released into the public domain.
//******************************************************************************
#ifndef _FLASHTXX_H_
#define _FLASHTXX_H_

#include <stdint.h>     // uint32_t, etc.

#if defined(__MKL26Z64__)
  #define FLASH_ID		"fw_teensyLC"		// target ID (in code)
  #define FLASH_SIZE		(0x10000)		// 64KB program flash
  #define FLASH_SECTOR_SIZE	(0x400)			// 1KB sector size
  #define FLASH_WRITE_SIZE	(4)			// 4-byte/32-bit writes
  #define FLASH_RESERVE		(2*FLASH_SECTOR_SIZE)	// reserve top of flash
  #define FLASH_BASE_ADDR	(0)			// code starts here
#elif defined(__MK20DX128__)
  #define FLASH_ID		"fw_teensy30"		// target ID (in code)
  #define FLASH_SIZE		(0x20000)		// 128KB program flash
  #define FLASH_SECTOR_SIZE	(0x400)			// 1KB sector size
  #define FLASH_WRITE_SIZE	(4)			// 4-byte/32-bit writes
  #define FLASH_RESERVE		(0*FLASH_SECTOR_SIZE)	// reserve top of flash
  #define FLASH_BASE_ADDR	(0)			// code starts here
#elif defined(__MK20DX256__)
  #define FLASH_ID		"fw_teensy32"		// target ID (in code)
  #define FLASH_SIZE		(0x40000)		// 256KB program flash
  #define FLASH_SECTOR_SIZE	(0x800)			// 2KB sectors
  #define FLASH_WRITE_SIZE	(4)    			// 4-byte/32-bit writes
  #define FLASH_RESERVE 	(0*FLASH_SECTOR_SIZE)	// reserve top of flash
  #define FLASH_BASE_ADDR	(0)			// code starts here
#elif defined(__MK64FX512__)
  #define FLASH_ID		"fw_teensy35"		// target ID (in code)
  #define FLASH_SIZE		(0x80000)		// 512KB program flash
  #define FLASH_SECTOR_SIZE	(0x1000)		// 4KB sector size
  #define FLASH_WRITE_SIZE	(8)			// 8-byte/64-bit writes
  #define FLASH_RESERVE		(0*FLASH_SECTOR_SIZE)	// reserve to of flash
  #define FLASH_BASE_ADDR	(0)			// code starts here
#elif defined(__MK66FX1M0__)
  #define FLASH_ID		"fw_teensy36"		// target ID (in code)
  #define FLASH_SIZE		(0x100000)		// 1MB program flash
  #define FLASH_SECTOR_SIZE	(0x1000)		// 4KB sector size
  #define FLASH_WRITE_SIZE	(8)			// 8-byte/64-bit writes
  #define FLASH_RESERVE		(2*FLASH_SECTOR_SIZE)	// reserve top of flash
  #define FLASH_BASE_ADDR	(0)			// code starts here
#elif defined(__IMXRT1062__) && defined(ARDUINO_TEENSY40)
  #define FLASH_ID		"fw_teensy40"		// target ID (in code)
  #define FLASH_SIZE		(0x200000)		// 2MB program flash
  #define FLASH_SECTOR_SIZE	(0x1000)		// 4KB sector size
  #define FLASH_WRITE_SIZE	(4)			// 4-byte/32-bit writes
  // Reserve must cover the PJRC EEPROM emulation (0x601F0000, 15 sectors) —
  // see the T4.1 comment below. 16 sectors = 0x601F0000..top.
  #define FLASH_RESERVE		(16*FLASH_SECTOR_SIZE)	// reserve top of flash + EEPROM emu
  #define FLASH_BASE_ADDR	(0x60000000)		// code starts here
#elif defined(__IMXRT1062__) && defined(ARDUINO_TEENSY41)
  #define FLASH_ID		"fw_teensy41"		// target ID (in code)
  #define FLASH_SIZE		(0x800000)		// 8MB
  #define FLASH_SECTOR_SIZE	(0x1000)		// 4KB sector size
  #define FLASH_WRITE_SIZE	(4)			// 4-byte/32-bit writes    
  // ⚠️ MUST cover the PJRC EEPROM emulation region! The Teensy 4.1 core's
  // eeprom.c writes wear-leveled records into flash 0x607C0000..0x607FF000
  // (63 sectors) — we use EEPROM for the IMU cal, written nearly every boot.
  // firmware_buffer_init() scans DOWN from (top - FLASH_RESERVE) for the first
  // non-erased word and puts the OTA staging buffer ABOVE it: with the stock
  // 16 KB reserve the scan stopped at the topmost EEPROM record, squeezing the
  // staging buffer into the sliver above the EEPROM write pointer (~224 KB and
  // shrinking with every cal write). When the firmware image outgrew that
  // sliver, OTA aborted with FW,ERR,addr_too_large — and successful older OTAs
  // were silently staging THROUGH the EEPROM sectors. 64 sectors = 256 KB
  // covers 0x607C0000..0x60800000, so the buffer lands in the ~7.5 MB of clean
  // flash between the code and the EEPROM region.
  #define FLASH_RESERVE		(64*FLASH_SECTOR_SIZE)	// top of flash + EEPROM emu
  #define FLASH_BASE_ADDR	(0x60000000)		// code starts here
#elif defined(__IMXRT1062__) && defined(ARDUINO_TEENSY_MICROMOD)
  #define FLASH_ID		"fw_teensyMM"		// target ID (in code)
  #define FLASH_SIZE		(0x1000000)		// 16MB
  #define FLASH_SECTOR_SIZE	(0x1000)		// 4KB sector size
  #define FLASH_WRITE_SIZE	(4)			// 4-byte/32-bit writes    
  #define FLASH_RESERVE		(4*FLASH_SECTOR_SIZE)	// reserve top of flash 
  #define FLASH_BASE_ADDR	(0x60000000)		// code starts here
#else
  #error MCU NOT SUPPORTED
#endif

#if defined(FLASH_ID)
  #define RAM_BUFFER_SIZE	(0 * 1024)
  #define IN_FLASH(a) ((a) >= FLASH_BASE_ADDR && (a) < FLASH_BASE_ADDR+FLASH_SIZE)
#endif

// reboot is the same for all ARM devices
#define CPU_RESTART_ADDR	((uint32_t *)0xE000ED0C)
#define CPU_RESTART_VAL		(0x5FA0004)
#define REBOOT			(*CPU_RESTART_ADDR = CPU_RESTART_VAL)

#define NO_BUFFER_TYPE		(0)
#define FLASH_BUFFER_TYPE	(1)
#define RAM_BUFFER_TYPE		(2)

// apparently better - thanks to Frank Boesing
#define RAMFUNC __attribute__ ((section(".fastrun"), noinline, noclone, optimize("Os") ))

#if defined(KINETISK) || defined(KINETISL)

// T3.x flash primitives (must be in RAM)
RAMFUNC int flash_word( uint32_t address, uint32_t value, int aFSEC, int oFSEC );
RAMFUNC int flash_phrase( uint32_t address, uint64_t value, int aFSEC, int oFSEC );
RAMFUNC int flash_erase_sector( uint32_t address, int aFSEC );
RAMFUNC int flash_sector_not_erased( uint32_t address );

// Cache control functions for T3.6 only
#if defined(__MK66FX1M0__)
/*
 * Copyright (c) 2015, Freescale Semiconductor, Inc.
 * Copyright 2016-2017 NXP
 */
void LMEM_EnableCodeCache(bool enable);
void LMEM_CodeCacheInvalidateAll(void);
void LMEM_CodeCachePushAll(void);
void LMEM_CodeCacheClearAll(void);
#endif // __MK66FX1M0__

#elif defined(__IMXRT1062__)

RAMFUNC int flash_sector_not_erased( uint32_t address );

// from cores\Teensy4\eeprom.c  --  use these functions at your own risk!!!
void eepromemu_flash_write(void *addr, const void *data, uint32_t len);
void eepromemu_flash_erase_sector(void *addr);
void eepromemu_flash_erase_32K_block(void *addr);
void eepromemu_flash_erase_64K_block(void *addr);

#endif // __IMXRT1062__

// functions used to move code from buffer to program flash (must be in RAM)
RAMFUNC void flash_move( uint32_t dst, uint32_t src, uint32_t size );

// functions that can be in flash
int  flash_write_block( uint32_t addr, char *data, uint32_t count );
int  flash_erase_block( uint32_t address, uint32_t size );

int  check_flash_id( uint32_t buffer, uint32_t size );
int  firmware_buffer_init( uint32_t *buffer_addr, uint32_t *buffer_size );
void firmware_buffer_free( uint32_t buffer_addr, uint32_t buffer_size );

#endif // _FLASHTXX_H_
