// RULES-ACTIVE-GLOBAL
#include <stdint.h>
#include "../loader/s5_mod_abi.h"

/* Hot-pluggable SD-card module S5_MOD.BIN (linked at 0x07100000, up to 1 MB).
 * Bundles the full S5 Chinese Control Center, S5II Live LUT Bar, 100 SD LUT loader,
 * and Direct-YCbCr 9^3 Real-Time LUT JPEG baker into a single SD-card binary! */

#define S5_MOD_BUILD         1
#define CC_STATE_ADDR        S5_MOD_STATE_ADDR             /* 0x07200000 */
#define CC_LUT_LIST_ADDR     (S5_MOD_STATE_ADDR + 0x0100u) /* 0x07200100 (100 * 64 B) */
#define CC_IO_BUF_ADDR       (S5_MOD_STATE_ADDR + 0x1A00u) /* 0x07201A00 (2048 B DMA) */
#define CC_SD_LUT_BUF_ADDR   (S5_MOD_STATE_ADDR + 0x2200u) /* 0x07202200 (2920 B 0xBEEF) */
#define HOOK_CC_STATE_ADDR   CC_STATE_ADDR
#define HOOK_SD_LUT_BUF_ADDR CC_SD_LUT_BUF_ADDR

#include "../src/control_center.c"
#include "../src/hook.c"

static int mod_on_setup_menu(void) {
    cc_open_from_setup_menu();
    return 1;
}

/* Entry point called once by resident s5_loader.c after loading & verifying CRC32. */
void s5_mod_main(volatile S5ModTable *tbl) {
    volatile CcState *st = cc_get_state();
    cc_boot_autoload_once(st);

    /* Let software keys run through the native translator too. Capturing both
     * here and at on_ui_event would bypass state bookkeeping or handle twice. */
    tbl->on_key           = 0;
    tbl->on_egl_swap      = cc_egl_swap_overlay;
    tbl->on_setup_menu    = mod_on_setup_menu;
    tbl->on_fn_lut        = cc_fn_lut_select_handler;
    tbl->on_lv_style_gate = cc_lv_style_gate;
    tbl->on_jpeg_image    = image_hook_body;
    tbl->on_ui_event      = cc_ui_event_hook;
}
