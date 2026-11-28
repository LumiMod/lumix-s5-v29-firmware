// RULES-ACTIVE-GLOBAL
#include <stddef.h>
#include <stdint.h>
#include "lut_tables.h"

/* Static .bss scratch buffers (keeps RTOS task stack < 160 bytes; S5 stack is ~4 KB):
 * - dynamic_rgb_lut9   : unpacked 9^3 RGB8 0xBEEF table (2,916 B)
 * - dynamic_ycbcr_lut9 : direct YCbCr->YCbCr 9^3 baked table (2,916 B) */
static uint32_t dynamic_rgb_lut9[9u * 9u * 9u];
static uint32_t dynamic_ycbcr_lut9[9u * 9u * 9u];

/* Single-cycle ARMv7 Thumb-2 unsigned 8-bit saturate (0..255). */
static inline uint8_t clamp_u8(int v) {
    uint32_t out;
    __asm__("usat %0, #8, %1" : "=r"(out) : "r"(v));
    return (uint8_t)out;
}

/* Single-cycle ARMv7 Thumb-2 signed 8-bit saturate (-128..+127). */
static inline int8_t clamp_s8(int v) {
    int32_t out;
    __asm__("ssat %0, #8, %1" : "=r"(out) : "r"(v));
    return (int8_t)out;
}

static inline uint32_t get32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline unsigned get16(const unsigned char *p) {
    return (unsigned)p[0] | ((unsigned)p[1] << 8);
}

/* Allow both uncached/low (0x06000000+) and high-alias (0x88000000+) DRAM addresses. */
static int valid_plane(uint32_t address, uint32_t bytes, unsigned stride, unsigned width, unsigned height) {
    return address >= 0x06000000u && address < 0xc0000000u &&
           !(address & 31u) && stride >= width && stride <= 8192u &&
           !(stride & 31u) && bytes >= stride * height &&
           bytes <= 64u * 1024u * 1024u && address <= 0xc0000000u - bytes;
}

/* 4x unrolled D-Cache maintenance (32B line stride). */
static void cache_range(uint32_t address, uint32_t bytes, int clean_only) {
    uint32_t end = address + bytes;
    uint32_t p = address;
    __asm__ volatile("dsb sy" ::: "memory");
    if (clean_only) {
        for (; p + 128u <= end; p += 128u) {
            uint32_t p1 = p + 32u, p2 = p + 64u, p3 = p + 96u;
            __asm__ volatile(
                "mcr p15, 0, %0, c7, c10, 1\n\t"
                "mcr p15, 0, %1, c7, c10, 1\n\t"
                "mcr p15, 0, %2, c7, c10, 1\n\t"
                "mcr p15, 0, %3, c7, c10, 1"
                :: "r"(p), "r"(p1), "r"(p2), "r"(p3) : "memory"
            );
        }
        for (; p < end; p += 32u)
            __asm__ volatile("mcr p15, 0, %0, c7, c10, 1" :: "r"(p) : "memory");
    } else {
        for (; p + 128u <= end; p += 128u) {
            uint32_t p1 = p + 32u, p2 = p + 64u, p3 = p + 96u;
            __asm__ volatile(
                "mcr p15, 0, %0, c7, c14, 1\n\t"
                "mcr p15, 0, %1, c7, c14, 1\n\t"
                "mcr p15, 0, %2, c7, c14, 1\n\t"
                "mcr p15, 0, %3, c7, c14, 1"
                :: "r"(p), "r"(p1), "r"(p2), "r"(p3) : "memory"
            );
        }
        for (; p < end; p += 32u)
            __asm__ volatile("mcr p15, 0, %0, c7, c14, 1" :: "r"(p) : "memory");
    }
    __asm__ volatile("dsb sy" ::: "memory");
}

