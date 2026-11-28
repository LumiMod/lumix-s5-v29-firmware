#!/usr/bin/env python3
# RULES-ACTIVE-GLOBAL
"""Offline patcher for Panasonic DC-S5 V2.9 (MC801) 03_program.bin:
1. Removes the 15/30-minute video recording time limit (LIMIT_ADDRESS 0x868261B6).
2. Unlocks NTSC (59.94 Hz) MOV high-res modes in wl + rom_tpl:
   - 0x00CC -> 0x00F4 (5.9K 16:9 29.97p HEVC 10-bit)
   - 0x00CE -> 0x00B7 (C4K 23.98p 150M 4:2:2 10-bit) [or 0x0098 4K 400M ALL-I with --with-all-i]
   - 0x00D1 -> 0x00F1 (5.9K 16:9 23.98p HEVC 10-bit)
   - 0x00D2 -> 0x00ED (6K Open Gate 3:2 23.98p HEVC 10-bit)
3. Unlocks PAL (50.00 Hz) MOV high-res modes in PAL wl + rom_tpl:
   - 0x00CD -> 0x00F3 (5.9K 16:9 25.00p HEVC 10-bit)
   - 0x00CF -> 0x00EF (5.4K Open Gate 3:2 25.00p HEVC 10-bit)
4. Disables still-photo playback View Assist double-LUT (0x866AE770: beq -> b).

Operates on a copy of s5_v29_decrypted/03_program.bin; never modifies the original.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

PROGRAM_BASE_VA = 0x86280000

GUARDS = (
    (0x866B493A, 0x2801, "rtos_sig1"),
    (0x866B493C, 0xD009, "rtos_branch"),
    (0x866B493E, 0xB2E0, "rtos_sig2"),
    (0x866B4952, 0x486B, "rtos_sig3"),
    (0x866B4954, 0x8800, "rtos_qid"),
    (0x866B4956, 0xBD10, "rtos_sig4"),
    (0x868261B4, 0x460D, "limit_prev_mov_r5_r1"),
    (0x868261B8, 0xE8BD, "limit_pop"),
    (0x86F15E98, 0xFFFF, "ntsc_wl_sentinel"),
    (0x86F15EDE, 0xFFFF, "pal_wl_sentinel"),
    (0x87089634, 0xFFFF, "ntsc_rom_tpl_sentinel"),
    (0x870896EC, 0xFFFF, "pal_rom_tpl_sentinel"),
    (0x866AE76E, 0x4208, "playback_lut_tst_r0_r1"),
    (0x866AE772, 0x4811, "playback_lut_ldr_r0_pc"),
    (0x86867D0A, 0x8BA0, "upd_ver_ldrh_r0_1c"),
    (0x86867D10, 0x4281, "upd_ver_cmp_r1_r0"),
)

LIMIT_PATCHES = (
    # mov r0, r5 (0x4628) -> movs r0, #0 (0x2000): return 0 (no recording time cap)
    (0x868261B6, 0x4628, 0x2000, "remove_recording_time_limit"),
    # bge #0x06867d2c (0xDA0B) -> nop (0xBF00): allow free upgrade, same-version reflash & downgrade
    # (keeps MC801 model check @ 0x06867D02 and SHA-256/CRC32 intact!)
    (0x86867D12, 0xDA0B, 0xBF00, "allow_any_version_flash_up_down"),
)

# Unsigned container path (payload[0] == 0): branch directly to the common
# component-table parser at 0x868C7098, skipping the ECDSA block at
# 0x868C7054..0x868C7092 and, crucially, skipping 0x868C7094 which clears
# verify_enabled ([sp,#0xF8]).  The per-component SHA-256 check at
# 0x868C71B4 therefore still runs; the outer CRC32 path is untouched.
#
# Original 0x868C70A4: 99 F8  (ldrb.w r0,[sb,#0x68])
# Patched  0x868C70A4: F8 E7  (b #0x868C7098)
UNSIGNED_ECDSA_SKIP_PATCHES = (
    (0x868C70A4, 0xF899, 0xE7F8, "unsigned_skip_ecdsa_keep_sha256"),
)

PLAYBACK_LUT_PATCHES = (
    # Playback (r1==2) View Assist branch: beq #0x866ae79a (0xD013) -> b #0x866ae79a (0xE013)
    # Prevents double-applying monitor LUT on already-baked LUT JPEGs during playback; Live View untouched.
    (0x866AE770, 0xD013, 0xE013, "disable_playback_double_lut"),
)

# 6K Open Gate (0x788a84, 0x788af8) & 5.9K (0x788c54, 0x788cc8, 0x788d3c, 0x788db0)
# Each entry stores (b1, b2) = (200_000_000, 200_000_000) at (file_off - 8, file_off - 4).
BITRATE_OFFSETS_6K_59K = (
    (0x788A7C, "6k_og_entry1"),
    (0x788AF0, "6k_og_entry2"),
    (0x788C4C, "59k_entry1"),
    (0x788CC0, "59k_entry2"),
    (0x788D34, "59k_entry3"),
    (0x788DA8, "59k_entry4"),
)


def build_video_patches(with_all_i: bool = False, all_modes: bool = True) -> tuple:
    ce_target = 0x0098 if (with_all_i and not all_modes) else 0x00B7
    ce_label = "4k24_400m_alli" if ce_target == 0x0098 else "c4k24_150m_10bit"
    patches = [
        # NTSC wl (0x86F15E88..0x86F15E96)
        (0x86F15E88, 0x00CC, 0x00F4, "ntsc_wl_59k30_lo"),
        (0x86F15E8A, 0x80CC, 0x80F4, "ntsc_wl_59k30_hi"),
        (0x86F15E8C, 0x00CE, ce_target, f"ntsc_wl_{ce_label}_lo"),
        (0x86F15E8E, 0x80CE, 0x8000 | ce_target, f"ntsc_wl_{ce_label}_hi"),
        (0x86F15E90, 0x00D1, 0x00F1, "ntsc_wl_59k24_lo"),
        (0x86F15E92, 0x80D1, 0x80F1, "ntsc_wl_59k24_hi"),
        (0x86F15E94, 0x00D2, 0x00ED, "ntsc_wl_6k2398_lo"),
        (0x86F15E96, 0x80D2, 0x80ED, "ntsc_wl_6k2398_hi"),
        # NTSC rom_tpl (0x870895F4..0x8708962C)
        (0x870895F4, 0x00CC, 0x00F4, "ntsc_tpl_59k30_lo"),
        (0x870895FC, 0x80CC, 0x80F4, "ntsc_tpl_59k30_hi"),
        (0x87089604, 0x00CE, ce_target, f"ntsc_tpl_{ce_label}_lo"),
        (0x8708960C, 0x80CE, 0x8000 | ce_target, f"ntsc_tpl_{ce_label}_hi"),
        (0x87089614, 0x00D1, 0x00F1, "ntsc_tpl_59k24_lo"),
        (0x8708961C, 0x80D1, 0x80F1, "ntsc_tpl_59k24_hi"),
        (0x87089624, 0x00D2, 0x00ED, "ntsc_tpl_6k2398_lo"),
        (0x8708962C, 0x80D2, 0x80ED, "ntsc_tpl_6k2398_hi"),
        # PAL wl (0x86F15ED6..0x86F15EDC)
        (0x86F15ED6, 0x00CD, 0x00F3, "pal_wl_59k25_lo"),
        (0x86F15ED8, 0x80CD, 0x80F3, "pal_wl_59k25_hi"),
        (0x86F15EDA, 0x00CF, 0x00EF, "pal_wl_54k_og25_lo"),
        (0x86F15EDC, 0x80CF, 0x80EF, "pal_wl_54k_og25_hi"),
        # PAL rom_tpl (0x870896CC..0x870896E4)
        (0x870896CC, 0x00CD, 0x00F3, "pal_tpl_59k25_lo"),
        (0x870896D4, 0x80CD, 0x80F3, "pal_tpl_59k25_hi"),
        (0x870896DC, 0x00CF, 0x00EF, "pal_tpl_54k_og25_lo"),
        (0x870896E4, 0x80CF, 0x80EF, "pal_tpl_54k_og25_hi"),
    ]
    if all_modes:
        # Full safe readout & ALL-I pack (skips 0x00F0 which stalls on record in 59.94 Hz):
        # 0x009B -> 0x0098 (4K 23.98p 400M 4:2:2 10-bit ALL-I)
        # 0x009F -> 0x00B8 (C4K 23.98p 400M 4:2:2 10-bit ALL-I)
        # 0x00A1 -> 0x0090 (C4K 29.97p 400M 4:2:2 10-bit ALL-I)
        # 0x00A5 -> 0x00A9 (4K-A 4:3 23.98p 400M 4:2:2 10-bit ALL-I)
        # 0x00A7 -> 0x00D0 (FHD 23.98p 200M 4:2:2 10-bit ALL-I)
        # 0x00BE -> 0x00EE (6K 3:2 Open Gate 24.00p HEVC 10-bit)
        # 0x00C0 -> 0x00F2 (5.9K 16:9 24.00p HEVC 10-bit)
        patches.extend([
            # NTSC wl
            (0x86F15E74, 0x009B, 0x0098, "ntsc_wl_4k24_400m_alli_lo"),
            (0x86F15E76, 0x809B, 0x8098, "ntsc_wl_4k24_400m_alli_hi"),
            (0x86F15E68, 0x009F, 0x00B8, "ntsc_wl_c4k24_400m_alli_lo"),
            (0x86F15E6A, 0x809F, 0x80B8, "ntsc_wl_c4k24_400m_alli_hi"),
            (0x86F15E78, 0x00A1, 0x0090, "ntsc_wl_c4k30_400m_alli_lo"),
            (0x86F15E7A, 0x80A1, 0x8090, "ntsc_wl_c4k30_400m_alli_hi"),
            (0x86F15E6C, 0x00A5, 0x00A9, "ntsc_wl_4ka24_400m_alli_lo"),
            (0x86F15E6E, 0x80A5, 0x80A9, "ntsc_wl_4ka24_400m_alli_hi"),
            (0x86F15E70, 0x00A7, 0x00D0, "ntsc_wl_fhd24_200m_alli_lo"),
            (0x86F15E72, 0x80A7, 0x80D0, "ntsc_wl_fhd24_200m_alli_hi"),
            (0x86F15E80, 0x00BE, 0x00EE, "ntsc_wl_6k2400_lo"),
            (0x86F15E82, 0x80BE, 0x80EE, "ntsc_wl_6k2400_hi"),
            (0x86F15E84, 0x00C0, 0x00F2, "ntsc_wl_59k2400_lo"),
            (0x86F15E86, 0x80C0, 0x80F2, "ntsc_wl_59k2400_hi"),
            # NTSC rom_tpl
            (0x87089574, 0x009B, 0x0098, "ntsc_tpl_4k24_400m_alli_lo"),
            (0x8708957C, 0x809B, 0x8098, "ntsc_tpl_4k24_400m_alli_hi"),
            (0x87089584, 0x009F, 0x00B8, "ntsc_tpl_c4k24_400m_alli_lo"),
            (0x8708958C, 0x809F, 0x80B8, "ntsc_tpl_c4k24_400m_alli_hi"),
            (0x87089594, 0x00A1, 0x0090, "ntsc_tpl_c4k30_400m_alli_lo"),
            (0x8708959C, 0x80A1, 0x8090, "ntsc_tpl_c4k30_400m_alli_hi"),
            (0x870895A4, 0x00A5, 0x00A9, "ntsc_tpl_4ka24_400m_alli_lo"),
            (0x870895AC, 0x80A5, 0x80A9, "ntsc_tpl_4ka24_400m_alli_hi"),
            (0x870895C4, 0x00A7, 0x00D0, "ntsc_tpl_fhd24_200m_alli_lo"),
            (0x870895CC, 0x80A7, 0x80D0, "ntsc_tpl_fhd24_200m_alli_hi"),
            (0x870895D4, 0x00BE, 0x00EE, "ntsc_tpl_6k2400_lo"),
            (0x870895DC, 0x80BE, 0x80EE, "ntsc_tpl_6k2400_hi"),
            (0x870895E4, 0x00C0, 0x00F2, "ntsc_tpl_59k2400_lo"),
            (0x870895EC, 0x80C0, 0x80F2, "ntsc_tpl_59k2400_hi"),
        ])
    return tuple(patches)


def va_to_offset(va: int) -> int:
    if va < PROGRAM_BASE_VA:
        raise ValueError(f"VA 0x{va:08x} is below base 0x{PROGRAM_BASE_VA:08x}")
    return va - PROGRAM_BASE_VA


def read_u16(buf: bytearray | bytes, offset: int) -> int:
    return struct.unpack_from("<H", buf, offset)[0]


def write_u16(buf: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<H", buf, offset, value)


def apply_patches(
    src_path: Path,
    dst_path: Path,
    report_path: Path,
    with_all_i: bool = False,
    bitrate_mbps: int = 300,
    playback_lut_fix: bool = True,
    all_modes: bool = True,
) -> dict:
    raw = src_path.read_bytes()
    src_sha256 = hashlib.sha256(raw).hexdigest()
    buf = bytearray(raw)

    for va, expected, name in GUARDS:
        off = va_to_offset(va)
        actual = read_u16(buf, off)
        if actual != expected:
            raise RuntimeError(
                f"Guard mismatch for {name} @ VA 0x{va:08x} (file 0x{off:06x}): "
                f"expected 0x{expected:04x}, got 0x{actual:04x}"
            )

    applied = []
    patch_list = (
        LIMIT_PATCHES
        + UNSIGNED_ECDSA_SKIP_PATCHES
        + build_video_patches(with_all_i=with_all_i, all_modes=all_modes)
    )
    if playback_lut_fix:
        patch_list = patch_list + PLAYBACK_LUT_PATCHES
    for va, expected, target, name in patch_list:
        off = va_to_offset(va)
        actual = read_u16(buf, off)
        if actual != expected:
            raise RuntimeError(
                f"Patch pre-check mismatch for {name} @ VA 0x{va:08x} (file 0x{off:06x}): "
                f"expected 0x{expected:04x}, got 0x{actual:04x}"
            )
        write_u16(buf, off, target)
        readback = read_u16(buf, off)
        if readback != target:
            raise RuntimeError(f"Readback failed for {name}")
        applied.append({
            "name": name,
            "va": f"0x{va:08x}",
            "file_offset": f"0x{off:06x}",
            "old_u16": f"0x{expected:04x}",
            "new_u16": f"0x{target:04x}",
        })

    if bitrate_mbps != 200:
        new_bps = bitrate_mbps * 1_000_000
        for off, name in BITRATE_OFFSETS_6K_59K:
            b1, b2 = struct.unpack_from("<II", buf, off)
            if (b1, b2) != (200_000_000, 200_000_000):
                raise RuntimeError(f"Bitrate pre-check mismatch for {name} @ 0x{off:06x}: {(b1, b2)}")
            struct.pack_into("<II", buf, off, new_bps, new_bps)
            applied.append({
                "name": f"bitrate_{name}",
                "va": f"0x{PROGRAM_BASE_VA + off:08x}",
                "file_offset": f"0x{off:06x}",
                "old_bps": 200_000_000,
                "new_bps": new_bps,
            })

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(buf)
    dst_sha256 = hashlib.sha256(buf).hexdigest()

    report = {
        "source": str(src_path),
        "source_sha256": src_sha256,
        "output": str(dst_path),
        "output_sha256": dst_sha256,
        "size_bytes": len(buf),
        "with_all_i": with_all_i,
        "all_modes": all_modes,
        "bitrate_mbps_6k_59k": bitrate_mbps,
        "playback_lut_fix": playback_lut_fix,
        "patches_applied": len(applied),
        "patches": applied,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch S5 V2.9 03_program.bin (30-min limit + NTSC/PAL high-res MOV + 6K/5.9K bitrate + playback LUT fix)")
    parser.add_argument("--src", type=Path, default=Path("/Users/lixiuqi/相机功能验证/s5_v29_decrypted/03_program.bin"))
    parser.add_argument("--dst", type=Path, default=Path("/Users/lixiuqi/相机功能验证/s5_v29_patched/03_program.bin"))
    parser.add_argument("--report", type=Path, default=Path("/Users/lixiuqi/相机功能验证/s5_v29_patched/patch_report.json"))
    parser.add_argument("--with-all-i", action="store_true", help="Use 4K 23.98p 400M ALL-I (0x0098) instead of C4K 23.98p 10-bit (0x00B7)")
    parser.add_argument("--bitrate-mbps", type=int, default=300, help="Target Mbps for 6K/5.9K modes (default: 300)")
    parser.add_argument("--no-playback-lut-fix", action="store_true", help="Skip playback double-LUT fix at 0x866AE770")
    args = parser.parse_args()
    report = apply_patches(
        args.src,
        args.dst,
        args.report,
        with_all_i=args.with_all_i,
        bitrate_mbps=args.bitrate_mbps,
        playback_lut_fix=not args.no_playback_lut_fix,
    )
    print(f"Patched {report['patches_applied']} halfwords -> {report['output']}")
    print(f"Source SHA-256: {report['source_sha256']}")
    print(f"Output SHA-256: {report['output_sha256']}")


if __name__ == "__main__":
    main()
