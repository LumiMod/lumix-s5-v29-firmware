// RULES-ACTIVE-GLOBAL
#include <stdint.h>
#include "cc_font_zh.h"

/* S5 Control Center (Luminol-style independent OpenVG OSD overlay)
 * - Chinese UI + SD card config (A:\S5_CC.CFG / B:\S5_CC.CFG)
 * - Up to 100 SD card .VLT / .CUBE LUTs from A:\LUT\ (or B:\LUT\)
 * - Full Live View ISP 3D-LUT hardware sync + Real-Time JPEG hook integration
 * - Thread-safe GUI redraw trigger + DMA-aligned static SD I/O + D-Cache maintenance
 * Linked at 0x07100000 in 03_program.bin tail cave (low-VA runtime domain).
 * Zero FPU/VFP instructions, zero heap allocation. */

#ifndef CC_STATE_ADDR
#define CC_STATE_ADDR       0x07108000u  /* 256 B CcState (32B aligned) */
#endif
#ifndef CC_LUT_LIST_ADDR
#define CC_LUT_LIST_ADDR    0x07108100u  /* 100 * 64 B = 6400 B CcLutEntry table */
#endif
#ifndef CC_IO_BUF_ADDR
#define CC_IO_BUF_ADDR      0x07109A00u  /* 2048 B DMA-safe 32B-aligned FS buffer */
#endif
#ifndef CC_SD_LUT_BUF_ADDR
#define CC_SD_LUT_BUF_ADDR  0x0710A200u  /* 2920 B 32B-aligned 0xBEEF 9^3 octant LUT */
#endif

#define CC_MAGIC            0x53354343u  /* "S5CC" */
#define CC_CFG_MAGIC        0x46433553u  /* "S5CF" */
#define CC_CFG_VERSION      3u
#define CC_MAX_SD_LUTS      100u

/* Live Panasonic DC-S5 V2.9 addresses (low-VA runtime domain) */
#define ADDR_PHOTO_STYLE    0x08CC1864u  /* 0x0A/0x0B=V-Log, 0x0C=HLG, 0x1E..0x27=My Photo Style 1..10 */
#define ADDR_LUT_SLOT_U8    0x08CC576Au  /* 0=Vlog_709/OFF, 1..4=LUT1..4 */
#define ADDR_LCD_VA_EN_U8   0x08CC576Bu  /* LCD V-Log View Assist enable (read by 0x066AE6EC) */
#define ADDR_HDMI_VA_EN_U8  0x08CC576Cu  /* HDMI V-Log View Assist enable (read by 0x066AE6EC) */
#define ADDR_LCD_VA_TYPE    0x08BDC310u  /* Monitor View Assist type (0=OFF, 1=LUT) */
#define ADDR_HDMI_VA_TYPE   0x08BDC314u  /* HDMI View Assist type (0=OFF, 1=LUT) */
#define ADDR_LUT_SLOT_U32   0x08BDC330u  /* Mirror slot index */
#define ADDR_LUT_SLOT_PTRS  0x08A9D47Cu  /* uint32_t[8] slot buffer pointers */
#define ADDR_LUT_FORCE_DIRTY 0x08A9D471u /* uint8_t[2] ISP LUT force-reload flags (LCD/HDMI) */
#define ADDR_LCD_LUT_DIRTY  0x08AA0CB6u  /* LCD LUT cache dirty flag */
#define ADDR_HDMI_LUT_DIRTY 0x08AA0CB9u  /* HDMI LUT cache dirty flag */
#define ADDR_OCTANT_MAP     0x06E39EB4u  /* 729 * {uint16_t tnum, uint16_t idx} */
#define ADDR_BEEP_ID1_CNT   0x06E35134u  /* Focus Beep sample count */

/* Original firmware functions (Thumb bit |1) */
#define FN_KEY_DISPATCH     0x06643681u
#define FN_COMMIT_UI        0x0683291Du  /* Thread-safe GUI redraw trigger */
#define FN_LV_LUT_APPLY     0x066AE6BDu  /* void lv_lut_apply(slot, lcd_en, hdmi_en) */
#define FN_EGL_QUERY_SURF   0x064C2181u
#define FN_VG_SETI          0x065D93D7u
#define FN_VG_GETI          0x065D9451u
#define FN_VG_SETFV         0x065D93F3u
#define FN_VG_CLEAR         0x065D9B0Du

/* Native Panasonic S5 V2.9 SD-card File System APIs (Thumb bit |1) */
#define FN_FS_PARSE_PATH    0x06426DA7u
#define FN_FS_OPEN          0x067DC0D5u
#define FN_FS_SEEK          0x067ADAC9u
#define FN_FS_READ          0x06432F01u
#define FN_FS_WRITE         0x067302D5u
#define FN_FS_CLOSE         0x067DA161u

#define FS_ERR_EXIST        0xA405u
#define FS_MODE_READ        0x0401u
#define FS_MODE_CREATE      0x0800u
#define FS_MODE_TRUNC       0x0200u

#define EGL_HEIGHT          0x3056u
#define EGL_WIDTH           0x3057u
#define VG_SCISSORING       0x1102u
#define VG_CLEAR_COLOR      0x1121u

/* Software-key indices, NOT native GUI key codes. */
#define KEY_UP              3u
#define KEY_DOWN            4u
#define KEY_LEFT            5u
#define KEY_RIGHT           6u
#define KEY_CENTER          7u
#define KEY_PLAY            8u
#define KEY_Q_MENU          15u
#define KEY_SET             58u

#define CC_ITEM_COUNT       7u

/* UI visibility modes */
#define CC_VIS_OFF          0u
#define CC_VIS_PANEL        1u  /* Full-screen Control Center (Setup -> 控制中心) */
#define CC_VIS_LUT_BAR      2u  /* S5II-style compact bottom Live LUT selector bar */

/* LUT Fn shortcut modes (bound to native Fn -> LUT Select @ 0x0686A074) */
#define LUT_HK_STOCK        0u  /* 关闭 (原厂) */
#define LUT_HK_BAR          1u  /* Fn: 实时选LUT (default: S5II-style bottom HUD bar) */
#define LUT_HK_PANEL        2u  /* Fn: 打开面板 (full Control Center) */

#define SD_STAT_NONE        0u  /* [无SD卡] */
#define SD_STAT_LOADED      1u  /* [SD卡已加载] */
#define SD_STAT_SAVED       2u  /* [SD卡已保存] */
#define SD_STAT_DIRTY       3u  /* [未保存] */
#define SD_STAT_SAVE_FAILED 4u  /* [保存失败] */

typedef struct {
    uint32_t magic;            /* +0  */
    uint8_t  visible;          /* +4  */
    uint8_t  cursor;           /* +5  */
    uint8_t  lut_all_styles;   /* +6  (read by hook.c) */
    uint8_t  beep_muted;       /* +7  */
    uint8_t  theme;            /* +8  */
    uint8_t  sd_lut_drive;     /* +9  0='A', 1='B' */
    uint8_t  sd_status;        /* +10 */
    uint8_t  sd_loaded_once;   /* +11 */
    uint8_t  sd_lut_active;    /* +12 (read by hook.c: 0=OFF/builtin, 1..100=SD LUT) */
    uint8_t  sd_lut_count;     /* +13 (0..100 discovered on SD) */
    uint8_t  sd_lut_err;       /* +14 */
    uint8_t  sd_scanned;       /* +15 */
    uint32_t saved_beep_cnt;   /* +16 */
    uint32_t saved_slot1_ptr;  /* +20 */
    uint32_t open_count;       /* +24 */
    char     active_label[20]; /* +28 */
    uint8_t  lut_hotkey;       /* +48: 0=原厂, 1=Fn实时选LUT, 2=Fn打开面板 */
    uint8_t  boot_checked;     /* +49: 1 once S5_CC.CFG auto-load probed after boot */
    uint8_t  pad_tail[2];
} CcState;

typedef struct {
    char fname[44];            /* up to 43 chars e.g. "Kodak_2383_Rec709_v2.cube" */
    char label[20];            /* display name up to 19 chars */
} CcLutEntry;                  /* 64 B */

typedef struct {
    uint32_t magic;
    uint32_t version;
    uint8_t  lut_slot;         /* 0..4 builtin */
    uint8_t  sd_lut_active;    /* 0..100 SD LUT */
    uint8_t  lut_all_styles;
    uint8_t  beep_muted;
    uint8_t  theme;
    uint8_t  lut_hotkey;
    uint8_t  pad[2];
    char     sd_fname[44];     /* saved SD LUT filename */
    uint32_t reserved[4];
    uint32_t checksum;
} CcDiskCfg;                   /* 80 B */

typedef void (*key_dispatch_fn)(uint32_t *ev);
typedef void (*commit_ui_fn)(void);
typedef void (*lv_lut_apply_fn)(uint32_t slot, uint32_t lcd_en, uint32_t hdmi_en);
typedef uint32_t (*egl_query_fn)(uint32_t dpy, uint32_t surf, uint32_t attr, int32_t *val);
typedef void (*vg_seti_fn)(uint32_t param, int32_t val);
typedef int32_t (*vg_geti_fn)(uint32_t param);
typedef void (*vg_setfv_fn)(uint32_t param, int32_t count, const uint32_t *vals);
typedef void (*vg_clear_fn)(int32_t x, int32_t y, int32_t w, int32_t h);

