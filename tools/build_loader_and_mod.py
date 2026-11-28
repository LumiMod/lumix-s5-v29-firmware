#!/usr/bin/env python3
# RULES-ACTIVE-GLOBAL
"""Build the split Magic-Lantern-style S5 V2.9 architecture:
1. Permanent ~2 KB in-camera base loader:
   - build_loader/03_program.bin  : base video/bitrate/ECDSA patches + s5_loader.c @ 0x070F8000
   - build_loader/07_postboot3.bin: 96-byte pb3_stub.c @ 0x879F7900
   - build_loader/20_osdover.bin  : 像素更新 -> 控制中心 / Pixel Refresh -> ControlCenter
2. Hot-pluggable SD-card module:
   - S5_MOD.BIN (copy to SD card root A:\\S5_MOD.BIN or B:\\S5_MOD.BIN)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from build_combined import (  # noqa: E402
    CALL,
    EGL_SWAP_ENTRY_LOW,
    EGL_SWAP_ORIG_PROLOGUE,
    FN_LUT_SELECT_LOW,
    FN_LUT_SELECT_ORIG_BL,
    KEY_DISPATCH_LOW,
    KEY_DISPATCH_ORIG_LOW,
    LV_STYLE_GATE_LOW,
    LV_STYLE_GATE_ORIG_BL,
    MODULE,
    ORIGINAL_GETTER,
    OSDOVER_EN_OFF,
    OSDOVER_ZH_OFF,
    PROG_BASE_LOW,
    SETUP_PIXEL_REFRESH_LOW,
    SETUP_PIXEL_REFRESH_ORIG_BL,
    make_bl,
    make_bw,
    parse_elf32,
    sha256,
)

LOADER_CAVE_LOW = 0x070F8000
LOADER_TABLE_LOW = 0x070FC000
LOADER_TABLE_CLEAR_SIZE = 0x1000  # 4 KB (S5ModTable + 2 KB DMA IO buf)
MOD_LOAD_ADDR = 0x07100000
MOD_STATE_ADDR = 0x07200000
MOD_STATE_CLEAR_SIZE = 0x4000
PB3_STUB_CAVE = 0x879F7900

S5_MOD_HDR_MAGIC = 0x444D3553  # "S5MD"
S5_MOD_ABI_VERSION = 2
UI_EVENT_ENTRY_LOW = 0x066BEEA6
UI_EVENT_ORIG_PROLOGUE = bytes.fromhex("2de9f041")  # push.w {r4-r8,lr}
RAW_INPUT_CALLSITE_LOW = 0x064759BE
RAW_INPUT_ORIG_LOW = 0x06642D22

DEFAULT_DEC_DIR = REPO_ROOT.parent / "s5_v29_decrypted"
DEFAULT_OUT_DIR = REPO_ROOT / "build_loader"
DEFAULT_MOD_OUT = REPO_ROOT / "S5_MOD.BIN"


def link_obj_split_bss(obj_bytes: bytes, base_addr: int) -> tuple[bytearray, int, dict[str, int]]:
    """Link an ELF32 ARM Thumb .o at base_addr, returning (progbits_payload, bss_size, sym_addrs)."""
    sections, symbols = parse_elf32(obj_bytes)
    payload = bytearray()
    sec_bases: dict[int, int] = {}

    prog_secs = [
        s for s in sections
        if (s["flags"] & 0x2) and s["size"] > 0 and s["type"] != 8
        and s["name"].startswith((".text", ".rodata", ".data"))
    ]
    bss_secs = [
        s for s in sections
        if (s["flags"] & 0x2) and s["size"] > 0 and s["type"] == 8
        and s["name"].startswith(".bss")
    ]
    order_key = lambda s: (
        0 if s["name"].startswith(".text") else
        1 if s["name"].startswith(".rodata") else 2,
        s["idx"],
    )
    for s in sorted(prog_secs, key=order_key):
        align = max(4, s["align"])
        pad = (align - (base_addr + len(payload)) % align) % align
        if pad:
            payload.extend(b"\x00" * pad)
        sec_bases[s["idx"]] = base_addr + len(payload)
        payload.extend(s["data"])

    # Align end of progbits to 4 bytes before .bss
    if len(payload) % 4:
        payload.extend(b"\x00" * (4 - len(payload) % 4))
    bss_start = base_addr + len(payload)
    cur = bss_start
    for s in sorted(bss_secs, key=lambda x: x["idx"]):
        align = max(4, s["align"])
        cur = (cur + align - 1) & ~(align - 1)
        sec_bases[s["idx"]] = cur
        cur += s["size"]
    bss_size = cur - bss_start

    for s in sections:
        if s["type"] not in (4, 9):
            continue
        target_sec_idx = s["info"]
        if target_sec_idx not in sec_bases:
            continue
        sec_off_in_payload = sec_bases[target_sec_idx] - base_addr
        is_rela = s["type"] == 4
        step = 12 if is_rela else 8
        for pos in range(0, s["size"], step):
            if is_rela:
                off, r_info, _add = struct.unpack_from("<IIi", s["data"], pos)
            else:
                off, r_info = struct.unpack_from("<II", s["data"], pos)
            sym = symbols[r_info >> 8]
            rtype = r_info & 0xFF
            sym_sec = sym["st_shndx"]
            if sym_sec not in sec_bases:
                raise RuntimeError(f"Unresolved external symbol: {sym['name']} (sec {sym_sec})")
            target_addr = sec_bases[sym_sec] + sym["st_value"]
            p_off = sec_off_in_payload + off
            if rtype in (2, 38):
                addend = struct.unpack_from("<I", payload, p_off)[0]
                struct.pack_into("<I", payload, p_off, (target_addr + addend) & 0xFFFFFFFF)
            elif rtype == 10:
                payload[p_off:p_off + 4] = make_bl(base_addr + p_off, target_addr & ~1)
            elif rtype in (30, 47):
                payload[p_off:p_off + 4] = make_bw(base_addr + p_off, target_addr & ~1)
            else:
                raise RuntimeError(f"Unsupported reloc type {rtype} for {sym['name']} in {s['name']}")

    sym_addrs = {
        sym["name"]: sec_bases[sym["st_shndx"]] + sym["st_value"]
        for sym in symbols if sym["name"] and sym["st_shndx"] in sec_bases
    }
    return payload, bss_size, sym_addrs


def clang_compile(src: Path, obj: Path, opt: str = "-O2", includes: list[Path] | None = None) -> None:
    cmd = [
        "clang", "--target=armv7-none-eabi", "-mcpu=cortex-a7", "-mthumb",
        "-mno-movt", "-mfpu=none", "-mfloat-abi=soft", "-fno-vectorize",
        "-fno-slp-vectorize", "-ffreestanding", "-fno-builtin", "-fno-stack-protector",
        opt, "-c", str(src), "-o", str(obj),
    ]
    for inc in includes or []:
        cmd += ["-I", str(inc)]
    subprocess.run(cmd, check=True)


def build_base_program(dec_dir: Path, out_dir: Path) -> dict:
    patcher = REPO_ROOT / "tools" / "patch_s5_video_unlim.py"
    prog_path = out_dir / "03_program.bin"
    subprocess.run([
        sys.executable, str(patcher),
        "--src", str(dec_dir / "03_program.bin"),
        "--dst", str(prog_path),
        "--report", str(out_dir / "patch_report.json"),
        "--with-all-i",
        "--bitrate-mbps", "300",
    ], check=True)

    ldr_o = out_dir / "s5_loader.o"
    clang_compile(REPO_ROOT / "loader" / "s5_loader.c", ldr_o, "-Os", [REPO_ROOT / "loader"])
    payload, bss_size, syms = link_obj_split_bss(ldr_o.read_bytes(), LOADER_CAVE_LOW)
    assert len(payload) + bss_size <= (LOADER_TABLE_LOW - LOADER_CAVE_LOW), "Loader overlaps S5ModTable!"
    (out_dir / "s5_loader_payload.bin").write_bytes(payload)

    prog = bytearray(prog_path.read_bytes())

    key_off = KEY_DISPATCH_LOW - PROG_BASE_LOW
    assert prog[key_off:key_off + 4] == make_bl(KEY_DISPATCH_LOW, KEY_DISPATCH_ORIG_LOW)
    prog[key_off:key_off + 4] = make_bl(KEY_DISPATCH_LOW, syms["ldr_key_hook"] & ~1)

    raw_off = RAW_INPUT_CALLSITE_LOW - PROG_BASE_LOW
    assert prog[raw_off:raw_off + 4] == make_bl(RAW_INPUT_CALLSITE_LOW, RAW_INPUT_ORIG_LOW)
    prog[raw_off:raw_off + 4] = make_bl(RAW_INPUT_CALLSITE_LOW, syms["ldr_raw_input_hook"] & ~1)

    ui_off = UI_EVENT_ENTRY_LOW - PROG_BASE_LOW
    assert prog[ui_off:ui_off + 4] == UI_EVENT_ORIG_PROLOGUE
    prog[ui_off:ui_off + 4] = make_bw(UI_EVENT_ENTRY_LOW, syms["ldr_ui_event_trampoline"] & ~1)

    pr_off = SETUP_PIXEL_REFRESH_LOW - PROG_BASE_LOW
    assert prog[pr_off:pr_off + 4] == make_bl(SETUP_PIXEL_REFRESH_LOW, SETUP_PIXEL_REFRESH_ORIG_BL)
    prog[pr_off:pr_off + 4] = make_bw(SETUP_PIXEL_REFRESH_LOW, syms["ldr_setup_menu_trampoline"] & ~1)

    fn_off = FN_LUT_SELECT_LOW - PROG_BASE_LOW
    assert prog[fn_off:fn_off + 4] == make_bl(FN_LUT_SELECT_LOW, FN_LUT_SELECT_ORIG_BL)
    prog[fn_off:fn_off + 4] = make_bw(FN_LUT_SELECT_LOW, syms["ldr_fn_lut_trampoline"] & ~1)

    lvg_off = LV_STYLE_GATE_LOW - PROG_BASE_LOW
    assert prog[lvg_off:lvg_off + 4] == make_bl(LV_STYLE_GATE_LOW, LV_STYLE_GATE_ORIG_BL)
    prog[lvg_off:lvg_off + 4] = make_bl(LV_STYLE_GATE_LOW, syms["ldr_lv_style_gate"] & ~1)

    swap_off = EGL_SWAP_ENTRY_LOW - PROG_BASE_LOW
    assert bytes(prog[swap_off:swap_off + 4]) == EGL_SWAP_ORIG_PROLOGUE
    prog[swap_off:swap_off + 4] = make_bw(EGL_SWAP_ENTRY_LOW, syms["ldr_egl_swap_trampoline"] & ~1)

    cave_off = LOADER_CAVE_LOW - PROG_BASE_LOW
    tbl_off = LOADER_TABLE_LOW - PROG_BASE_LOW
    st_off = MOD_STATE_ADDR - PROG_BASE_LOW
    assert prog[cave_off:cave_off + len(payload)] == b"\xff" * len(payload)
    assert prog[tbl_off:tbl_off + LOADER_TABLE_CLEAR_SIZE] == b"\xff" * LOADER_TABLE_CLEAR_SIZE
    assert prog[st_off:st_off + MOD_STATE_CLEAR_SIZE] == b"\xff" * MOD_STATE_CLEAR_SIZE
    prog[cave_off:cave_off + len(payload)] = payload
    prog[tbl_off:tbl_off + LOADER_TABLE_CLEAR_SIZE] = b"\x00" * LOADER_TABLE_CLEAR_SIZE
    prog[st_off:st_off + MOD_STATE_CLEAR_SIZE] = b"\x00" * MOD_STATE_CLEAR_SIZE

    prog_path.write_bytes(prog)
    return {
        "loader_address": f"0x{LOADER_CAVE_LOW:08x}",
        "loader_bytes": len(payload),
        "table_address": f"0x{LOADER_TABLE_LOW:08x}",
        "ldr_key_hook": f"0x{syms['ldr_key_hook']:08x}",
        "raw_input_callsite": f"0x{RAW_INPUT_CALLSITE_LOW:08x}",
        "ldr_raw_input_hook": f"0x{syms['ldr_raw_input_hook']:08x}",
        "ui_event_entry": f"0x{UI_EVENT_ENTRY_LOW:08x}",
        "ldr_ui_event_trampoline": f"0x{syms['ldr_ui_event_trampoline']:08x}",
        "ldr_setup_menu_trampoline": f"0x{syms['ldr_setup_menu_trampoline']:08x}",
        "ldr_fn_lut_trampoline": f"0x{syms['ldr_fn_lut_trampoline']:08x}",
        "ldr_lv_style_gate": f"0x{syms['ldr_lv_style_gate']:08x}",
        "ldr_egl_swap_trampoline": f"0x{syms['ldr_egl_swap_trampoline']:08x}",
        "program_sha256": sha256(prog),
    }


def build_base_postboot3(dec_dir: Path, out_dir: Path) -> dict:
    stub_o = out_dir / "pb3_stub.o"
    clang_compile(REPO_ROOT / "loader" / "pb3_stub.c", stub_o, "-Os", [REPO_ROOT / "loader"])
    payload, bss_size, syms = link_obj_split_bss(stub_o.read_bytes(), PB3_STUB_CAVE)
    assert bss_size == 0
    (out_dir / "pb3_stub_payload.bin").write_bytes(payload)

    pb3 = bytearray((dec_dir / "07_postboot3.bin").read_bytes())
    call_off = CALL - MODULE
    assert pb3[call_off:call_off + 4] == make_bl(CALL, ORIGINAL_GETTER)
    cave_off = PB3_STUB_CAVE - MODULE
    assert pb3[cave_off:cave_off + len(payload)] == b"\xff" * len(payload)

    pb3[call_off:call_off + 4] = make_bl(CALL, syms["pb3_image_bridge"] & ~1)
    pb3[cave_off:cave_off + len(payload)] = payload
    (out_dir / "07_postboot3.bin").write_bytes(pb3)
    return {
        "stub_address": f"0x{PB3_STUB_CAVE:08x}",
        "stub_entry": f"0x{syms['pb3_image_bridge']:08x}",
        "stub_bytes": len(payload),
        "postboot3_sha256": sha256(pb3),
    }


def build_osdover(dec_dir: Path, out_dir: Path) -> dict:
    osd = bytearray((dec_dir / "20_osdover.bin").read_bytes())
    zh_old, zh_new = "像素更新".encode("utf-16le"), "控制中心".encode("utf-16le")
    en_old, en_new = "Pixel Refresh".encode("utf-16le"), "ControlCenter".encode("utf-16le")
    assert osd[OSDOVER_ZH_OFF:OSDOVER_ZH_OFF + len(zh_old)] == zh_old
    assert osd[OSDOVER_EN_OFF:OSDOVER_EN_OFF + len(en_old)] == en_old
    osd[OSDOVER_ZH_OFF:OSDOVER_ZH_OFF + len(zh_new)] = zh_new
    osd[OSDOVER_EN_OFF:OSDOVER_EN_OFF + len(en_new)] = en_new
    (out_dir / "20_osdover.bin").write_bytes(osd)
    return {"sha256": sha256(osd)}


def build_sd_mod(out_dir: Path, mod_out: Path) -> dict:
    mod_o = out_dir / "s5_mod_main.o"
    clang_compile(
        REPO_ROOT / "mod" / "s5_mod_main.c", mod_o, "-O2",
        [REPO_ROOT / "loader", REPO_ROOT / "src"],
    )
    payload, bss_size, syms = link_obj_split_bss(mod_o.read_bytes(), MOD_LOAD_ADDR)
    entry_addr = syms["s5_mod_main"] | 1
    assert len(payload) + bss_size <= 0x00100000, "S5_MOD.BIN exceeds 1 MB window!"

    crc = zlib.crc32(payload) & 0xFFFFFFFF
    hdr = struct.pack(
        "<8I",
        S5_MOD_HDR_MAGIC,
        S5_MOD_ABI_VERSION,
        MOD_LOAD_ADDR,
        entry_addr,
        len(payload),
        bss_size,
        crc,
        0,
    )
    mod_blob = hdr + payload
    mod_out.write_bytes(mod_blob)
    return {
        "file": str(mod_out),
        "abi_version": S5_MOD_ABI_VERSION,
        "total_bytes": len(mod_blob),
        "load_addr": f"0x{MOD_LOAD_ADDR:08x}",
        "entry_addr": f"0x{entry_addr:08x}",
        "payload_bytes": len(payload),
        "bss_bytes": bss_size,
        "crc32": f"0x{crc:08x}",
        "sha256": sha256(mod_blob),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build S5 V2.9 permanent base loader + SD S5_MOD.BIN")
    ap.add_argument("--dec-dir", type=Path, default=DEFAULT_DEC_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--mod-out", type=Path, default=DEFAULT_MOD_OUT)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    prog_info = build_base_program(args.dec_dir, args.out_dir)
    pb3_info = build_base_postboot3(args.dec_dir, args.out_dir)
    osd_info = build_osdover(args.dec_dir, args.out_dir)
    mod_info = build_sd_mod(args.out_dir, args.mod_out)

    manifest = {
        "03_program": prog_info,
        "07_postboot3": pb3_info,
        "20_osdover": osd_info,
        "s5_mod": mod_info,
    }
    (args.out_dir / "loader_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Built resident s5_loader ({prog_info['loader_bytes']} B @ {prog_info['loader_address']})")
    print(f"Built resident pb3_stub  ({pb3_info['stub_bytes']} B @ {pb3_info['stub_address']})")
    print(f"Built SD module S5_MOD.BIN ({mod_info['total_bytes']} B, payload={mod_info['payload_bytes']} B, "
          f"bss={mod_info['bss_bytes']} B, crc32={mod_info['crc32']})")


if __name__ == "__main__":
    main()