/* Sample 9^3 RGB LUT once per grid node (only called 729 times during setup, <0.05 ms). */
static void sample_rgb9_setup(const uint32_t *lut, unsigned r, unsigned g, unsigned b, int out[3]) {
    unsigned ri = r >> 5, rw = r & 31u;
    unsigned gi = g >> 5, gw = g & 31u;
    unsigned bi = b >> 5, bw = b & 31u;
    const uint32_t *p = lut + bi * 81u + gi * 9u + ri;
    uint32_t v0 = p[0], v3 = p[91u], v1, v2;
    int w1, w2, w3;
    if (rw >= gw) {
        if (gw >= bw)      { v1 = p[1];  v2 = p[10]; w1 = rw; w2 = gw; w3 = bw; }
        else if (rw >= bw) { v1 = p[1];  v2 = p[82]; w1 = rw; w2 = bw; w3 = gw; }
        else               { v1 = p[81]; v2 = p[82]; w1 = bw; w2 = rw; w3 = gw; }
    } else {
        if (bw > gw)       { v1 = p[81]; v2 = p[90]; w1 = bw; w2 = gw; w3 = rw; }
        else if (bw > rw)  { v1 = p[9];  v2 = p[90]; w1 = gw; w2 = bw; w3 = rw; }
        else               { v1 = p[9];  v2 = p[10]; w1 = gw; w2 = rw; w3 = bw; }
    }
    int r0 = (int)(v0 & 0xFFu), r1 = (int)(v1 & 0xFFu), r2 = (int)(v2 & 0xFFu), r3 = (int)(v3 & 0xFFu);
    int g0 = (int)((v0 >> 8) & 0xFFu), g1 = (int)((v1 >> 8) & 0xFFu), g2 = (int)((v2 >> 8) & 0xFFu), g3 = (int)((v3 >> 8) & 0xFFu);
    int b0 = (int)(v0 >> 16), b1 = (int)(v1 >> 16), b2 = (int)(v2 >> 16), b3 = (int)(v3 >> 16);
    out[0] = r0 + (((r1 - r0) * w1 + (r2 - r1) * w2 + (r3 - r2) * w3 + 16) >> 5);
    out[1] = g0 + (((g1 - g0) * w1 + (g2 - g1) * w2 + (g3 - g2) * w3 + 16) >> 5);
    out[2] = b0 + (((b1 - b0) * w1 + (b2 - b1) * w2 + (b3 - b2) * w3 + 16) >> 5);
}

/* Unpack real DC-S5 0xBEEF 9^3 octant LUT and bake directly into YCbCr->YCbCr 9^3 table. */
static void unpack_beef9_to_ycbcr(const uint16_t *src, uint32_t *dst_ycbcr) {
    for (unsigned i = 0; i < 9u * 9u * 9u; ++i) {
        unsigned off = beef9_slot_off[i];
        uint32_t w0 = src[off * 2u];
        uint32_t w1 = src[off * 2u + 1u];
        uint32_t r6 = w0 & 0x3Fu;
        uint32_t b6 = (w0 >> 6) & 0x3Fu;
        uint32_t g6 = ((w0 >> 12) & 0x0Fu) | ((w1 & 0x03u) << 4);
        uint32_t r8 = (r6 << 2) | (r6 >> 4);
        uint32_t g8 = (g6 << 2) | (g6 >> 4);
        uint32_t b8 = (b6 << 2) | (b6 >> 4);
        dynamic_rgb_lut9[i] = r8 | (g8 << 8) | (b8 << 16);
    }
    unsigned idx = 0;
    for (unsigned cr_i = 0; cr_i < 9u; ++cr_i) {
        int cr = (int)(cr_i == 8u ? 255u : (cr_i << 5)) - 128;
        for (unsigned cb_i = 0; cb_i < 9u; ++cb_i) {
            int cb = (int)(cb_i == 8u ? 255u : (cb_i << 5)) - 128;
            int dr = (359 * cr + 128) >> 8;
            int dg = (88 * cb + 183 * cr + 128) >> 8;
            int db = (454 * cb + 128) >> 8;
            for (unsigned y_i = 0; y_i < 9u; ++y_i) {
                int y = (int)(y_i == 8u ? 255u : (y_i << 5));
                int rgb[3];
                sample_rgb9_setup(dynamic_rgb_lut9, clamp_u8(y + dr), clamp_u8(y - dg), clamp_u8(y + db), rgb);
                uint32_t yo = clamp_u8((77 * rgb[0] + 150 * rgb[1] + 29 * rgb[2] + 128) >> 8);
                uint32_t cbo = (uint32_t)((int)clamp_s8((-43 * rgb[0] - 85 * rgb[1] + 128 * rgb[2] + 128) >> 8) + 128);
                uint32_t cro = (uint32_t)((int)clamp_s8((128 * rgb[0] - 107 * rgb[1] - 21 * rgb[2] + 128) >> 8) + 128);
                dst_ycbcr[idx++] = yo | (cbo << 8) | (cro << 16);
            }
        }
    }
}