typedef uint32_t (*fs_parse_fn)(const char *path, uint32_t *out_drv, const char **out_rel, uint16_t *out_flag);
typedef uint32_t (*fs_open_fn)(uint32_t drv, uint32_t *out_fd, const char *rel, uint32_t mode);
typedef uint32_t (*fs_seek_fn)(uint32_t fd, uint32_t offset, uint32_t whence);
typedef uint32_t (*fs_read_fn)(uint32_t fd, void *buf, uint32_t flags, uint32_t len, uint32_t z, uint32_t *out_stat);
typedef uint32_t (*fs_write_fn)(uint32_t fd, const void *buf, uint32_t flags, uint32_t len, uint32_t z, uint32_t *out_stat);
typedef uint32_t (*fs_close_fn)(uint32_t fd, uint32_t flags);

static const uint32_t COL_BG[4]       = { 0x3D808081u, 0x3DA4A4A5u, 0x3DE0E0E1u, 0x3F70A3D7u };
static const uint32_t COL_BORDER[4]   = { 0x3E48C8C9u, 0x3E828283u, 0x3EA6A6A7u, 0x3F800000u };
static const uint32_t COL_ROW_ALT[4]  = { 0x3DB0B0B1u, 0x3DE0E0E1u, 0x3E141415u, 0x3F800000u };
static const uint32_t COL_ROW_SEL[4]  = { 0x3E088889u, 0x3E48C8C9u, 0x3E949495u, 0x3F800000u };
static const uint32_t COL_WHITE[4]    = { 0x3F6E6E6Fu, 0x3F727273u, 0x3F787879u, 0x3F800000u };
static const uint32_t COL_MUTED[4]    = { 0x3F0E0E0Fu, 0x3F181819u, 0x3F262627u, 0x3F800000u };
static const uint32_t COL_DARK_TXT[4] = { 0x3D404041u, 0x3D606061u, 0x3D808081u, 0x3F800000u };
static const uint32_t COL_PILL_OFF[4] = { 0x3E888889u, 0x3E989899u, 0x3EB0B0B1u, 0x3F800000u };
static const uint32_t COL_GREEN[4]    = { 0x3DA0A0A1u, 0x3F28A8A9u, 0x3EE4E4E5u, 0x3F800000u };
static const uint32_t COL_AMBER[4]    = { 0x3F6E6E6Fu, 0x3F28A8A9u, 0x3DF0F0F1u, 0x3F800000u };
static const uint32_t COL_RED[4]      = { 0x3F646465u, 0x3E686869u, 0x3E888889u, 0x3F800000u };

static const uint32_t COL_ACCENT[3][4] = {
    { 0x3DCCCCCDu, 0x3F266667u, 0x3F6E6E6Fu, 0x3F800000u }, /* 青蓝 */
    { 0x3F6E6E6Fu, 0x3F28A8A9u, 0x3DF0F0F1u, 0x3F800000u }, /* 琥珀 */
    { 0x3DF0F0F1u, 0x3F48C8C9u, 0x3EFAFAFBu, 0x3F800000u }, /* 翠绿 */
};

static const uint16_t OCTANT_BASES[8] = { 0u, 125u, 225u, 325u, 405u, 505u, 585u, 665u };
static const char *const SD_CFG_PATHS[2] = { "A:\\S5_CC.CFG", "B:\\S5_CC.CFG" };
static const char *const SD_LIST_PATHS[2] = { "A:\\LUT\\LIST.TXT", "B:\\LUT\\LIST.TXT" };

