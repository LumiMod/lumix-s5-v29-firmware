// RULES-ACTIVE-GLOBAL
#include <stdint.h>
#include "s5_mod_abi.h"

/* Permanent ~96-byte resident bridge linked at 0x879F7900 in 07_postboot3.bin.
 * Calls original get_description(image_id, description) @ 0x87864ACD, then checks
 * S5ModTable @ 0x070FC000. If S5_MOD.BIN registered on_jpeg_image, dispatches to it;
 * otherwise returns immediately with zero overhead! */

int pb3_image_bridge(uint32_t image_id, unsigned char *description) {
    int (*get_description)(uint32_t, unsigned char *) = (void *)(uintptr_t)0x87864ACDu;
    int status = get_description(image_id, description);

    /* Invalidate the 2 cache lines of S5ModTable @ 0x070FC000 for SMP coherency */
    uint32_t p0 = S5_MOD_TABLE_ADDR;
    uint32_t p1 = S5_MOD_TABLE_ADDR + 32u;
    __asm__ volatile(
        "dsb sy\n\t"
        "mcr p15, 0, %0, c7, c14, 1\n\t"
        "mcr p15, 0, %1, c7, c14, 1\n\t"
        "dsb sy"
        :: "r"(p0), "r"(p1) : "memory"
    );

    const volatile S5ModTable *tbl = (const volatile S5ModTable *)(uintptr_t)S5_MOD_TABLE_ADDR;
    if (tbl->magic == S5_MOD_TBL_MAGIC && tbl->loaded && tbl->on_jpeg_image)
        return tbl->on_jpeg_image(status, image_id, description);
    return status;
}
