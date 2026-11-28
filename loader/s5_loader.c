// RULES-ACTIVE-GLOBAL
#include <stdint.h>
#include "s5_mod_abi.h"

/* Permanent ~1.8 KB resident SD-card module loader linked at 0x070F8000 in 03_program.bin.
 * - Zero FPU, zero heap, 8-byte AAPCS-aligned trampolines.
 * - Loads A:\S5_MOD.BIN (fallback B:\S5_MOD.BIN) into RWX RAM @ 0x07100000,
 *   verifies header + IEEE CRC32, cleans D-Cache, invalidates I-Cache, and calls entry.
 * - If no S5_MOD.BIN is present (or CRC32 fails), every hook transparently falls back
 *   to 100% factory stock Panasonic firmware behavior! */

#define FN_KEY_DISPATCH     0x06643681u
#define FN_RAW_INPUT        0x06642D23u
#define FN_ORIG_PIXEL_REF   0x06825F19u
#define FN_ORIG_LV_STYLE    0x066A07F5u

#define FN_FS_PARSE_PATH    0x06426DA7u
#define FN_FS_OPEN          0x067DC0D5u
#define FN_FS_READ          0x06432F01u
#define FN_FS_CLOSE         0x067DA161u
#define FN_FS_CHECK_DRIVE   0x0668794Bu
#define FS_MODE_READ        0x0401u

typedef void (*orig_key_fn)(uint32_t *ev);
typedef void (*orig_raw_fn)(uint32_t source, const uint32_t *state);
typedef int  (*orig_lv_style_fn)(uint32_t style);
typedef uint32_t (*fs_parse_fn)(const char *path, uint32_t *out_drv, const char **out_rel, uint16_t *out_flag);
typedef uint32_t (*fs_open_fn)(uint32_t drv, uint32_t *out_fd, const char *rel, uint32_t mode);
typedef uint32_t (*fs_read_fn)(uint32_t fd, void *buf, uint32_t flags, uint32_t len, uint32_t z, uint32_t *out_stat);
typedef uint32_t (*fs_close_fn)(uint32_t fd, uint32_t flags);
typedef uint32_t (*fs_check_drive_fn)(uint32_t drv, uint32_t mode);

static const char *const MOD_PATHS[2] = { "A:\\S5_MOD.BIN", "B:\\S5_MOD.BIN" };

static void ldr_dcache_range(uint32_t addr, uint32_t bytes, int clean_only) {
    uint32_t p = addr & ~31u;
    uint32_t end = (addr + bytes + 31u) & ~31u;
    __asm__ volatile("dsb sy" ::: "memory");
    if (clean_only) {
        for (; p < end; p += 32u)
            __asm__ volatile("mcr p15, 0, %0, c7, c10, 1" :: "r"(p) : "memory");
    } else {
        for (; p < end; p += 32u)
            __asm__ volatile("mcr p15, 0, %0, c7, c14, 1" :: "r"(p) : "memory");
    }
    __asm__ volatile("dsb sy" ::: "memory");
}

static void ldr_sync_icache(uint32_t addr, uint32_t bytes) {
    ldr_dcache_range(addr, bytes, 1);
    uint32_t z = 0u;
    /* Broadcast I-Cache & branch predictor invalidate across all 4 Cortex-A7 SMP cores */
    __asm__ volatile(
        "mcr p15, 0, %0, c7, c1, 0\n\t" /* ICIALLUIS (Inner Shareable SMP broadcast) */
        "mcr p15, 0, %0, c7, c5, 0\n\t" /* ICIALLU   (local core I-Cache) */
        "mcr p15, 0, %0, c7, c5, 6\n\t" /* BPIALL    (local branch predictor) */
        "dsb sy\n\t"
        "isb"
        :: "r"(z) : "memory"
    );
}

static uint32_t ldr_crc32(const uint8_t *buf, uint32_t len) {
    uint32_t crc = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < len; ++i) {
        crc ^= buf[i];
        for (uint32_t b = 0; b < 8u; ++b)
            crc = (crc & 1u) ? ((crc >> 1) ^ 0xEDB88320u) : (crc >> 1);
    }
    return ~crc;
}

