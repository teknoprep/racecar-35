// ---------------------------------------------------------------------------
// zdeflate.h — tiny raw-DEFLATE compressor for the dash's upload path
// (v0.1.127 "zblocks"). Fixed-Huffman LZ77 with a stored-block fallback, one
// COMPLETE deflate stream per call (BFINAL=1), input capped at 32 KB so all
// match distances fit the DEFLATE 32K window and positions fit uint16_t.
//
// Why this exists: session NDJSON is ~85-90% redundant (repeated keys), and
// the dash→cloud TCP socket is hard-capped at ~70 KB/s by the lwIP send
// buffer (5744 B) ÷ path RTT. Compressing the body makes the socket carry
// 4-8x more RAW telemetry per second, moving the upload bottleneck back to
// the UART wire. A full zlib/miniz costs 50 KB+ of flash we don't have
// (~66 KB OTA headroom); this is ~3 KB and the server inflates it with
// stock zlib (decompressobj(-15) handles fixed-Huffman fine).
//
// Verified against Python zlib on-host before first flash (see BUILD notes):
// compress → zlib.decompressobj(-15).decompress → byte-identical, ~5-7x on
// real telemetry shapes.
//
// Deflate bit-order gotchas honoured here:
//   - the bitstream is LSB-first,
//   - but Huffman CODES are written MSB-of-code-first (hence zdRev),
//   - extra bits (length/distance) are plain LSB-first values.
// ---------------------------------------------------------------------------
#pragma once
#include <stdint.h>
#include <string.h>
#include <stddef.h>

#define ZDEF_BLOCK_MAX 32768u
#define ZDEF_HASH_BITS 12
#define ZDEF_HASH_SIZE (1u << ZDEF_HASH_BITS)
#define ZDEF_NIL       0xFFFFu
#define ZDEF_MAXCHAIN  64
// Caller must give an output buffer of at least ZDEF_BOUND(n) bytes: the
// stored-block fallback needs n + 5 (header) and we keep a safety margin.
#define ZDEF_BOUND(n)  ((n) + 16u)

// Workspace: 8 KB heads + 64 KB chains = 72 KB. Allocate ONCE from PSRAM.
typedef struct {
    uint16_t head[ZDEF_HASH_SIZE];
    uint16_t prev[ZDEF_BLOCK_MAX];
} ZDeflWS;

typedef struct {
    uint8_t* out;
    size_t   cap, len;
    uint32_t acc;
    int      nbits;
    int      overflow;
} ZdBitW;

static inline void zdPutBits(ZdBitW* b, uint32_t v, int n) {
    b->acc |= v << b->nbits;
    b->nbits += n;
    while (b->nbits >= 8) {
        if (b->len < b->cap) b->out[b->len++] = (uint8_t)(b->acc & 0xFF);
        else b->overflow = 1;
        b->acc >>= 8;
        b->nbits -= 8;
    }
}

static inline uint32_t zdRev(uint32_t v, int n) {
    uint32_t r = 0;
    while (n--) { r = (r << 1) | (v & 1u); v >>= 1u; }
    return r;
}

// Huffman codes go MSB-first into the LSB-first stream.
static inline void zdPutHuff(ZdBitW* b, uint32_t code, int n) {
    zdPutBits(b, zdRev(code, n), n);
}

// Fixed lit/len tree (RFC1951 §3.2.6).
static inline void zdPutLitLen(ZdBitW* b, uint32_t sym) {
    if      (sym <= 143) zdPutHuff(b, 0x030 + sym,         8);
    else if (sym <= 255) zdPutHuff(b, 0x190 + (sym - 144), 9);
    else if (sym <= 279) zdPutHuff(b, 0x000 + (sym - 256), 7);
    else                 zdPutHuff(b, 0x0C0 + (sym - 280), 8);
}

static const uint16_t ZD_LBASE[29] = {3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,
                                      35,43,51,59,67,83,99,115,131,163,195,227,258};
static const uint8_t  ZD_LEXT[29]  = {0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,
                                      3,3,3,3,4,4,4,4,5,5,5,5,0};
static const uint16_t ZD_DBASE[30] = {1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,
                                      257,385,513,769,1025,1537,2049,3073,4097,
                                      6145,8193,12289,16385,24577};
static const uint8_t  ZD_DEXT[30]  = {0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,
                                      7,7,8,8,9,9,10,10,11,11,12,12,13,13};

