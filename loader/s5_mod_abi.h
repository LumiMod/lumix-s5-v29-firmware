// RULES-ACTIVE-GLOBAL
#ifndef S5_MOD_ABI_H
#define S5_MOD_ABI_H

#include <stdint.h>

/* Memory map in 03_program.bin tail cave (0x070DC95C..0x07267000, AP=011 XN=0 RWX):
 * - 0x070F8000 .. 0x070FBFFF (16 KB) : Resident s5_loader.c code (permanent in NAND)
 * - 0x070FC000 .. 0x070FCFFF ( 4 KB) : Resident S5ModTable + 2 KB DMA I/O buffer
 * - 0x07100000 .. 0x071FFFFF ( 1 MB) : Hot-pluggable SD-card S5_MOD.BIN code/rodata/data/bss
 * - 0x07200000 .. 0x07203FFF (16 KB) : Module runtime state (CcState, 100 LUT list, 0xBEEF buf)
 */
#define S5_LOADER_CODE_ADDR     0x070F8000u
#define S5_MOD_TABLE_ADDR       0x070FC000u
#define S5_LOADER_IO_BUF_ADDR   0x070FC400u  /* 2048 B 32B-aligned DMA buffer */
#define S5_MOD_LOAD_ADDR        0x07100000u
#define S5_MOD_MAX_SIZE         0x00100000u  /* 1 MB max S5_MOD.BIN payload + bss */
#define S5_MOD_STATE_ADDR       0x07200000u

#define S5_MOD_HDR_MAGIC        0x444D3553u  /* "S5MD" */
#define S5_MOD_TBL_MAGIC        0x544D3553u  /* "S5MT" */
#define S5_MOD_ABI_VERSION      1u

typedef void (*s5_on_key_fn)(uint32_t *ev);
typedef void (*s5_on_egl_swap_fn)(uint32_t dpy, uint32_t surf);
typedef int  (*s5_on_setup_menu_fn)(void);
typedef int  (*s5_on_fn_lut_fn)(void);
typedef int  (*s5_on_lv_style_gate_fn)(uint32_t style);
typedef int  (*s5_on_jpeg_image_fn)(int status, uint32_t image_id, unsigned char *description);

/* Resident dispatch table @ 0x070FC000 (shared by s5_loader, pb3_stub, and S5_MOD.BIN). */
typedef struct {
    uint32_t               magic;            /* +0x00: S5_MOD_TBL_MAGIC once initialized */
    uint32_t               abi_version;      /* +0x04: S5_MOD_ABI_VERSION */
    uint8_t                loaded;           /* +0x08: 1 if S5_MOD.BIN loaded & initialized */
    uint8_t                probed;           /* +0x09: 1 once boot SD probe ran */
    uint8_t                load_err;         /* +0x0A: 0=ok, 1=not_found, 2=bad_hdr, 3=bad_crc, 4=io_err */
    uint8_t                drive_idx;        /* +0x0B: 0='A', 1='B' */
    uint32_t               mod_size;         /* +0x0C: loaded payload_size */
    uint32_t               mod_crc32;        /* +0x10: verified IEEE CRC32 */
    s5_on_key_fn           on_key;           /* +0x14 */
    s5_on_egl_swap_fn      on_egl_swap;      /* +0x18 */
    s5_on_setup_menu_fn    on_setup_menu;    /* +0x1C: return 1 if handled, 0 for stock fallback */
    s5_on_fn_lut_fn        on_fn_lut;        /* +0x20: return 1 if handled, 0 for stock fallback */
    s5_on_lv_style_gate_fn on_lv_style_gate; /* +0x24 */
    s5_on_jpeg_image_fn    on_jpeg_image;    /* +0x28 */
} S5ModTable;

/* 32-byte header at offset 0 of A:\S5_MOD.BIN / B:\S5_MOD.BIN */
typedef struct {
    uint32_t magic;        /* S5_MOD_HDR_MAGIC ("S5MD") */
    uint32_t abi_version;  /* S5_MOD_ABI_VERSION (1) */
    uint32_t load_addr;    /* must equal S5_MOD_LOAD_ADDR (0x07100000) */
    uint32_t entry_addr;   /* Thumb address (|1) of s5_mod_main(S5ModTable *tbl) */
    uint32_t payload_size; /* bytes immediately following this 32B header */
    uint32_t bss_size;     /* bytes to zero at load_addr + payload_size */
    uint32_t crc32;        /* IEEE 802.3 CRC32 over payload[0..payload_size) */
    uint32_t flags;        /* reserved, 0 */
} S5ModHeader;

typedef void (*s5_mod_entry_fn)(volatile S5ModTable *tbl);

#endif /* S5_MOD_ABI_H */