static volatile S5ModTable *ldr_get_table(void) {
    volatile S5ModTable *tbl = (volatile S5ModTable *)(uintptr_t)S5_MOD_TABLE_ADDR;
    if (tbl->magic != S5_MOD_TBL_MAGIC) {
        volatile uint8_t *p = (volatile uint8_t *)tbl;
        for (uint32_t i = 0; i < sizeof(S5ModTable); ++i)
            p[i] = 0u;
        tbl->abi_version = S5_MOD_ABI_VERSION;
        __asm__ volatile("dmb sy" ::: "memory");
        tbl->magic = S5_MOD_TBL_MAGIC;
        ldr_dcache_range(S5_MOD_TABLE_ADDR, sizeof(S5ModTable), 1);
    }
    return tbl;
}

static uint32_t ldr_fs_read_dma(uint32_t fd, uint8_t *io, uint32_t len) {
    fs_read_fn p_read = (fs_read_fn)(uintptr_t)FN_FS_READ;
    ldr_dcache_range((uint32_t)(uintptr_t)io, len, 0);
    uint32_t stat[2] = { 0u, 0u };
    if (p_read(fd, io, 0u, len, 0u, stat) != 0u)
        return 0u;
    ldr_dcache_range((uint32_t)(uintptr_t)io, len, 0);
    return stat[1];
}

#define S5_LOADER_LOCK_ADDR 0x070FC040u
#define S5_LOADER_READY_MASK_ADDR 0x070FC044u

static int ldr_try_load_mod(volatile S5ModTable *tbl) {
    if (tbl->loaded)
        return 1;

    volatile uint32_t *lock = (volatile uint32_t *)(uintptr_t)S5_LOADER_LOCK_ADDR;
    if (__sync_lock_test_and_set(lock, 1u) != 0u)
        return 0;
    if (tbl->loaded) {
        __sync_lock_release(lock);
        return 1;
    }

    fs_parse_fn p_parse = (fs_parse_fn)(uintptr_t)FN_FS_PARSE_PATH;
    fs_open_fn  p_open  = (fs_open_fn)(uintptr_t)FN_FS_OPEN;
    fs_close_fn p_close = (fs_close_fn)(uintptr_t)FN_FS_CLOSE;
    uint8_t *io = (uint8_t *)(uintptr_t)S5_LOADER_IO_BUF_ADDR;

    tbl->probed = 1u;
    uint8_t last_err = 1u;

    for (uint8_t d = 0u; d < 2u; ++d) {
        uint32_t drv = 0u, fd = 0u;
        const char *rel = 0;
        uint16_t flag = 0u;
        if (p_parse(MOD_PATHS[d], &drv, &rel, &flag) != 0u)
            continue;
        if (p_open(drv, &fd, rel, FS_MODE_READ) != 0u)
            continue;

        uint32_t got = ldr_fs_read_dma(fd, io, sizeof(S5ModHeader));
        if (got != sizeof(S5ModHeader)) {
            p_close(fd, 0u);
            last_err = 2u;
            continue;
        }

        S5ModHeader hdr;
        uint8_t *hdst = (uint8_t *)&hdr;
        for (uint32_t i = 0; i < sizeof(S5ModHeader); ++i)
            hdst[i] = io[i];

        if (hdr.magic != S5_MOD_HDR_MAGIC ||
            hdr.abi_version != S5_MOD_ABI_VERSION ||
            hdr.load_addr != S5_MOD_LOAD_ADDR ||
            hdr.payload_size == 0u ||
            hdr.payload_size > S5_MOD_MAX_SIZE ||
            hdr.bss_size > S5_MOD_MAX_SIZE - hdr.payload_size ||
            hdr.entry_addr < S5_MOD_LOAD_ADDR ||
            hdr.entry_addr >= S5_MOD_LOAD_ADDR + hdr.payload_size ||
            !(hdr.entry_addr & 1u)) {
            p_close(fd, 0u);
            last_err = 2u;
            continue;
        }

        uint8_t *dst = (uint8_t *)(uintptr_t)S5_MOD_LOAD_ADDR;
        uint32_t rem = hdr.payload_size;
        uint32_t off = 0u;
        while (rem > 0u) {
            uint32_t chunk = (rem > 2048u) ? 2048u : rem;
            uint32_t n = ldr_fs_read_dma(fd, io, chunk);
            if (n == 0u)
                break;
            for (uint32_t i = 0; i < n; ++i)
                dst[off + i] = io[i];
            off += n;
            rem -= n;
        }
        p_close(fd, 0u);

        if (off != hdr.payload_size) {
            last_err = 4u;
            continue;
        }

        uint32_t crc = ldr_crc32(dst, hdr.payload_size);
        if (crc != hdr.crc32) {
            last_err = 3u;
            continue;
        }

        for (uint32_t i = 0; i < hdr.bss_size; ++i)
            dst[hdr.payload_size + i] = 0u;

        ldr_sync_icache(S5_MOD_LOAD_ADDR, hdr.payload_size + hdr.bss_size);

        tbl->drive_idx = d;
        tbl->mod_size = hdr.payload_size;
        tbl->mod_crc32 = crc;
        tbl->load_err = 0u;

        s5_mod_entry_fn entry = (s5_mod_entry_fn)(uintptr_t)hdr.entry_addr;
        entry(tbl);

        __asm__ volatile("dmb sy" ::: "memory");
        tbl->loaded = 1u;
        ldr_dcache_range(S5_MOD_TABLE_ADDR, sizeof(S5ModTable), 1);
        __sync_lock_release(lock);
        return 1;
    }

    tbl->load_err = last_err;
    ldr_dcache_range(S5_MOD_TABLE_ADDR, sizeof(S5ModTable), 1);
    __sync_lock_release(lock);
    return 0;
}