/* Real DC-S5 slot pointers at 0x08A9D47C[1..4] reside in 0x062B6000..0x062B9000. */
static const uint16_t *active_slot_lut9(uint32_t lut_id) {
    if (lut_id < 1u || lut_id >= 8u)
        return NULL;
    const volatile uint32_t *slot_ptrs = (const volatile uint32_t *)(uintptr_t)0x08a9d47cu;
    uint32_t buf_addr = slot_ptrs[lut_id];
    if (buf_addr < 0x06000000u || buf_addr >= 0xc0000000u || (buf_addr & 3u))
        return NULL;
    const uint16_t *hdr = (const uint16_t *)(uintptr_t)buf_addr;
    if (hdr[0] != 0xbeefu || hdr[1] != 1u)
        return NULL;
    return hdr + 2;
}

/* Ultra-fast 4:2:2 direct YCbCr->YCbCr 9^3 tetrahedral kernel with SWAR 2-lane (Y+Cr) packing. */
static inline void process_pair_ycbcr9(const uint32_t *p_uv, unsigned y0, unsigned y1,
                                       int cb_w, int cr_w, int cb_ge_cr,
                                       unsigned char *yp, int8_t *cp) {
    const unsigned scb = 9u, scr = 81u, s_diag = 91u;

    /* Pixel 0 (even): SWAR 2-lane (Y + Cr together) + Cb */
    const uint32_t *p0 = p_uv + (y0 >> 5);
    int yw0 = (int)(y0 & 31u);
    uint32_t v0 = p0[0], v3 = p0[s_diag], v1, v2;
    uint32_t w1, w2, w3;
    if (cb_ge_cr) {
        if (yw0 >= cb_w)      { v1 = p0[1];   v2 = p0[scb + 1u]; w1 = (uint32_t)yw0;  w2 = (uint32_t)cb_w; w3 = (uint32_t)cr_w; }
        else if (yw0 >= cr_w) { v1 = p0[scb]; v2 = p0[scb + 1u]; w1 = (uint32_t)cb_w; w2 = (uint32_t)yw0;  w3 = (uint32_t)cr_w; }
        else                  { v1 = p0[scb]; v2 = p0[scr + scb]; w1 = (uint32_t)cb_w; w2 = (uint32_t)cr_w; w3 = (uint32_t)yw0;  }
    } else {
        if (yw0 >= cr_w)      { v1 = p0[1];   v2 = p0[scr + 1u]; w1 = (uint32_t)yw0;  w2 = (uint32_t)cr_w; w3 = (uint32_t)cb_w; }
        else if (yw0 >= cb_w) { v1 = p0[scr]; v2 = p0[scr + 1u]; w1 = (uint32_t)cr_w; w2 = (uint32_t)yw0;  w3 = (uint32_t)cb_w; }
        else                  { v1 = p0[scr]; v2 = p0[scr + scb]; w1 = (uint32_t)cr_w; w2 = (uint32_t)cb_w; w3 = (uint32_t)yw0;  }
    }
    uint32_t k0 = 32u - w1;
    uint32_t k1 = w1 - w2;
    uint32_t k2 = w2 - w3;
    uint32_t k3 = w3;
    uint32_t ycr = (v0 & 0x00FF00FFu) * k0 + (v1 & 0x00FF00FFu) * k1 +
                   (v2 & 0x00FF00FFu) * k2 + (v3 & 0x00FF00FFu) * k3 + 0x00100010u;
    uint32_t cbv = ((v0 >> 8) & 0xFFu) * k0 + ((v1 >> 8) & 0xFFu) * k1 +
                   ((v2 >> 8) & 0xFFu) * k2 + ((v3 >> 8) & 0xFFu) * k3 + 16u;
    yp[0] = (uint8_t)(ycr >> 5);
    cp[0] = (int8_t)((int)(cbv >> 5) - 128);
    cp[1] = (int8_t)((int)(ycr >> 21) - 128);

    /* Pixel 1 (odd): ONLY Y1' (byte loads, 3 muls total!) */
    const uint8_t *p1b = (const uint8_t *)(p_uv + (y1 >> 5));
    int yw1 = (int)(y1 & 31u);
    int q0 = (int)p1b[0], q3 = (int)p1b[s_diag << 2], q1, q2;
    int u1, u2, u3;
    if (cb_ge_cr) {
        if (yw1 >= cb_w)      { q1 = (int)p1b[4];        q2 = (int)p1b[(scb + 1u) << 2]; u1 = yw1;  u2 = cb_w; u3 = cr_w; }
        else if (yw1 >= cr_w) { q1 = (int)p1b[scb << 2]; q2 = (int)p1b[(scb + 1u) << 2]; u1 = cb_w; u2 = yw1;  u3 = cr_w; }
        else                  { q1 = (int)p1b[scb << 2]; q2 = (int)p1b[(scr + scb) << 2]; u1 = cb_w; u2 = cr_w; u3 = yw1;  }
    } else {
        if (yw1 >= cr_w)      { q1 = (int)p1b[4];        q2 = (int)p1b[(scr + 1u) << 2]; u1 = yw1;  u2 = cr_w; u3 = cb_w; }
        else if (yw1 >= cb_w) { q1 = (int)p1b[scr << 2]; q2 = (int)p1b[(scr + 1u) << 2]; u1 = cr_w; u2 = yw1;  u3 = cb_w; }
        else                  { q1 = (int)p1b[scr << 2]; q2 = (int)p1b[(scr + scb) << 2]; u1 = cr_w; u2 = cb_w; u3 = yw1;  }
    }
    yp[1] = (uint8_t)(q0 + (((q1 - q0) * u1 + (q2 - q1) * u2 + (q3 - q2) * u3 + 16) >> 5));
}