static inline void zdPutMatch(ZdBitW* b, uint32_t len, uint32_t dist) {
    int lc = 28;
    while (lc > 0 && ZD_LBASE[lc] > len) --lc;
    zdPutLitLen(b, 257 + (uint32_t)lc);
    if (ZD_LEXT[lc]) zdPutBits(b, len - ZD_LBASE[lc], ZD_LEXT[lc]);
    int dc = 29;
    while (dc > 0 && ZD_DBASE[dc] > dist) --dc;
    zdPutHuff(b, (uint32_t)dc, 5);          // fixed dist codes: 5 bits, code == index
    if (ZD_DEXT[dc]) zdPutBits(b, dist - ZD_DBASE[dc], ZD_DEXT[dc]);
}

static inline uint32_t zdHash(const uint8_t* p) {
    uint32_t v = (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16);
    return (v * 2654435761u) >> (32 - ZDEF_HASH_BITS);
}

// Emit one complete stored-block stream (incompressible fallback; also the
// guarantee that output never exceeds ZDEF_BOUND(n)).
static size_t zdStored(const uint8_t* in, size_t n, uint8_t* out, size_t cap) {
    if (cap < n + 5) return 0;
    out[0] = 0x01;                            // BFINAL=1, BTYPE=00, pad to byte
    out[1] = (uint8_t)(n & 0xFF);
    out[2] = (uint8_t)((n >> 8) & 0xFF);
    out[3] = (uint8_t)(~n & 0xFF);
    out[4] = (uint8_t)((~n >> 8) & 0xFF);
    memcpy(out + 5, in, n);
    return n + 5;
}

// Compress one block (n <= ZDEF_BLOCK_MAX) into a complete raw-deflate
// stream. Returns compressed size (> 0 always if cap >= ZDEF_BOUND(n)).
static size_t zdeflate(ZDeflWS* ws, const uint8_t* in, size_t n,
                       uint8_t* out, size_t cap) {
    if (n == 0 || n > ZDEF_BLOCK_MAX) return 0;
    memset(ws->head, 0xFF, sizeof(ws->head));

    ZdBitW b; b.out = out; b.cap = cap; b.len = 0; b.acc = 0; b.nbits = 0; b.overflow = 0;
    zdPutBits(&b, 1, 1);                      // BFINAL
    zdPutBits(&b, 1, 2);                      // BTYPE=01 fixed Huffman

    size_t i = 0;
    while (i < n) {
        uint32_t best_len = 0, best_dist = 0;
        if (i + 4 <= n) {
            const uint32_t h = zdHash(in + i);
            uint16_t j = ws->head[h];
            int chain = ZDEF_MAXCHAIN;
            const uint32_t max_len = (uint32_t)((n - i) < 258 ? (n - i) : 258);
            while (j != ZDEF_NIL && chain-- > 0) {
                const uint8_t* p = in + j;
                const uint8_t* q = in + i;
                if (p[best_len] == q[best_len]) {   // cheap reject
                    uint32_t l = 0;
                    while (l < max_len && p[l] == q[l]) ++l;
                    if (l > best_len) {
                        best_len  = l;
                        best_dist = (uint32_t)(i - j);
                        if (l >= 96) break;          // good enough — stop walking
                    }
                }
                j = ws->prev[j];
            }
        }
        if (best_len >= 4) {
            zdPutMatch(&b, best_len, best_dist);
            // Insert hash entries for the whole covered span so later matches
            // can point into it (bounded: <= 258 inserts).
            const size_t end = i + best_len;
            while (i < end) {
                if (i + 3 <= n) {
                    const uint32_t h2 = zdHash(in + i);
                    ws->prev[i] = ws->head[h2];
                    ws->head[h2] = (uint16_t)i;
                }
                ++i;
            }
        } else {
            zdPutLitLen(&b, in[i]);
            if (i + 3 <= n) {
                const uint32_t h2 = zdHash(in + i);
                ws->prev[i] = ws->head[h2];
                ws->head[h2] = (uint16_t)i;
            }
            ++i;
        }
        if (b.overflow) break;                // incompressible — bail to stored
    }
    if (!b.overflow) {
        zdPutLitLen(&b, 256);                 // end of block
        if (b.nbits > 0) zdPutBits(&b, 0, 8 - b.nbits);  // flush/pad
    }
    if (b.overflow || b.len >= n)             // didn't help — ship it stored
        return zdStored(in, n, out, cap);
    return b.len;
}