/* Native drive validator used by fs_open before its filesystem semaphore.
 * These three handle identities come from 0x06687960..0x0668797A in S5 V2.9.
 * The validator only reads fields: no path dispatch, file I/O or waits while
 * the filesystems are not ready. Do not infer readiness from the first key. */
static uint32_t ldr_ready_drives(void) {
    fs_check_drive_fn check = (fs_check_drive_fn)(uintptr_t)FN_FS_CHECK_DRIVE;
    uint32_t mask = 0u;
    for (uint32_t i = 0u; i < 3u; ++i) {
        uint32_t drv = 0x8C2C41C0u + i * 0x00120F10u + 0x00098ED8u;
        if (check(drv, 1u) == 0u)
            mask |= 1u << i;
    }
    return mask;
}

/* Physical message 0x11F001, callsite 0x064759BE. Only new button/joystick
 * presses may probe; repeats, releases, idle and generic UI events cannot.
 * An observed drive-readiness transition allows retry after an early miss.
 * Setup/Fn remain explicit retries (including same-state card replacements).
 * Always forward the original source/payload to the native raw-state handler. */
void ldr_raw_input_hook(uint32_t source, const uint32_t *state) {
    volatile S5ModTable *tbl = ldr_get_table();
    if (!tbl->loaded && state &&
        ((state[1] & 0x2FFFu) || (state[9] & 0x3FFFFu))) {
        volatile uint32_t *last = (volatile uint32_t *)(uintptr_t)S5_LOADER_READY_MASK_ADDR;
        uint32_t ready = ldr_ready_drives();
        if (ready != *last) {
            *last = ready;
            tbl->probed = 0u;
        }
        if (ready && !tbl->probed)
            ldr_try_load_mod(tbl);
    }
    ((orig_raw_fn)(uintptr_t)FN_RAW_INPUT)(source, state);
}

/* Software-key message 0x11F002 only; physical input uses 0x11F001 instead. */
void ldr_key_hook(uint32_t *ev) {
    volatile S5ModTable *tbl = ldr_get_table();
    if (!tbl->probed && ev && ev[1] == 1u)
        ldr_try_load_mod(tbl);
    if (tbl->loaded && tbl->on_key) {
        tbl->on_key(ev);
        return;
    }
    ((orig_key_fn)(uintptr_t)FN_KEY_DISPATCH)(ev);
}

/* Common native UI event entry. Do not probe SD here: unrelated UI events and
 * the module's own initialization can re-enter this dispatcher. Setup/Fn and
 * the physical/software-key hooks retain their task-context loading paths. */
int ldr_ui_event_handler(uint32_t event, uint32_t param, uint32_t extra) {
    volatile S5ModTable *tbl = ldr_get_table();
    if (tbl->loaded && tbl->abi_version == S5_MOD_ABI_VERSION && tbl->on_ui_event)
        return tbl->on_ui_event(event, param, extra);
    return 0;
}

/* 0x066BEEA6: replace push.w {r4-r8,lr}, replay it on pass-through.
 * Preserve all arguments and the caller LR; the C callback uses an 8-byte
 * aligned stack. No runtime code writes or instruction-cache race. */