/* D-Cache maintenance (32B line stride) for ISP DMA, SDMMC DMA, and high-VA hook.c coherency. */
static void cc_dcache_range(uint32_t addr, uint32_t bytes, int clean_only) {
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

static inline char cc_toupper(char c) {
    return (c >= 'a' && c <= 'z') ? (char)(c - 32) : c;
}

static int cc_starts_ci(const char *s, const char *kw) {
    while (*kw) {
        if (cc_toupper(*s++) != *kw++)
            return 0;
    }
    return 1;
}

static const char *cc_skip_bom_ws(const char *p) {
    const uint8_t *u = (const uint8_t *)p;
    if (u[0] == 0xEFu && u[1] == 0xBBu && u[2] == 0xBFu)
        p += 3;
    while (*p == ' ' || *p == '\t')
        ++p;
    return p;
}

static void cc_strcpy_n(char *dst, const char *src, uint32_t max_len) {
    uint32_t i = 0;
    while (i + 1u < max_len && src[i]) {
        dst[i] = src[i];
        ++i;
    }
    dst[i] = '\0';
}

static int cc_streq_ci(const char *a, const char *b) {
    while (*a && *b) {
        if (cc_toupper(*a) != cc_toupper(*b))
            return 0;
        ++a;
        ++b;
    }
    return *a == *b;
}

static void cc_strip_ext(char *dst, const char *fname, uint32_t max_len) {
    uint32_t len = 0, dot = 0;
    while (fname[len]) {
        if (fname[len] == '.')
            dot = len;
        ++len;
    }
    uint32_t cut = (dot > 0u) ? dot : len;
    if (cut + 1u > max_len)
        cut = max_len - 1u;
    for (uint32_t i = 0; i < cut; ++i)
        dst[i] = fname[i];
    dst[cut] = '\0';
}

static void cc_build_lut_path(char *out, uint8_t drv_idx, const char *fname) {
    out[0] = (drv_idx == 0u) ? 'A' : 'B';
    out[1] = ':';
    out[2] = '\\';
    out[3] = 'L';
    out[4] = 'U';
    out[5] = 'T';
    out[6] = '\\';
    cc_strcpy_n(out + 7, fname, 48u);
}

static void cc_fmt_num3(char *dst, uint32_t n) {
    dst[0] = (char)('0' + (n / 100u) % 10u);
    dst[1] = (char)('0' + (n / 10u) % 10u);
    dst[2] = (char)('0' + (n % 10u));
}

static void cc_fmt_uint(char *dst, uint32_t n) {
    char tmp[12];
    uint32_t k = 0;
    if (n == 0u) {
        dst[0] = '0';
        dst[1] = '\0';
        return;
    }
    while (n > 0u && k < 10u) {
        tmp[k++] = (char)('0' + (n % 10u));
        n /= 10u;
    }
    for (uint32_t i = 0; i < k; ++i)
        dst[i] = tmp[k - 1u - i];
    dst[k] = '\0';
}

static int cc_has_ext_ci(const char *fname, const char *ext) {
    uint32_t fl = 0u, el = 0u;
    while (fname[fl]) ++fl;
    while (ext[el]) ++el;
    return (fl >= el) ? cc_streq_ci(fname + fl - el, ext) : 0;
}

static inline volatile CcState *cc_get_state(void) {
    volatile CcState *st = (volatile CcState *)(uintptr_t)CC_STATE_ADDR;
    if (st->magic != CC_MAGIC) {
        st->visible = 0u;
        st->cursor = 0u;
        st->lut_all_styles = 0u;
        st->beep_muted = 0u;
        st->theme = 0u;
        st->sd_lut_drive = 0u;
        st->sd_status = SD_STAT_NONE;
        st->sd_loaded_once = 0u;
        st->sd_lut_active = 0u;
        st->sd_lut_count = 0u;
        st->sd_lut_err = 0u;
        st->sd_scanned = 0u;
        st->saved_beep_cnt = *(const volatile uint32_t *)(uintptr_t)ADDR_BEEP_ID1_CNT;
        st->saved_slot1_ptr = ((const volatile uint32_t *)(uintptr_t)ADDR_LUT_SLOT_PTRS)[1];
        st->open_count = 0u;
        st->active_label[0] = '\0';
        st->lut_hotkey = LUT_HK_BAR;
        st->boot_checked = 0u;
        __asm__ volatile("dmb sy" ::: "memory");
        st->magic = CC_MAGIC;
        cc_dcache_range(CC_STATE_ADDR, sizeof(CcState), 1);
    }
    return st;
}

static int cc_is_builtin_populated(volatile CcState *st, uint32_t slot) {
    if (slot < 1u || slot > 4u)
        return 0;
    const volatile uint32_t *slot_ptrs = (const volatile uint32_t *)(uintptr_t)ADDR_LUT_SLOT_PTRS;
    uint32_t ptr = slot_ptrs[slot];
    if (slot == 1u && ptr == CC_SD_LUT_BUF_ADDR)
        ptr = st->saved_slot1_ptr;
    if (ptr < 0x06000000u || ptr >= 0xC0000000u || (ptr & 3u))
        return 0;
    const volatile uint16_t *hdr = (const volatile uint16_t *)(uintptr_t)ptr;
    return (hdr[0] == 0xBEEFu && hdr[1] == 1u) ? 1 : 0;
}

static uint32_t cc_get_builtin_slot(void) {
    uint8_t va_en = *(const volatile uint8_t *)(uintptr_t)ADDR_LCD_VA_EN_U8;
    uint32_t s = *(const volatile uint8_t *)(uintptr_t)ADDR_LUT_SLOT_U8;
    if (s == 0u || s > 4u)
        return 0u;
    const volatile uint32_t *slot_ptrs = (const volatile uint32_t *)(uintptr_t)ADDR_LUT_SLOT_PTRS;
    uint32_t ptr = slot_ptrs[s];
    if (ptr < 0x06000000u || ptr >= 0xC0000000u || (ptr & 3u))
        return 0u;
    const volatile uint16_t *hdr = (const volatile uint16_t *)(uintptr_t)ptr;
    if (hdr[0] != 0xBEEFu || hdr[1] != 1u)
        return 0u;
    (void)va_en;
    return s;
}

/* Hooked at 0x066AE7C4 (replaces BL 0x066A07F4) so Live View 3D-LUT stays active in
 * V-Log (0x0A/0x0B), My Photo Style 1..10 (0x1E..0x27), and All Styles when enabled! */
int cc_lv_style_gate(uint32_t style) {
    if (style == 0x0Au || style == 0x0Bu)
        return 1;
    const volatile CcState *st = (const volatile CcState *)(uintptr_t)CC_STATE_ADDR;
    if (st->magic != CC_MAGIC)
        return 0;
    if (st->lut_all_styles)
        return 1;
    if ((st->sd_lut_active != 0u || *(const volatile uint8_t *)(uintptr_t)ADDR_LUT_SLOT_U8 != 0u) &&
        style >= 0x1Eu && style <= 0x27u)
        return 1;
    return 0;
}

/* Push LUT change directly to Panasonic's Live View ISP 3D-LUT hardware engine! */
static void cc_sync_liveview_isp(volatile CcState *st, uint32_t slot) {
    cc_dcache_range(CC_STATE_ADDR, sizeof(CcState), 1);

    uint8_t style = *(const volatile uint8_t *)(uintptr_t)ADDR_PHOTO_STYLE;
    int style_ok = st->lut_all_styles || (style == 0x0Au) || (style == 0x0Bu) ||
                   (style >= 0x1Eu && style <= 0x27u);

    /* Never enable ISP 3D-LUT on an unpopulated 0x0000 buffer (prevents black screen)! */
    int buf_ok = 0;
    if (slot >= 1u && slot <= 4u) {
        uint32_t ptr = ((const volatile uint32_t *)(uintptr_t)ADDR_LUT_SLOT_PTRS)[slot];
        if (ptr >= 0x06000000u && ptr < 0xC0000000u && !(ptr & 3u)) {
            const volatile uint16_t *hdr = (const volatile uint16_t *)(uintptr_t)ptr;
            if (hdr[0] == 0xBEEFu && hdr[1] == 1u)
                buf_ok = 1;
        }
    }
    uint32_t enable = (buf_ok && style_ok) ? 1u : 0u;

    /* Sync persistent View Assist enable flags read by 0x066AE6EC on shutter half-press! */
    *(volatile uint8_t *)(uintptr_t)ADDR_LCD_VA_EN_U8 = (uint8_t)enable;
    *(volatile uint8_t *)(uintptr_t)ADDR_HDMI_VA_EN_U8 = (uint8_t)enable;
    *(volatile uint32_t *)(uintptr_t)ADDR_LCD_VA_TYPE = enable;
    *(volatile uint32_t *)(uintptr_t)ADDR_HDMI_VA_TYPE = enable;

    /* Force ISP LUT dirty flags so switching SD01 -> SD02 (both on slot 1) always re-uploads! */
    volatile uint8_t *force_dirty = (volatile uint8_t *)(uintptr_t)ADDR_LUT_FORCE_DIRTY;
    force_dirty[0] = 1u;
    force_dirty[1] = 1u;
    *(volatile uint8_t *)(uintptr_t)ADDR_LCD_LUT_DIRTY = 1u;
    *(volatile uint8_t *)(uintptr_t)ADDR_HDMI_LUT_DIRTY = 1u;

    lv_lut_apply_fn p_lv_apply = (lv_lut_apply_fn)(uintptr_t)FN_LV_LUT_APPLY;
    p_lv_apply(enable ? slot : 0u, enable, enable);
}

static void cc_restore_slot1_ptr(volatile CcState *st) {
    volatile uint32_t *slot_ptrs = (volatile uint32_t *)(uintptr_t)ADDR_LUT_SLOT_PTRS;
    if (slot_ptrs[1] == CC_SD_LUT_BUF_ADDR && st->saved_slot1_ptr != 0u)
        slot_ptrs[1] = st->saved_slot1_ptr;
}

static void cc_set_builtin_slot(volatile CcState *st, uint32_t slot) {
    if (slot > 4u)
        slot = 0u;
    cc_restore_slot1_ptr(st);
    st->sd_lut_active = 0u;
    st->sd_lut_err = 0u;
    *(volatile uint8_t *)(uintptr_t)ADDR_LUT_SLOT_U8 = (uint8_t)slot;
    *(volatile uint32_t *)(uintptr_t)ADDR_LUT_SLOT_U32 = slot;
    cc_sync_liveview_isp(st, slot);
}

static void cc_apply_beep_mute(volatile CcState *st, uint8_t mute) {
    volatile uint32_t *beep_cnt = (volatile uint32_t *)(uintptr_t)ADDR_BEEP_ID1_CNT;
    if (mute) {
        if (!st->beep_muted) {
            uint32_t cur = *beep_cnt;
            if (cur != 0u)
                st->saved_beep_cnt = cur;
        }
        *beep_cnt = 0u;
        st->beep_muted = 1u;
    } else {
        if (st->saved_beep_cnt != 0u)
            *beep_cnt = st->saved_beep_cnt;
        st->beep_muted = 0u;
    }
    cc_dcache_range(ADDR_BEEP_ID1_CNT, 32u, 1);
}

static int cc_fs_open_read(const char *path, uint32_t *out_fd) {
    fs_parse_fn p_parse = (fs_parse_fn)(uintptr_t)FN_FS_PARSE_PATH;
    fs_open_fn  p_open  = (fs_open_fn)(uintptr_t)FN_FS_OPEN;
    uint32_t drv = 0u;
    const char *rel = 0;
    uint16_t flag = 0u;
    if (p_parse(path, &drv, &rel, &flag) != 0u)
        return 0;
    return (p_open(drv, out_fd, rel, FS_MODE_READ) == 0u) ? 1 : 0;
}

static uint32_t cc_fs_read_dma(uint32_t fd, char *io, uint32_t len) {
    fs_read_fn p_read = (fs_read_fn)(uintptr_t)FN_FS_READ;
    cc_dcache_range((uint32_t)(uintptr_t)io, len, 0);
    uint32_t stat[2] = { 0u, 0u };
    if (p_read(fd, io, 0u, len, 0u, stat) != 0u)
        return 0u;
    cc_dcache_range((uint32_t)(uintptr_t)io, len, 0);
    return stat[1];
}

static int cc_extract_title(const char *buf, uint32_t n, char *out_label, uint32_t max_len) {
    for (uint32_t i = 0; i + 6u < n; ++i) {
        if (cc_starts_ci(buf + i, "TITLE") && (buf[i + 5] == ' ' || buf[i + 5] == '\t' || buf[i + 5] == '"')) {
            uint32_t j = i + 5u;
            while (j < n && (buf[j] == ' ' || buf[j] == '\t' || buf[j] == '"'))
                ++j;
            uint32_t k = 0;
            while (j < n && k + 1u < max_len && buf[j] != '"' && buf[j] != '\r' && buf[j] != '\n')
                out_label[k++] = buf[j++];
            while (k > 0u && (out_label[k - 1u] == ' ' || out_label[k - 1u] == '\t'))
                --k;
            out_label[k] = '\0';
            return (k > 0u) ? 1 : 0;
        }
    }
    return 0;
}

static void cc_push_list_line(CcLutEntry *list, uint32_t *count, char *line, uint32_t lp) {
    line[lp] = '\0';
    const char *p = cc_skip_bom_ws(line);
    uint32_t len = 0;
    while (p[len]) ++len;
    while (len > 0u && (p[len - 1u] == ' ' || p[len - 1u] == '\t'))
        --len;
    if (len == 0u || p[0] == '#' || p[0] == ';')
        return;
    char clean[44];
    if (len >= sizeof(clean))
        len = sizeof(clean) - 1u;
    for (uint32_t i = 0; i < len; ++i)
        clean[i] = p[i];
    clean[len] = '\0';
    if (*count < CC_MAX_SD_LUTS) {
        cc_strcpy_n(list[*count].fname, clean, 44u);
        cc_strip_ext(list[*count].label, clean, 20u);
        (*count)++;
    }
}

static void cc_scan_sd_luts(volatile CcState *st) {
    fs_close_fn p_close = (fs_close_fn)(uintptr_t)FN_FS_CLOSE;

    CcLutEntry *list = (CcLutEntry *)(uintptr_t)CC_LUT_LIST_ADDR;
    char *io = (char *)(uintptr_t)CC_IO_BUF_ADDR;
    uint32_t count = 0u;
    st->sd_scanned = 1u;

    /* Mode 1: A:\LUT\LIST.TXT or B:\LUT\LIST.TXT */
    for (uint8_t d = 0u; d < 2u; ++d) {
        uint32_t fd = 0u;
        if (!cc_fs_open_read(SD_LIST_PATHS[d], &fd))
            continue;
        st->sd_lut_drive = d;
        char line[64];
        uint32_t lp = 0u;
        while (count < CC_MAX_SD_LUTS) {
            uint32_t got = cc_fs_read_dma(fd, io, 1024u);
            if (got == 0u)
                break;
            for (uint32_t i = 0; i < got && count < CC_MAX_SD_LUTS; ++i) {
                char c = io[i];
                if (c == '\r' || c == '\n') {
                    if (lp > 0u) {
                        cc_push_list_line(list, &count, line, lp);
                        lp = 0u;
                    }
                } else if (lp + 1u < sizeof(line)) {
                    line[lp++] = c;
                }
            }
        }
        if (lp > 0u && count < CC_MAX_SD_LUTS)
            cc_push_list_line(list, &count, line, lp);
        p_close(fd, 0u);
        st->sd_lut_count = (uint8_t)count;
        return;
    }

    /* Mode 2: zero-config numbered probe (001.CUBE / 001.VLT .. 100) on A: then B: */
    for (uint8_t d = 0u; d < 2u; ++d) {
        uint32_t misses = 0u;
        for (uint32_t n = 1u; n <= CC_MAX_SD_LUTS && count < CC_MAX_SD_LUTS; ++n) {
            char fname[16];
            char path[56];
            int found = 0;
            for (uint32_t ext = 0u; ext < 2u && !found; ++ext) {
                cc_fmt_num3(fname, n);
                if (ext == 0u) {
                    fname[3] = '.'; fname[4] = 'C'; fname[5] = 'U'; fname[6] = 'B'; fname[7] = 'E'; fname[8] = '\0';
                } else {
                    fname[3] = '.'; fname[4] = 'V'; fname[5] = 'L'; fname[6] = 'T'; fname[7] = '\0';
                }
                cc_build_lut_path(path, d, fname);
                uint32_t fd = 0u;
                if (cc_fs_open_read(path, &fd)) {
                    uint32_t got = cc_fs_read_dma(fd, io, 256u);
                    p_close(fd, 0u);
                    cc_strcpy_n(list[count].fname, fname, 44u);
                    if (!cc_extract_title(io, got, list[count].label, 20u))
                        cc_strip_ext(list[count].label, fname, 20u);
                    ++count;
                    found = 1;
                    misses = 0u;
                }
            }
            if (!found) {
                ++misses;
                if (misses >= 6u)
                    break;
            }
        }
        if (count > 0u) {
            st->sd_lut_drive = d;
            break;
        }
    }
    st->sd_lut_count = (uint8_t)count;
}

static const char *cc_parse_num(const char *p, int32_t *out_val, int *is_float) {
    while (*p == ' ' || *p == '\t')
        ++p;
    int neg = 0;
    if (*p == '-') { neg = 1; ++p; }
    else if (*p == '+') { ++p; }

    int32_t ipart = 0;
    int digits = 0;
    while (*p >= '0' && *p <= '9') {
        if (ipart < 100000)
            ipart = ipart * 10 + (*p - '0');
        ++p;
        digits = 1;
    }
    if (!digits && *p != '.')
        return 0;

    int32_t fpart = 0;
    int saw_dot = 0;
    if (*p == '.') {
        saw_dot = 1;
        ++p;
        int32_t mul = 1000;
        while (*p >= '0' && *p <= '9') {
            if (mul > 0) {
                fpart += (*p - '0') * mul;
                mul /= 10;
            }
            ++p;
        }
        if (*p == 'e' || *p == 'E') {
            ++p;
            int eneg = 0;
            if (*p == '-') { eneg = 1; ++p; }
            else if (*p == '+') { ++p; }
            int32_t exp = 0;
            while (*p >= '0' && *p <= '9') { exp = exp * 10 + (*p - '0'); ++p; }
            if (eneg && exp >= 4) { ipart = 0; fpart = 0; }
            else if (!eneg && exp > 0) {
                for (int32_t e = 0; e < exp && ipart < 10000; ++e) {
                    ipart = ipart * 10 + (fpart / 1000);
                    fpart = (fpart % 1000) * 10;
                }
            }
        }
    }
    *is_float = saw_dot;
    int32_t val = saw_dot ? (ipart * 10000 + fpart) : ipart;
    *out_val = neg ? -val : val;
    return p;
}

static uint32_t cc_val_to_6bit(int32_t val, int is_float, int is_vlt) {
    if (val <= 0)
        return 0u;
    if (is_vlt && !is_float) {
        if (val >= 4095)
            return 63u;
        return (uint32_t)((val * 63 + 2047) / 4095);
    }
    if (!is_float)
        return (val >= 1) ? 63u : 0u;
    if (val >= 10000)
        return 63u;
    return (uint32_t)((val * 63 + 5000) / 10000);
}

static void cc_pack_beef9_entry(uint16_t *dst_body, uint32_t r9, uint32_t g9, uint32_t b9,
                                uint32_t r6, uint32_t g6, uint32_t b6) {
    const uint16_t *oct_map = (const uint16_t *)(uintptr_t)ADDR_OCTANT_MAP;
    uint32_t map_idx = (g9 * 81u + b9 * 9u + r9) * 2u;
    uint32_t tnum = oct_map[map_idx];
    uint32_t idx  = oct_map[map_idx + 1u];
    uint32_t slot = OCTANT_BASES[tnum & 7u] + idx;
    uint16_t w0 = (uint16_t)((r6 & 0x3Fu) | ((b6 & 0x3Fu) << 6) | ((g6 & 0x0Fu) << 12));
    uint16_t w1 = (uint16_t)((g6 >> 4) & 0x03u);
    dst_body[slot * 2u] = w0;
    dst_body[slot * 2u + 1u] = w1;
}

typedef struct {
    uint16_t *body;
    uint32_t lut_n;
    uint32_t lut_1d_skip;
    int      is_vlt;
    int      samp_ready;
    uint8_t  samp_map[66];
    uint32_t triplet_idx;
    uint32_t packed_cnt;
} CcParseCtx;

static void cc_parse_lut_line(CcParseCtx *ctx, const char *raw_line) {
    const char *p = cc_skip_bom_ws(raw_line);
    if (*p == '\0' || *p == '#')
        return;

    if (cc_starts_ci(p, "VLT_")) {
        ctx->is_vlt = 1;
        ctx->lut_n = 17u;
        return;
    }
    if (cc_starts_ci(p, "LUT_1D_SIZE")) {
        const char *q = p + 11;
        int32_t nv = 0;
        int fl = 0;
        if (cc_parse_num(q, &nv, &fl) && nv > 0 && nv <= 65536)
            ctx->lut_1d_skip = (uint32_t)nv;
        return;
    }
    if (cc_starts_ci(p, "LUT_3D")) {
        const char *q = p;
        while (*q && *q != ' ' && *q != '\t') ++q;
        int32_t nv = 0;
        int fl = 0;
        if (cc_parse_num(q, &nv, &fl) && nv >= 9 && nv <= 65)
            ctx->lut_n = (uint32_t)nv;
        return;
    }
    if (!((*p >= '0' && *p <= '9') || *p == '.' || *p == '-' || *p == '+'))
        return;

    if (ctx->lut_1d_skip > 0u) {
        ctx->lut_1d_skip--;
        return;
    }

    if (!ctx->samp_ready) {
        for (uint32_t k = 0; k < 66u; ++k)
            ctx->samp_map[k] = 0xFFu;
        for (uint32_t g = 0; g < 9u; ++g) {
            uint32_t coord = (g * (ctx->lut_n - 1u) + 4u) / 8u;
            if (coord < 66u)
                ctx->samp_map[coord] = (uint8_t)g;
        }
        ctx->samp_ready = 1;
    }

    uint32_t n2 = ctx->lut_n * ctx->lut_n;
    if (ctx->triplet_idx >= n2 * ctx->lut_n)
        return;
    uint32_t b = ctx->triplet_idx / n2;
    uint32_t rem = ctx->triplet_idx - b * n2;
    uint32_t g = rem / ctx->lut_n;
    uint32_t r = rem - g * ctx->lut_n;
    ctx->triplet_idx++;

    if (r < 66u && g < 66u && b < 66u) {
        uint8_t r9 = ctx->samp_map[r];
        uint8_t g9 = ctx->samp_map[g];
        uint8_t b9 = ctx->samp_map[b];
        if (r9 < 9u && g9 < 9u && b9 < 9u) {
            int32_t vr = 0, vg = 0, vb = 0;
            int fr = 0, fg = 0, fb = 0;
            const char *p1 = cc_parse_num(p, &vr, &fr);
            const char *p2 = p1 ? cc_parse_num(p1, &vg, &fg) : 0;
            const char *p3 = p2 ? cc_parse_num(p2, &vb, &fb) : 0;
            if (p3) {
                if (!fr && (vr > 1 || vg > 1 || vb > 1))
                    ctx->is_vlt = 1;
                uint32_t r6 = cc_val_to_6bit(vr, fr, ctx->is_vlt);
                uint32_t g6 = cc_val_to_6bit(vg, fg, ctx->is_vlt);
                uint32_t b6 = cc_val_to_6bit(vb, fb, ctx->is_vlt);
                cc_pack_beef9_entry(ctx->body, r9, g9, b9, r6, g6, b6);
                ctx->packed_cnt++;
            }
        }
    }
}

static int cc_load_sd_lut(volatile CcState *st, uint8_t sd_idx) {
    if (sd_idx < 1u || sd_idx > st->sd_lut_count)
        return 0;

    fs_close_fn p_close = (fs_close_fn)(uintptr_t)FN_FS_CLOSE;

    const CcLutEntry *ent = &((const CcLutEntry *)(uintptr_t)CC_LUT_LIST_ADDR)[sd_idx - 1u];
    char path[56];
    cc_build_lut_path(path, st->sd_lut_drive, ent->fname);

    uint32_t fd = 0u;
    if (!cc_fs_open_read(path, &fd)) {
        st->sd_lut_err = 1u;
        return 0;
    }

    uint16_t *beef = (uint16_t *)(uintptr_t)CC_SD_LUT_BUF_ADDR;
    char *io = (char *)(uintptr_t)CC_IO_BUF_ADDR;

    CcParseCtx ctx;
    ctx.body = beef + 2;
    ctx.lut_n = 17u;
    ctx.lut_1d_skip = 0u;
    ctx.is_vlt = cc_has_ext_ci(ent->fname, ".VLT");
    ctx.samp_ready = 0;
    ctx.triplet_idx = 0u;
    ctx.packed_cnt = 0u;

    char line[112];
    uint32_t lp = 0u;

    for (;;) {
        uint32_t got = cc_fs_read_dma(fd, io, 2048u);
        if (got == 0u)
            break;
        for (uint32_t i = 0; i < got; ++i) {
            char c = io[i];
            if (c != '\r' && c != '\n') {
                if (lp + 1u < sizeof(line))
                    line[lp++] = c;
                continue;
            }
            if (lp > 0u) {
                line[lp] = '\0';
                lp = 0u;
                cc_parse_lut_line(&ctx, line);
            }
        }
    }
    /* Flush final line even if file has no trailing newline at EOF! */
    if (lp > 0u) {
        line[lp] = '\0';
        cc_parse_lut_line(&ctx, line);
    }
    p_close(fd, 0u);

    if (ctx.packed_cnt < 729u) {
        st->sd_lut_err = 1u;
        return 0;
    }

    beef[0] = 0xBEEFu;
    beef[1] = 1u;
    cc_dcache_range(CC_SD_LUT_BUF_ADDR, 2944u, 1);

    cc_strcpy_n((char *)st->active_label, ent->label, 20u);
    __asm__ volatile("dmb sy" ::: "memory");
    st->sd_lut_err = 0u;
    st->sd_lut_active = sd_idx;

    /* Hot-swap Slot 1 pointer for Live View + sync ISP 3D-LUT hardware immediately! */
    volatile uint32_t *slot_ptrs = (volatile uint32_t *)(uintptr_t)ADDR_LUT_SLOT_PTRS;
    if (slot_ptrs[1] != CC_SD_LUT_BUF_ADDR && slot_ptrs[1] != 0u)
        st->saved_slot1_ptr = slot_ptrs[1];
    slot_ptrs[1] = CC_SD_LUT_BUF_ADDR;
    *(volatile uint8_t *)(uintptr_t)ADDR_LUT_SLOT_U8 = 1u;
    *(volatile uint32_t *)(uintptr_t)ADDR_LUT_SLOT_U32 = 1u;
    cc_sync_liveview_isp(st, 1u);
    return 1;
}

static uint32_t cc_cfg_checksum(const CcDiskCfg *cfg) {
    const uint32_t *w = (const uint32_t *)cfg;
    uint32_t sum = 0x5A5A1234u;
    for (uint32_t i = 0; i < (sizeof(CcDiskCfg) / 4u) - 1u; ++i)
        sum = ((sum << 5) | (sum >> 27)) ^ w[i];
    return sum;
}

static int cc_sd_load_cfg_path(const char *path, CcDiskCfg *out_cfg) {
    fs_close_fn p_close = (fs_close_fn)(uintptr_t)FN_FS_CLOSE;

    uint32_t fd = 0u;
    if (!cc_fs_open_read(path, &fd))
        return 0;

    char *io = (char *)(uintptr_t)CC_IO_BUF_ADDR;
    uint32_t got = cc_fs_read_dma(fd, io, sizeof(CcDiskCfg));
    p_close(fd, 0u);
    if (got != sizeof(CcDiskCfg))
        return 0;

    const uint8_t *src = (const uint8_t *)io;
    uint8_t *dst = (uint8_t *)out_cfg;
    for (uint32_t i = 0; i < sizeof(CcDiskCfg); ++i)
        dst[i] = src[i];

    if (out_cfg->magic != CC_CFG_MAGIC || out_cfg->version != CC_CFG_VERSION)
        return 0;
    if (out_cfg->checksum != cc_cfg_checksum(out_cfg))
        return 0;
    return 1;
}

static int cc_sd_load(volatile CcState *st) {
    CcDiskCfg cfg;
    for (uint32_t i = 0; i < 2u; ++i) {
        if (cc_sd_load_cfg_path(SD_CFG_PATHS[i], &cfg)) {
            st->lut_all_styles = cfg.lut_all_styles ? 1u : 0u;
            cc_apply_beep_mute(st, cfg.beep_muted ? 1u : 0u);
            st->theme = (cfg.theme < 3u) ? cfg.theme : 0u;
            st->lut_hotkey = (cfg.lut_hotkey <= LUT_HK_PANEL) ? cfg.lut_hotkey : LUT_HK_BAR;

            if (cfg.sd_lut_active > 0u && st->sd_lut_count > 0u) {
                uint8_t match = 0u;
                const CcLutEntry *list = (const CcLutEntry *)(uintptr_t)CC_LUT_LIST_ADDR;
                if (cfg.sd_fname[0]) {
                    for (uint8_t k = 0; k < st->sd_lut_count; ++k) {
                        if (cc_streq_ci(list[k].fname, cfg.sd_fname)) {
                            match = (uint8_t)(k + 1u);
                            break;
                        }
                    }
                }
                if (match == 0u && cfg.sd_lut_active <= st->sd_lut_count)
                    match = cfg.sd_lut_active;
                if (match > 0u && cc_load_sd_lut(st, match)) {
                    /* loaded cleanly */
                } else {
                    cc_set_builtin_slot(st, cfg.lut_slot <= 4u ? cfg.lut_slot : 0u);
                }
            } else {
                cc_set_builtin_slot(st, cfg.lut_slot <= 4u ? cfg.lut_slot : 0u);
            }

            st->sd_status = SD_STAT_LOADED;
            st->sd_loaded_once = 1u;
            cc_dcache_range(CC_STATE_ADDR, sizeof(CcState), 1);
            return 1;
        }
    }
    return 0;
}

static int cc_sd_save_path(const char *path, const CcDiskCfg *cfg) {
    fs_parse_fn p_parse = (fs_parse_fn)(uintptr_t)FN_FS_PARSE_PATH;
    fs_open_fn  p_open  = (fs_open_fn)(uintptr_t)FN_FS_OPEN;
    fs_seek_fn  p_seek  = (fs_seek_fn)(uintptr_t)FN_FS_SEEK;
    fs_write_fn p_write = (fs_write_fn)(uintptr_t)FN_FS_WRITE;
    fs_close_fn p_close = (fs_close_fn)(uintptr_t)FN_FS_CLOSE;

    uint32_t drv = 0u, fd = 0u;
    const char *rel = 0;
    uint16_t flag = 0u;
    if (p_parse(path, &drv, &rel, &flag) != 0u)
        return 0;

    uint32_t r = p_open(drv, &fd, rel, FS_MODE_CREATE);
    if (r == FS_ERR_EXIST)
        r = p_open(drv, &fd, rel, FS_MODE_TRUNC);
    if (r != 0u)
        return 0;

    /* Copy to 32B-aligned DMA-safe static buffer and clean D-Cache before SDMMC write */
    uint8_t *io = (uint8_t *)(uintptr_t)CC_IO_BUF_ADDR;
    const uint8_t *src = (const uint8_t *)cfg;
    for (uint32_t i = 0; i < sizeof(CcDiskCfg); ++i)
        io[i] = src[i];
    cc_dcache_range(CC_IO_BUF_ADDR, sizeof(CcDiskCfg), 1);

    p_seek(fd, 0u, 1u);
    uint32_t stat[2] = { 0u, 0u };
    r = p_write(fd, io, 0u, sizeof(CcDiskCfg), 0u, stat);
    p_close(fd, 0u);
    return (r == 0u && stat[1] == sizeof(CcDiskCfg)) ? 1 : 0;
}

static int cc_sd_save(volatile CcState *st) {
    CcDiskCfg cfg;
    cfg.magic = CC_CFG_MAGIC;
    cfg.version = CC_CFG_VERSION;
    cfg.lut_slot = (uint8_t)(st->sd_lut_active ? 0u : cc_get_builtin_slot());
    cfg.sd_lut_active = st->sd_lut_active;
    cfg.lut_all_styles = st->lut_all_styles;
    cfg.beep_muted = st->beep_muted;
    cfg.theme = st->theme;
    cfg.lut_hotkey = st->lut_hotkey;
    cfg.pad[0] = 0u; cfg.pad[1] = 0u;
    for (uint32_t i = 0; i < 44u; ++i) cfg.sd_fname[i] = '\0';
    if (st->sd_lut_active >= 1u && st->sd_lut_active <= st->sd_lut_count) {
        const CcLutEntry *ent = &((const CcLutEntry *)(uintptr_t)CC_LUT_LIST_ADDR)[st->sd_lut_active - 1u];
        cc_strcpy_n(cfg.sd_fname, ent->fname, 44u);
    }
    cfg.reserved[0] = 0u; cfg.reserved[1] = 0u; cfg.reserved[2] = 0u; cfg.reserved[3] = 0u;
    cfg.checksum = cc_cfg_checksum(&cfg);

    for (uint32_t i = 0; i < 2u; ++i) {
        if (cc_sd_save_path(SD_CFG_PATHS[i], &cfg)) {
            st->sd_status = SD_STAT_SAVED;
            st->sd_loaded_once = 1u;
            cc_dcache_range(CC_STATE_ADDR, sizeof(CcState), 1);
            return 1;
        }
    }
    st->sd_status = SD_STAT_SAVE_FAILED;
    return 0;
}

static int cc_next_valid_idx(volatile CcState *st, int cur, int dir) {
    int total = 5 + (int)st->sd_lut_count;
    int step = (dir < 0) ? -1 : 1;
    for (int tries = 0; tries < total; ++tries) {
        cur += step;
        while (cur < 0) cur += total;
        while (cur >= total) cur -= total;
        if (cur == 0 || cur > 4 || cc_is_builtin_populated(st, (uint32_t)cur))
            return cur;
    }
    return 0;
}

static void cc_step_lut(volatile CcState *st, int delta) {
    int cur = st->sd_lut_active ? (4 + (int)st->sd_lut_active) : (int)cc_get_builtin_slot();
    int steps = (delta < 0) ? -delta : delta;
    int dir = (delta < 0) ? -1 : 1;
    if (steps == 0)
        steps = 1;
    for (int i = 0; i < steps; ++i)
        cur = cc_next_valid_idx(st, cur, dir);

    if (cur <= 4) {
        cc_set_builtin_slot(st, (uint32_t)cur);
    } else {
        cc_load_sd_lut(st, (uint8_t)(cur - 4));
    }
    st->sd_status = SD_STAT_DIRTY;
}

static void cc_adjust_item(volatile CcState *st, int dir, int is_set) {
    switch (st->cursor) {
    case 0:
        cc_step_lut(st, (is_set && st->sd_lut_count >= 10u) ? 10 : dir);
        break;
    case 1: {
        int m = (int)st->lut_hotkey + dir;
        if (m < 0) m = 2;
        if (m > 2) m = 0;
        st->lut_hotkey = (uint8_t)m;
        st->sd_status = SD_STAT_DIRTY;
        break;
    }
    case 2:
        st->lut_all_styles = st->lut_all_styles ? 0u : 1u;
        st->sd_status = SD_STAT_DIRTY;
        cc_sync_liveview_isp(st, st->sd_lut_active ? 1u : cc_get_builtin_slot());
        break;
    case 3:
        cc_apply_beep_mute(st, st->beep_muted ? 0u : 1u);
        st->sd_status = SD_STAT_DIRTY;
        break;
    case 4: {
        int t = (int)st->theme + dir;
        if (t < 0) t = 2;
        if (t > 2) t = 0;
        st->theme = (uint8_t)t;
        st->sd_status = SD_STAT_DIRTY;
        break;
    }
    case 5:
        if (!is_set)
            return;
        cc_set_builtin_slot(st, 0u);
        st->lut_hotkey = LUT_HK_BAR;
        st->lut_all_styles = 0u;
        cc_apply_beep_mute(st, 0u);
        st->theme = 0u;
        st->sd_status = SD_STAT_DIRTY;
        cc_dcache_range(CC_STATE_ADDR, sizeof(CcState), 1);
        break;
    case 6:
        if (!is_set)
            return;
        if (cc_sd_save(st))
            st->visible = CC_VIS_OFF;
        break;
    default:
        break;
    }
}

/* Called directly from Setup Menu item 0x01B5 (像素更新 -> 控制中心) @ 0x068596AE. */
void cc_open_from_setup_menu(void) {
    volatile CcState *st = cc_get_state();
    st->visible = CC_VIS_OFF;
    __asm__ volatile("dmb sy" ::: "memory");
    st->boot_checked = 1u;
    cc_scan_sd_luts(st);
    if (!st->sd_loaded_once)
        cc_sd_load(st);
    st->cursor = 0u;
    st->open_count++;
    __asm__ volatile("dmb sy" ::: "memory");
    st->visible = CC_VIS_PANEL;
    cc_dcache_range(CC_STATE_ADDR, sizeof(CcState), 1);
}

int cc_fn_lut_select_handler(void) {
    volatile CcState *st = cc_get_state();
    if (st->visible != CC_VIS_OFF) {
        st->visible = CC_VIS_OFF;
        cc_dcache_range(CC_STATE_ADDR, sizeof(CcState), 1);
        return 1;
    }
    st->boot_checked = 1u;
    if (!st->sd_scanned)
        cc_scan_sd_luts(st);
    if (!st->sd_loaded_once)
        cc_sd_load(st);

    if (st->lut_hotkey == LUT_HK_STOCK)
        return 0;
    st->cursor = 0u;
    st->open_count++;
    __asm__ volatile("dmb sy" ::: "memory");
    st->visible = (st->lut_hotkey == LUT_HK_PANEL) ? CC_VIS_PANEL : CC_VIS_LUT_BAR;
    cc_dcache_range(CC_STATE_ADDR, sizeof(CcState), 1);
    return 1;
}

__attribute__((naked)) void cc_fn_lut_trampoline(void) {
    __asm__ volatile(
        "push {r0-r3, r4, lr}\n\t"
        "bl cc_fn_lut_select_handler\n\t"
        "cmp r0, #0\n\t"
        "pop {r0-r3, r4, lr}\n\t"
        "beq 1f\n\t"
        "ldr pc, =0x0686A0EB\n\t"
        "1:\n\t"
        "ldr ip, =0x06465A03\n\t"
        "blx ip\n\t"
        "ldr pc, =0x0686A079\n\t"
        ".ltorg\n\t"
    );
}

static void cc_boot_autoload_once(volatile CcState *st) {
    st->boot_checked = 1u;
    if (st->sd_loaded_once)
        return;
    fs_close_fn p_close = (fs_close_fn)(uintptr_t)FN_FS_CLOSE;
    for (uint32_t i = 0; i < 2u; ++i) {
        uint32_t fd = 0u;
        if (cc_fs_open_read(SD_CFG_PATHS[i], &fd)) {
            p_close(fd, 0u);
            if (!st->sd_scanned)
                cc_scan_sd_luts(st);
            cc_sd_load(st);
            return;
        }
    }
}

void cc_key_hook(uint32_t *ev) {
    key_dispatch_fn orig_dispatch = (key_dispatch_fn)(uintptr_t)FN_KEY_DISPATCH;
    if (!ev) {
        orig_dispatch(ev);
        return;
    }
    volatile CcState *st = cc_get_state();
    if (!st->boot_checked && ev[1] == 1u)
        cc_boot_autoload_once(st);

    if (st->visible == CC_VIS_OFF) {
        orig_dispatch(ev);
        return;
    }

    uint32_t key = ev[0];
    uint32_t action = ev[1];

    if (action == 1u) {
        if (st->visible == CC_VIS_LUT_BAR) {
            if (key == KEY_LEFT) {
                cc_step_lut(st, -1);
            } else if (key == KEY_RIGHT) {
                cc_step_lut(st, +1);
            } else if (key == KEY_UP) {
                cc_step_lut(st, (st->sd_lut_count >= 10u) ? -10 : -1);
            } else if (key == KEY_DOWN) {
                cc_step_lut(st, (st->sd_lut_count >= 10u) ? +10 : +1);
            } else if (key == KEY_CENTER || key == KEY_SET || key == KEY_Q_MENU || key == KEY_PLAY) {
                st->visible = CC_VIS_OFF;
            }
        } else {
            if (key == KEY_UP) {
                st->cursor = (st->cursor == 0u) ? (CC_ITEM_COUNT - 1u) : (st->cursor - 1u);
            } else if (key == KEY_DOWN) {
                st->cursor = (st->cursor + 1u >= CC_ITEM_COUNT) ? 0u : (st->cursor + 1u);
            } else if (key == KEY_LEFT) {
                cc_adjust_item(st, -1, 0);
            } else if (key == KEY_RIGHT) {
                cc_adjust_item(st, +1, 0);
            } else if (key == KEY_CENTER || key == KEY_SET) {
                cc_adjust_item(st, +1, 1);
            } else if (key == KEY_Q_MENU || key == KEY_PLAY) {
                st->visible = CC_VIS_OFF;
            }
        }
        /* Request immediate GUI frame redraw so even static Setup Menu refreshes instantly! */
        commit_ui_fn p_commit = (commit_ui_fn)(uintptr_t)FN_COMMIT_UI;
        p_commit();
    }
}

#ifdef S5_MOD_BUILD
/* Native event 0x0F000002 packs key in low 16 bits, action in high 16:
 * 0=press, 1=repeat, 2=release. Firmware 0x0663EB5A maps raw left/right/up/down
 * to GUI 3/4/5/6 (software indices are 5/6/3/4). Bank +0x24 joystick input
 * uses the same GUI codes, or 0x5E..0x61 in the native special UI mode.
 * Preserve native raw-state processing; consume only the resulting UI input.
 */
static uint32_t cc_captured_gui_keys[4];

int cc_ui_event_hook(uint32_t event, uint32_t param, uint32_t extra) {
    (void)extra;
    volatile CcState *st = cc_get_state();
    if (event == 0x0F000010u)
        return st->visible != CC_VIS_OFF; /* Native auxiliary input; no background action. */
    if (event != 0x0F000002u)
        return 0; /* Redraw, timers and lifecycle events must keep flowing. */

    uint32_t gui_key = param & 0xFFFFu;
    uint32_t action = param >> 16;
    uint32_t mask = 1u << (gui_key & 31u);
    uint32_t *captured = gui_key < 128u ? &cc_captured_gui_keys[gui_key >> 5] : 0;
    if (st->visible == CC_VIS_OFF) {
        /* A new press belongs to stock UI. Some native keys emit no release,
         * so never leave a key blocked across a subsequent distinct press. */
        if (action == 0u) {
            if (captured) *captured &= ~mask;
            return 0;
        }
        if (captured && (*captured & mask)) {
            if (action == 2u) *captured &= ~mask;
            return 1; /* Consume the closing press's repeat/release tail. */
        }
        return 0;
    }

    if (captured) {
        if (action == 2u) *captured &= ~mask;
        else *captured |= mask;
    }
    if (action > 1u)
        return 1;

    uint32_t key;
    switch (gui_key) {
    case 3u: case 0x5Eu: key = KEY_LEFT; break;
    case 4u: case 0x5Fu: key = KEY_RIGHT; break;
    case 5u: case 0x60u: key = KEY_UP; break;
    case 6u: case 0x61u: key = KEY_DOWN; break;
    case 7u: case 0x48u: key = KEY_CENTER; break;
    case 8u: case 10u: key = KEY_PLAY; break; /* Playback or Delete/Back closes only. */
    default: return 1; /* Other keys cannot operate the menu under the panel. */
    }
    if (action == 1u && key != KEY_LEFT && key != KEY_RIGHT && key != KEY_UP && key != KEY_DOWN)
        return 1; /* Holding confirm/exit must not repeatedly activate items. */

    uint32_t ev[2] = { key, 1u };
    cc_key_hook(ev); /* Shared panel actions, with explicitly translated semantics. */
    return 1;
}
#endif

static void cc_rect(int32_t surf_h, int32_t x, int32_t y_top, int32_t w, int32_t h, const uint32_t *col) {
    if (w <= 0 || h <= 0)
        return;
    vg_setfv_fn p_setfv = (vg_setfv_fn)(uintptr_t)FN_VG_SETFV;
    vg_clear_fn p_clear = (vg_clear_fn)(uintptr_t)FN_VG_CLEAR;
    p_setfv(VG_CLEAR_COLOR, 4, col);
    p_clear(x, surf_h - y_top - h, w, h);
}

static const uint32_t *cc_find_zh(uint16_t cp) {
    uint32_t lo = 0u, hi = CC_ZH_COUNT;
    while (lo < hi) {
        uint32_t mid = (lo + hi) >> 1;
        uint16_t m_cp = CC_ZH_GLYPHS[mid].cp;
        if (m_cp == cp)
            return CC_ZH_GLYPHS[mid].rows;
        if (m_cp < cp)
            lo = mid + 1u;
        else
            hi = mid;
    }
    return 0;
}

static void cc_draw_bits_row(vg_clear_fn p_clear, int32_t surf_h, int32_t cx, int32_t ry_top,
                             uint32_t bits, int32_t gw, int32_t scale) {
    int32_t c = 0;
    while (c < gw) {
        while (c < gw && !(bits & (1u << (gw - 1 - c))))
            ++c;
        if (c >= gw)
            break;
        int32_t start = c;
        while (c < gw && (bits & (1u << (gw - 1 - c))))
            ++c;
        p_clear(cx + start * scale, surf_h - ry_top - scale, (c - start) * scale, scale);
    }
}

static void cc_text(int32_t surf_h, int32_t x, int32_t y_top, int32_t scale,
                    const char *s, const uint32_t *col) {
    vg_setfv_fn p_setfv = (vg_setfv_fn)(uintptr_t)FN_VG_SETFV;
    vg_clear_fn p_clear = (vg_clear_fn)(uintptr_t)FN_VG_CLEAR;
    p_setfv(VG_CLEAR_COLOR, 4, col);

    const uint8_t *p = (const uint8_t *)s;
    int32_t cx = x;
    while (*p) {
        uint32_t cp;
        if (p[0] < 0x80u) {
            cp = p[0];
            p += 1;
        } else if ((p[0] & 0xE0u) == 0xC0u && p[1]) {
            cp = ((uint32_t)(p[0] & 0x1Fu) << 6) | (p[1] & 0x3Fu);
            p += 2;
        } else if ((p[0] & 0xF0u) == 0xE0u && p[1] && p[2]) {
            cp = ((uint32_t)(p[0] & 0x0Fu) << 12) | ((uint32_t)(p[1] & 0x3Fu) << 6) | (p[2] & 0x3Fu);
            p += 3;
        } else {
            p += 1;
            continue;
        }

        if (cp >= 0x20u && cp <= 0x7Eu) {
            const uint16_t *rows = CC_ASCII_GLYPHS[cp - 0x20u];
            for (int32_t r = 0; r < CC_FONT_H; ++r) {
                uint32_t bits = rows[r];
                if (bits)
                    cc_draw_bits_row(p_clear, surf_h, cx, y_top + r * scale, bits, CC_FONT_ASCII_W, scale);
            }
            cx += CC_FONT_ASCII_W * scale;
        } else if (cp >= 0x80u && cp <= 0xFFFFu) {
            const uint32_t *rows = cc_find_zh((uint16_t)cp);
            if (rows) {
                for (int32_t r = 0; r < CC_FONT_H; ++r) {
                    uint32_t bits = rows[r];
                    if (bits)
                        cc_draw_bits_row(p_clear, surf_h, cx, y_top + r * scale, bits, CC_FONT_CJK_W, scale);
                }
            }
            cx += (CC_FONT_CJK_W + 1) * scale;
        }
    }
}

static void cc_render_overlay(volatile CcState *st, int32_t surf_w, int32_t surf_h) {
    const uint32_t *accent = COL_ACCENT[st->theme < 3u ? st->theme : 0u];

    int32_t pw = (surf_w >= 860) ? 840 : (surf_w - 40);
    int32_t ph = (surf_h >= 560) ? 544 : (surf_h - 40);
    int32_t px = (surf_w - pw) / 2;
    int32_t py = (surf_h - ph) / 2;

    cc_rect(surf_h, px - 2, py - 2, pw + 4, ph + 4, COL_BORDER);
    cc_rect(surf_h, px, py, pw, ph, COL_BG);

    cc_text(surf_h, px + 24, py + 16, 1, "S5 自定义控制中心", accent);
    cc_text(surf_h, px + pw - 156, py + 16, 1, "V2.9 增强版", COL_WHITE);
    cc_rect(surf_h, px + 18, py + 46, pw - 36, 3, accent);

    cc_text(surf_h, px + 104, py + 58, 1, "入口: 设置 > 控制中心", COL_MUTED);
    const char *sd_str = "[无SD卡]";
    const uint32_t *sd_bg = COL_PILL_OFF;
    const uint32_t *sd_fg = COL_WHITE;
    int32_t sd_w = 114;
    if (st->sd_status == SD_STAT_SAVED) {
        sd_str = "[SD卡已保存]"; sd_bg = COL_GREEN; sd_fg = COL_DARK_TXT; sd_w = 156;
    } else if (st->sd_status == SD_STAT_LOADED) {
        sd_str = "[SD卡已加载]"; sd_bg = COL_GREEN; sd_fg = COL_DARK_TXT; sd_w = 156;
    } else if (st->sd_status == SD_STAT_DIRTY) {
        sd_str = "[未保存]"; sd_bg = COL_AMBER; sd_fg = COL_DARK_TXT; sd_w = 114;
    } else if (st->sd_status == SD_STAT_SAVE_FAILED) {
        sd_str = "[保存失败]"; sd_bg = COL_RED; sd_w = 156;
    }
    cc_rect(surf_h, px + 368, py + 56, sd_w, 28, sd_bg);
    cc_text(surf_h, px + 378, py + 59, 1, sd_str, sd_fg);

    char num_buf[8];
    cc_fmt_uint(num_buf, st->sd_lut_count);
    cc_text(surf_h, px + 548, py + 58, 1, "SD卡LUT:", COL_MUTED);
    cc_text(surf_h, px + 654, py + 58, 1, num_buf, accent);
    cc_text(surf_h, px + 694, py + 58, 1, "个", COL_MUTED);

    static const char *const BUILTIN_LABELS[5] = {
        "关闭 (纯V-Log)", "机内 1 (LUT1)", "机内 2 (LUT2)", "机内 3 (LUT3)", "机内 4 (LUT4)"
    };
    static const char *const THEME_LABELS[3] = { "青蓝", "琥珀", "翠绿" };
    static const char *const HK_LABELS[3] = { "关闭 (原厂)", "Fn: 实时选LUT", "Fn: 打开面板" };
    static const char *const ROW_NAMES[CC_ITEM_COUNT] = {
        "实时 LUT 槽位", "LUT 快捷键", "LUT 生效格调", "对焦提示音静音", "菜单主题配色", "恢复默认设置", "保存配置并退出"
    };

    cc_rect(surf_h, px + 18, py + 94, 4, 68, accent);
    cc_text(surf_h, px + 32, py + 100, 1, "S5", accent);
    cc_text(surf_h, px + 32, py + 130, 1, "菜单", COL_WHITE);

    int32_t row_x = px + 104;
    int32_t row_w = pw - 126;
    int32_t row_y0 = py + 94;
    int32_t row_h = 50;
    int32_t row_gap = 55;

    char sd_lut_label[32];
    for (uint32_t i = 0; i < CC_ITEM_COUNT; ++i) {
        int32_t ry = row_y0 + (int32_t)i * row_gap;
        int selected = (st->cursor == i);
        cc_rect(surf_h, row_x, ry, row_w, row_h, selected ? COL_ROW_SEL : COL_ROW_ALT);
        if (selected)
            cc_rect(surf_h, row_x, ry, 6, row_h, accent);

        cc_text(surf_h, row_x + 22, ry + 14, 1, ROW_NAMES[i], selected ? COL_WHITE : COL_MUTED);

        const char *val_str = "";
        const uint32_t *pill_col = COL_PILL_OFF;
        const uint32_t *txt_col = COL_WHITE;
        int32_t pill_w = 240;

        switch (i) {
        case 0:
            pill_w = 320;
            if (st->sd_lut_err) {
                val_str = "SD加载失败"; pill_col = COL_RED; txt_col = COL_WHITE;
            } else if (st->sd_lut_active >= 1u) {
                sd_lut_label[0] = 'S'; sd_lut_label[1] = 'D';
                uint32_t idx = st->sd_lut_active;
                uint32_t pos = 2u;
                if (idx >= 100u) sd_lut_label[pos++] = '1';
                sd_lut_label[pos++] = (char)('0' + (idx / 10u) % 10u);
                sd_lut_label[pos++] = (char)('0' + (idx % 10u));
                sd_lut_label[pos++] = ':'; sd_lut_label[pos++] = ' ';
                cc_strcpy_n(sd_lut_label + pos, (const char *)st->active_label, 19u);
                val_str = sd_lut_label; pill_col = COL_GREEN; txt_col = COL_DARK_TXT;
            } else {
                uint32_t slot = cc_get_builtin_slot();
                val_str = BUILTIN_LABELS[slot];
                pill_col = (slot != 0u) ? COL_GREEN : COL_PILL_OFF;
                txt_col = (slot != 0u) ? COL_DARK_TXT : COL_WHITE;
            }
            break;
        case 1: {
            uint8_t hk = (st->lut_hotkey <= LUT_HK_PANEL) ? st->lut_hotkey : LUT_HK_BAR;
            val_str = HK_LABELS[hk];
            pill_col = (hk == LUT_HK_STOCK) ? COL_PILL_OFF : (hk == LUT_HK_BAR ? COL_GREEN : accent);
            txt_col = (hk == LUT_HK_STOCK) ? COL_WHITE : COL_DARK_TXT;
            break;
        }
        case 2:
            val_str = st->lut_all_styles ? "全部照片格调" : "仅V-Log/自定";
            pill_col = st->lut_all_styles ? COL_GREEN : accent;
            txt_col = COL_DARK_TXT;
            break;
        case 3:
            val_str = st->beep_muted ? "开启" : "关闭";
            pill_col = st->beep_muted ? COL_GREEN : COL_PILL_OFF;
            txt_col = st->beep_muted ? COL_DARK_TXT : COL_WHITE;
            break;
        case 4:
            val_str = THEME_LABELS[st->theme < 3u ? st->theme : 0u];
            pill_col = accent; txt_col = COL_DARK_TXT;
            break;
        case 5:
            val_str = "[重置]"; pill_col = COL_RED; txt_col = COL_WHITE;
            break;
        case 6:
            val_str = "[保存退出]"; pill_col = accent; txt_col = COL_DARK_TXT;
            break;
        default:
            break;
        }

        int32_t pill_h = 34;
        int32_t pill_x = row_x + row_w - pill_w - 18;
        int32_t pill_y = ry + (row_h - pill_h) / 2;
        cc_rect(surf_h, pill_x, pill_y, pill_w, pill_h, pill_col);
        cc_text(surf_h, pill_x + 14, pill_y + 6, 1, val_str, txt_col);
    }

    cc_rect(surf_h, px + 18, py + ph - 52, pw - 36, 2, COL_BORDER);
    cc_text(surf_h, px + 36, py + ph - 38, 1, "上下:选择  左右:切换  SET:+10/确认  回放:退出", COL_MUTED);
}

static void cc_format_lut_name(volatile CcState *st, int idx, char *out, uint32_t max_len) {
    static const char *const BUILTIN_SHORT[5] = {
        "关闭 (纯V-Log)", "机内 1 (LUT1)", "机内 2 (LUT2)", "机内 3 (LUT3)", "机内 4 (LUT4)"
    };
    int total = 5 + (int)st->sd_lut_count;
    while (idx < 0) idx += total;
    while (idx >= total) idx -= total;
    if (idx <= 4) {
        cc_strcpy_n(out, BUILTIN_SHORT[idx], max_len);
        return;
    }
    uint32_t sd_i = (uint32_t)(idx - 4);
    const CcLutEntry *ent = &((const CcLutEntry *)(uintptr_t)CC_LUT_LIST_ADDR)[sd_i - 1u];
    uint32_t pos = 0u;
    out[pos++] = 'S'; out[pos++] = 'D';
    if (sd_i >= 100u) out[pos++] = '1';
    out[pos++] = (char)('0' + (sd_i / 10u) % 10u);
    out[pos++] = (char)('0' + (sd_i % 10u));
    out[pos++] = ':'; out[pos++] = ' ';
    cc_strcpy_n(out + pos, ent->label, (max_len > pos) ? (max_len - pos) : 1u);
}

static void cc_render_lut_bar(volatile CcState *st, int32_t surf_w, int32_t surf_h) {
    const uint32_t *accent = COL_ACCENT[st->theme < 3u ? st->theme : 0u];
    int32_t bw = (surf_w >= 920) ? 900 : (surf_w - 24);
    int32_t bh = 122;
    int32_t bx = (surf_w - bw) / 2;
    int32_t by = surf_h - bh - 14;

    cc_rect(surf_h, bx - 2, by - 2, bw + 4, bh + 4, COL_BORDER);
    cc_rect(surf_h, bx, by, bw, bh, COL_BG);
    cc_rect(surf_h, bx, by, bw, 3, accent);

    cc_text(surf_h, bx + 18, by + 10, 1, "实时 LUT", accent);
    char num_buf[8];
    cc_fmt_uint(num_buf, st->sd_lut_count);
    cc_text(surf_h, bx + bw - 196, by + 10, 1, "SD卡LUT:", COL_MUTED);
    cc_text(surf_h, bx + bw - 90, by + 10, 1, num_buf, accent);
    cc_text(surf_h, bx + bw - 50, by + 10, 1, "个", COL_MUTED);

    int cur = st->sd_lut_active ? (4 + (int)st->sd_lut_active) : (int)cc_get_builtin_slot();
    int prev_i = cc_next_valid_idx(st, cur, -1);
    int next_i = cc_next_valid_idx(st, cur, +1);
    char prev_s[24], cur_s[28], next_s[24];
    cc_format_lut_name(st, prev_i, prev_s, 18u);
    cc_format_lut_name(st, cur,    cur_s,  24u);
    cc_format_lut_name(st, next_i, next_s, 18u);

    int32_t cy = by + 40;
    int32_t ch = 42;
    int32_t side_w = 232;
    int32_t mid_w = bw - side_w * 2 - 48;

    cc_rect(surf_h, bx + 16, cy + 3, side_w, ch - 6, COL_ROW_ALT);
    cc_text(surf_h, bx + 26, cy + 10, 1, prev_s, COL_MUTED);

    int32_t mx = bx + 16 + side_w + 8;
    const uint32_t *mid_bg = st->sd_lut_err ? COL_RED : ((cur != 0) ? COL_GREEN : COL_PILL_OFF);
    const uint32_t *mid_fg = st->sd_lut_err ? COL_WHITE : ((cur != 0) ? COL_DARK_TXT : COL_WHITE);
    cc_rect(surf_h, mx - 2, cy - 2, mid_w + 4, ch + 4, accent);
    cc_rect(surf_h, mx, cy, mid_w, ch, mid_bg);
    cc_text(surf_h, mx + 12, cy + 10, 1, "<", mid_fg);
    cc_text(surf_h, mx + 34, cy + 10, 1, st->sd_lut_err ? "SD加载失败" : cur_s, mid_fg);
    cc_text(surf_h, mx + mid_w - 24, cy + 10, 1, ">", mid_fg);

    int32_t rx = mx + mid_w + 8;
    cc_rect(surf_h, rx, cy + 3, side_w, ch - 6, COL_ROW_ALT);
    cc_text(surf_h, rx + 10, cy + 10, 1, next_s, COL_MUTED);

    cc_text(surf_h, bx + 18, by + bh - 30, 1, "左右:实时切换  上下:+-10  SET/回放:确认退出", COL_MUTED);
}

void cc_egl_swap_overlay(uint32_t dpy, uint32_t surf) {
    const volatile CcState *ro = (const volatile CcState *)(uintptr_t)CC_STATE_ADDR;
    if (ro->magic != CC_MAGIC || ro->visible == CC_VIS_OFF)
        return;
    volatile CcState *st = (volatile CcState *)(uintptr_t)CC_STATE_ADDR;

    if (st->visible != CC_VIS_OFF) {
        egl_query_fn p_query = (egl_query_fn)(uintptr_t)FN_EGL_QUERY_SURF;
        vg_geti_fn p_geti = (vg_geti_fn)(uintptr_t)FN_VG_GETI;
        vg_seti_fn p_seti = (vg_seti_fn)(uintptr_t)FN_VG_SETI;

        int32_t w = 960, h = 640;
        int32_t qw = 0, qh = 0;
        if (p_query(dpy, surf, EGL_WIDTH, &qw) && qw >= 320 && qw <= 1920)
            w = qw;
        if (p_query(dpy, surf, EGL_HEIGHT, &qh) && qh >= 240 && qh <= 1200)
            h = qh;

        int32_t prev_scissor = p_geti(VG_SCISSORING);
        p_seti(VG_SCISSORING, 0);
        if (st->visible == CC_VIS_LUT_BAR)
            cc_render_lut_bar(st, w, h);
        else
            cc_render_overlay(st, w, h);
        p_seti(VG_SCISSORING, prev_scissor);
    }
}

__attribute__((naked)) void cc_egl_swap_trampoline(void) {
    __asm__ volatile(
        "push {r0, r1, r2, r3, ip, lr}\n\t"
        "bl cc_egl_swap_overlay\n\t"
        "pop {r0, r1, r2, r3, ip, lr}\n\t"
        "push.w {r4, r5, r6, r7, r8, lr}\n\t"
        "ldr ip, =0x064C3CDF\n\t"
        "bx ip\n\t"
        ".ltorg\n\t"
    );
}