#ifndef HOOK_CC_STATE_ADDR
#define HOOK_CC_STATE_ADDR   0x07108000u
#endif
#ifndef HOOK_SD_LUT_BUF_ADDR
#define HOOK_SD_LUT_BUF_ADDR 0x0710a200u
#endif

int image_hook_body(int status, uint32_t image_id, unsigned char *description) {
    (void)image_id;

    /* Invalidate CcState cache lines so SMP cores always see live Control Center updates */
    cache_range(HOOK_CC_STATE_ADDR, 64u, 0);
    const volatile uint32_t *cc_hdr = (const volatile uint32_t *)(uintptr_t)HOOK_CC_STATE_ADDR;
    const volatile uint8_t *cc_bytes = (const volatile uint8_t *)(uintptr_t)HOOK_CC_STATE_ADDR;
    int cc_valid = (cc_hdr[0] == 0x53354343u);
    int all_styles = cc_valid && (cc_bytes[6] != 0u);
    uint8_t photo_style = *(const volatile uint8_t *)(uintptr_t)0x08cc1864u;
    /* Verified S5 V2.9 Photo Style IDs: 0x0A/0x0B = V-Log, 0x1E..0x27 = My Photo Style 1..10 */
    int is_rtl_style = all_styles || (photo_style == 0x0au) || (photo_style == 0x0bu) ||
                       (photo_style >= 0x1eu && photo_style <= 0x27u);
    unsigned width  = get16(description + 0x1c);
    unsigned height = get16(description + 0x1e);

    if (status || !is_rtl_style || get32(description + 0x20) != 2u ||
        width < 1600u || width > 6000u || (width & 1u) ||
        height < 720u || height > 4000u || (height & 1u))
        return status;

    const uint16_t *raw_slot = NULL;
    /* Priority 1: Active SD-card LUT (1..100) pre-packed into HOOK_SD_LUT_BUF_ADDR by Control Center */
    if (cc_valid && cc_bytes[12] != 0u) {
        cache_range(HOOK_SD_LUT_BUF_ADDR, 2944u, 0);
        const uint16_t *sd_hdr = (const uint16_t *)(uintptr_t)HOOK_SD_LUT_BUF_ADDR;
        if (sd_hdr[0] == 0xbeefu && sd_hdr[1] == 1u)
            raw_slot = sd_hdr + 2;
    }

    if (!raw_slot) {
        /* Priority 2: Built-in LUT Select slot (0x08CC576A: 0=OFF/Pure V-Log, 1..4=LUT1..LUT4) */
        uint32_t lut_id = *(const volatile uint8_t *)(uintptr_t)0x08cc576au;
        if (lut_id == 0u)
            return status;
        if (lut_id > 4u)
            lut_id = *(const volatile uint32_t *)(uintptr_t)0x08bdc330u;
        raw_slot = active_slot_lut9(lut_id);
        if (!raw_slot)
            return status;
    }

    uint32_t y_address = get32(description), c_address = get32(description + 12);
    unsigned y_stride = get16(description + 8), c_stride = get16(description + 20);
    uint32_t y_bytes = get32(description + 4), c_bytes = get32(description + 16);
    if (!valid_plane(y_address, y_bytes, y_stride, width, height) ||
        !valid_plane(c_address, c_bytes, c_stride, width, height) ||
        !(y_address + y_bytes <= c_address || c_address + c_bytes <= y_address))
        return status;

    unpack_beef9_to_ycbcr(raw_slot, dynamic_ycbcr_lut9);

    cache_range(y_address, y_stride * height, 0);
    cache_range(c_address, c_stride * height, 0);

    for (unsigned row = 0; row < height; ++row) {
        unsigned char *y = (void *)(uintptr_t)(y_address + row * y_stride);
        int8_t *c = (int8_t *)(uintptr_t)(c_address + row * c_stride);
        for (unsigned x = 0; x < width; x += 2u) {
            unsigned cb_u = (unsigned)((int)c[x] + 128);
            unsigned cr_u = (unsigned)((int)c[x + 1u] + 128);
            const uint32_t *p_uv = dynamic_ycbcr_lut9 + (cr_u >> 5) * 81u + (cb_u >> 5) * 9u;
            int cb_w = (int)(cb_u & 31u), cr_w = (int)(cr_u & 31u);
            process_pair_ycbcr9(p_uv, y[x], y[x + 1u], cb_w, cr_w, cb_w >= cr_w, y + x, c + x);
        }
    }

    cache_range(y_address, y_stride * height, 1);
    cache_range(c_address, c_stride * height, 1);
    return status;
}

#ifndef S5_MOD_BUILD
int image_hook(uint32_t image_id, unsigned char *description) {
    int (*get_description)(uint32_t, unsigned char *) =
        (void *)(uintptr_t)0x87864acdu;
    int status = get_description(image_id, description);
    return image_hook_body(status, image_id, description);
}
#endif