__attribute__((naked)) void ldr_ui_event_trampoline(void) {
    __asm__ volatile(
        "push {r0-r3, ip, lr}\n\t"
        "bl ldr_ui_event_handler\n\t"
        "cmp r0, #0\n\t"
        "pop {r0-r3, ip, lr}\n\t"
        "beq 1f\n\t"
        "bx lr\n\t"
        "1:\n\t"
        "push.w {r4-r8, lr}\n\t"
        "ldr pc, =0x066BEEAB\n\t"
        ".ltorg\n\t"
    );
}

/* Hook 2: Setup Menu ID 0x01B5 (像素更新 -> 控制中心) @ 0x068596AE */
int ldr_setup_menu_handler(void) {
    volatile S5ModTable *tbl = ldr_get_table();
    if (!tbl->loaded)
        ldr_try_load_mod(tbl);
    if (tbl->loaded && tbl->on_setup_menu)
        return tbl->on_setup_menu();
    return 0;
}

__attribute__((naked)) void ldr_setup_menu_trampoline(void) {
    __asm__ volatile(
        "push {r0-r3, r4, lr}\n\t"
        "bl ldr_setup_menu_handler\n\t"
        "cmp r0, #0\n\t"
        "pop {r0-r3, r4, lr}\n\t"
        "beq 1f\n\t"
        "ldr pc, =0x068598D1\n\t"  /* pop {r4..r8, lr}; b.w 0x0683291c (commit_ui) */
        "1:\n\t"
        "ldr ip, =0x06825F19\n\t"  /* Stock Pixel Refresh fallback when no S5_MOD.BIN */
        "blx ip\n\t"
        "ldr pc, =0x068596B3\n\t"
        ".ltorg\n\t"
    );
}

/* Hook 3: Native Fn -> LUT Select @ 0x0686A074 */
int ldr_fn_lut_handler(void) {
    volatile S5ModTable *tbl = ldr_get_table();
    if (!tbl->loaded)
        ldr_try_load_mod(tbl);
    if (tbl->loaded && tbl->on_fn_lut)
        return tbl->on_fn_lut();
    return 0;
}

__attribute__((naked)) void ldr_fn_lut_trampoline(void) {
    __asm__ volatile(
        "push {r0-r3, r4, lr}\n\t"
        "bl ldr_fn_lut_handler\n\t"
        "cmp r0, #0\n\t"
        "pop {r0-r3, r4, lr}\n\t"
        "beq 1f\n\t"
        "ldr pc, =0x0686A0EB\n\t"  /* add sp, #0x1c; pop {r4..fp, lr}; b.w commit_ui */
        "1:\n\t"
        "ldr ip, =0x06465A03\n\t"  /* Stock Fn -> LUT Select fallback */
        "blx ip\n\t"
        "ldr pc, =0x0686A079\n\t"
        ".ltorg\n\t"
    );
}

/* Hook 4: Live View 3D-LUT Photo Style Gate @ 0x066AE7C4 */
int ldr_lv_style_gate(uint32_t style) {
    const volatile S5ModTable *tbl = (const volatile S5ModTable *)(uintptr_t)S5_MOD_TABLE_ADDR;
    if (tbl->magic == S5_MOD_TBL_MAGIC && tbl->loaded && tbl->on_lv_style_gate)
        return tbl->on_lv_style_gate(style);
    return ((orig_lv_style_fn)(uintptr_t)FN_ORIG_LV_STYLE)(style);
}

/* Hook 5: Inline eglSwapBuffers @ 0x064C3CDA (zero-write fast exit when unloaded) */
void ldr_egl_swap_overlay(uint32_t dpy, uint32_t surf) {
    const volatile S5ModTable *tbl = (const volatile S5ModTable *)(uintptr_t)S5_MOD_TABLE_ADDR;
    if (tbl->magic == S5_MOD_TBL_MAGIC && tbl->loaded && tbl->on_egl_swap)
        tbl->on_egl_swap(dpy, surf);
}

__attribute__((naked)) void ldr_egl_swap_trampoline(void) {
    __asm__ volatile(
        "push {r0, r1, r2, r3, ip, lr}\n\t"
        "bl ldr_egl_swap_overlay\n\t"
        "pop {r0, r1, r2, r3, ip, lr}\n\t"
        "push.w {r4, r5, r6, r7, r8, lr}\n\t"
        "ldr ip, =0x064C3CDF\n\t"
        "bx ip\n\t"
        ".ltorg\n\t"
    );
}
