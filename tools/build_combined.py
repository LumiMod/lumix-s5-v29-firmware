#!/usr/bin/env python3
# RULES-ACTIVE-GLOBAL
"""Build combined Panasonic Lumix DC-S5 V2.9 (MC801) patched components:
- 03_program.bin  : video/mode/bitrate/playback/ECDSA patches + S5 Control Center + S5II Live LUT Bar
- 07_postboot3.bin: Direct-YCbCr 9^3 Real-Time LUT JPEG hook (Slot 0 = pure V-Log, Built-in 1..4 + 100 SD LUTs)
- 20_osdover.bin  : in-place exact-length rename of Pixel Refresh (像素更新 -> 控制中心 / ControlCenter)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEC_DIR = REPO_ROOT.parent / "s5_v29_decrypted"
DEFAULT_OUT_DIR = REPO_ROOT / "build"

# 07_postboot3.bin (high-VA runtime domain)
CAVE = 0x879F7900
MODULE = 0x87707000
CALL = 0x877537A2
ORIGINAL_GETTER = 0x87864ACC

# 03_program.bin (low-VA runtime domain: 0x06280000..0x07267000)
PROG_BASE_LOW = 0x06280000
CC_CAVE_LOW = 0x07100000
CC_STATE_LOW = 0x07108000
CC_STATE_SIZE = 0x3400  # 13 KB: CcState + 100*64B LUT list + 2KB DMA IO buf + 2920B 0xBEEF buf
KEY_DISPATCH_LOW = 0x064759C6
KEY_DISPATCH_ORIG_LOW = 0x06643680
EGL_SWAP_ENTRY_LOW = 0x064C3CDA        # eglSwapBuffers prologue: push.w {r4, r5, r6, r7, r8, lr}
EGL_SWAP_ORIG_PROLOGUE = bytes.fromhex("2de9f041")
SETUP_PIXEL_REFRESH_LOW = 0x068596AE   # Setup Menu ID 0x01B5 (像素更新 -> 控制中心)
SETUP_PIXEL_REFRESH_ORIG_BL = 0x06825F18
SETUP_DISPATCH_RET_LOW = 0x068598D0    # pop.w {r4..r8, lr}; b.w 0x0683291c (commit_ui redraw!)
FN_LUT_SELECT_LOW = 0x0686A074         # Native Fn -> LUT Select entry (BL 0x06465A02)
FN_LUT_SELECT_ORIG_BL = 0x06465A02
LV_STYLE_GATE_LOW = 0x066AE7C4         # Live View LUT Photo Style check (BL 0x066A07F4)
LV_STYLE_GATE_ORIG_BL = 0x066A07F4

# 20_osdover.bin exact-length UTF-16LE label replacements
OSDOVER_ZH_OFF = 0x876436
OSDOVER_EN_OFF = 0x551654


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_bl(source: int, target: int) -> bytes:
    disp = target - (source + 4)
    assert disp % 2 == 0 and -(1 << 24) <= disp < (1 << 24), f"BL out of range: {source:#x} -> {target:#x}"
    val = disp & 0x1FFFFFF
    s, i1, i2 = (val >> 24) & 1, (val >> 23) & 1, (val >> 22) & 1
    hw1 = 0xF000 | (s << 10) | ((val >> 12) & 0x3FF)
    hw2 = 0xD000 | ((1 ^ s ^ i1) << 13) | ((1 ^ s ^ i2) << 11) | ((val >> 1) & 0x7FF)
    code = struct.pack("<HH", hw1, hw2)
    insn = next(Cs(CS_ARCH_ARM, CS_MODE_THUMB).disasm(code, source))
    assert insn.mnemonic == "bl" and int(insn.op_str[1:], 16) == target
    return code


def make_bw(source: int, target: int) -> bytes:
    disp = target - (source + 4)
    assert disp % 2 == 0 and -(1 << 24) <= disp < (1 << 24), f"B.W out of range: {source:#x} -> {target:#x}"
    val = disp & 0x1FFFFFF
    s, i1, i2 = (val >> 24) & 1, (val >> 23) & 1, (val >> 22) & 1
    hw1 = 0xF000 | (s << 10) | ((val >> 12) & 0x3FF)
    hw2 = 0x9000 | ((1 ^ s ^ i1) << 13) | ((1 ^ s ^ i2) << 11) | ((val >> 1) & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def parse_elf32(raw: bytes) -> tuple[list[dict], list[dict]]:
    assert raw[:4] == b"\x7fELF" and raw[4] == 1 and raw[5] == 1, "Expected 32-bit LE ELF"
    e_shoff = struct.unpack_from("<I", raw, 32)[0]
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", raw, 46)
    sections = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        (
            sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size,
            sh_link, sh_info, sh_addralign, sh_entsize,
        ) = struct.unpack_from("<10I", raw, o)
        data = b"" if sh_type == 8 else raw[sh_offset:sh_offset + sh_size]
        sections.append({
            "idx": i, "name_off": sh_name, "type": sh_type, "flags": sh_flags,
            "offset": sh_offset, "size": sh_size, "link": sh_link, "info": sh_info,
            "align": max(1, sh_addralign), "entsize": sh_entsize, "data": data, "name": "",
        })
    shstr = sections[e_shstrndx]["data"]
    for s in sections:
        end = shstr.index(b"\x00", s["name_off"])
        s["name"] = shstr[s["name_off"]:end].decode("ascii", errors="replace")

    symtab_sec = next(s for s in sections if s["name"] == ".symtab")
    strtab = sections[symtab_sec["link"]]["data"]
    symbols = []
    for off in range(0, symtab_sec["size"], 16):
        st_name, st_value, st_size, st_info, st_other, st_shndx = struct.unpack_from("<IIIBBH", symtab_sec["data"], off)
        end = strtab.index(b"\x00", st_name)
        symbols.append({
            "name": strtab[st_name:end].decode("ascii", errors="replace"),
            "st_value": st_value,
            "st_shndx": st_shndx,
        })
    return sections, symbols


def link_relocatable_obj(obj_bytes: bytes, base_addr: int) -> tuple[bytearray, dict[str, int]]:
    sections, symbols = parse_elf32(obj_bytes)
    payload = bytearray()
    sec_bases: dict[int, int] = {}

    alloc_secs = [
        s for s in sections
        if (s["flags"] & 0x2) and s["size"] > 0
        and s["name"].startswith((".text", ".rodata", ".data", ".bss"))
    ]
    order_key = lambda s: (
        0 if s["name"].startswith(".text") else
        1 if s["name"].startswith(".rodata") else
        2 if s["name"].startswith(".data") else
        3 if s["name"].startswith(".bss") else 4,
        s["idx"],
    )
    for s in sorted(alloc_secs, key=order_key):
        align = max(4, s["align"])
        pad = (align - (base_addr + len(payload)) % align) % align
        if pad:
            payload.extend(b"\x00" * pad)
        sec_bases[s["idx"]] = base_addr + len(payload)
        if s["type"] == 8:
            payload.extend(b"\x00" * s["size"])
        else:
            payload.extend(s["data"])

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
            if rtype in (2, 38):  # R_ARM_ABS32 / R_ARM_TARGET1
                addend = struct.unpack_from("<I", payload, p_off)[0]
                struct.pack_into("<I", payload, p_off, (target_addr + addend) & 0xFFFFFFFF)
            elif rtype == 10:  # R_ARM_THM_CALL
                payload[p_off:p_off + 4] = make_bl(base_addr + p_off, target_addr & ~1)
            elif rtype in (30, 47):  # R_ARM_THM_JUMP24
                payload[p_off:p_off + 4] = make_bw(base_addr + p_off, target_addr & ~1)
            else:
                raise RuntimeError(f"Unsupported reloc type {rtype} for {sym['name']} in {s['name']}")

    sym_addrs = {
        sym["name"]: sec_bases[sym["st_shndx"]] + sym["st_value"]
        for sym in symbols if sym["name"] and sym["st_shndx"] in sec_bases
    }
    return payload, sym_addrs


def build_postboot3(dec_dir: Path, out_dir: Path) -> dict:
    src_dir = REPO_ROOT / "src"
    hook_c = src_dir / "hook.c"
    hook_o = out_dir / "hook.o"
    payload_bin = out_dir / "payload.bin"
    cmd = [
        "clang", "--target=armv7-none-eabi", "-mcpu=cortex-a7", "-mthumb",
        "-mno-movt", "-mfpu=none", "-mfloat-abi=soft", "-fno-vectorize",
        "-fno-slp-vectorize", "-ffreestanding", "-fno-builtin", "-fno-stack-protector",
        "-I", str(src_dir), "-O3", "-c", str(hook_c), "-o", str(hook_o),
    ]
    subprocess.run(cmd, check=True)

    payload, sym_addrs = link_relocatable_obj(hook_o.read_bytes(), CAVE)
    hook_entry = sym_addrs["image_hook"]
    payload_bin.write_bytes(payload)

    pb3 = bytearray((dec_dir / "07_postboot3.bin").read_bytes())
    call_off = CALL - MODULE
    assert pb3[call_off:call_off + 4] == make_bl(CALL, ORIGINAL_GETTER), "Original BL mismatch in 07_postboot3.bin"
    cave_off = CAVE - MODULE
    assert pb3[cave_off:cave_off + len(payload)] == b"\xff" * len(payload), "07_postboot3.bin cave not empty!"

    pb3[call_off:call_off + 4] = make_bl(CALL, hook_entry & ~1)
    pb3[cave_off:cave_off + len(payload)] = payload
    (out_dir / "07_postboot3.bin").write_bytes(pb3)
    return {
        "hook_call": f"0x{CALL:08x}",
        "payload_address": f"0x{CAVE:08x}",
        "hook_entry": f"0x{hook_entry:08x}",
        "payload_bytes": len(payload),
        "postboot3_sha256": sha256(pb3),
    }


def build_program_with_control_center(dec_dir: Path, out_dir: Path) -> dict:
    src_dir = REPO_ROOT / "src"
    patcher = REPO_ROOT / "tools" / "patch_s5_video_unlim.py"
    subprocess.run([
        sys.executable, str(patcher),
        "--src", str(dec_dir / "03_program.bin"),
        "--dst", str(out_dir / "03_program.bin"),
        "--report", str(out_dir / "patch_report.json"),
        "--with-all-i",
        "--bitrate-mbps", "300",
    ], check=True)

    cc_c = src_dir / "control_center.c"
    cc_o = out_dir / "control_center.o"
    cc_bin = out_dir / "control_center_payload.bin"
    cmd = [
        "clang", "--target=armv7-none-eabi", "-mcpu=cortex-a7", "-mthumb",
        "-mno-movt", "-mfpu=none", "-mfloat-abi=soft", "-fno-vectorize",
        "-fno-slp-vectorize", "-ffreestanding", "-fno-builtin", "-fno-stack-protector",
        "-I", str(src_dir), "-O2", "-c", str(cc_c), "-o", str(cc_o),
    ]
    subprocess.run(cmd, check=True)

    payload, sym_addrs = link_relocatable_obj(cc_o.read_bytes(), CC_CAVE_LOW)
    assert len(payload) <= (CC_STATE_LOW - CC_CAVE_LOW), "Control center payload overlaps CC_STATE_LOW!"
    cc_key_entry = sym_addrs["cc_key_hook"]
    cc_swap_entry = sym_addrs["cc_egl_swap_trampoline"]
    cc_open_entry = sym_addrs["cc_open_from_setup_menu"]
    cc_fn_lut_entry = sym_addrs["cc_fn_lut_trampoline"]
    cc_lv_gate_entry = sym_addrs["cc_lv_style_gate"]
    cc_bin.write_bytes(payload)

    prog_path = out_dir / "03_program.bin"
    prog = bytearray(prog_path.read_bytes())

    key_off = KEY_DISPATCH_LOW - PROG_BASE_LOW
    assert prog[key_off:key_off + 4] == make_bl(KEY_DISPATCH_LOW, KEY_DISPATCH_ORIG_LOW)
    prog[key_off:key_off + 4] = make_bl(KEY_DISPATCH_LOW, cc_key_entry & ~1)

    pr_off = SETUP_PIXEL_REFRESH_LOW - PROG_BASE_LOW
    assert prog[pr_off:pr_off + 4] == make_bl(SETUP_PIXEL_REFRESH_LOW, SETUP_PIXEL_REFRESH_ORIG_BL)
    prog[pr_off:pr_off + 4] = make_bl(SETUP_PIXEL_REFRESH_LOW, cc_open_entry & ~1)
    prog[pr_off + 4:pr_off + 8] = make_bw(SETUP_PIXEL_REFRESH_LOW + 4, SETUP_DISPATCH_RET_LOW)

    fn_off = FN_LUT_SELECT_LOW - PROG_BASE_LOW
    assert prog[fn_off:fn_off + 4] == make_bl(FN_LUT_SELECT_LOW, FN_LUT_SELECT_ORIG_BL)
    prog[fn_off:fn_off + 4] = make_bw(FN_LUT_SELECT_LOW, cc_fn_lut_entry & ~1)

    lvg_off = LV_STYLE_GATE_LOW - PROG_BASE_LOW
    assert prog[lvg_off:lvg_off + 4] == make_bl(LV_STYLE_GATE_LOW, LV_STYLE_GATE_ORIG_BL)
    prog[lvg_off:lvg_off + 4] = make_bl(LV_STYLE_GATE_LOW, cc_lv_gate_entry & ~1)

    swap_off = EGL_SWAP_ENTRY_LOW - PROG_BASE_LOW
    assert bytes(prog[swap_off:swap_off + 4]) == EGL_SWAP_ORIG_PROLOGUE
    prog[swap_off:swap_off + 4] = make_bw(EGL_SWAP_ENTRY_LOW, cc_swap_entry & ~1)

    cave_off = CC_CAVE_LOW - PROG_BASE_LOW
    state_off = CC_STATE_LOW - PROG_BASE_LOW
    assert prog[cave_off:cave_off + len(payload)] == b"\xff" * len(payload)
    assert prog[state_off:state_off + CC_STATE_SIZE] == b"\xff" * CC_STATE_SIZE
    prog[cave_off:cave_off + len(payload)] = payload
    prog[state_off:state_off + CC_STATE_SIZE] = b"\x00" * CC_STATE_SIZE

    prog_path.write_bytes(prog)
    return {
        "base_patches": json.loads((out_dir / "patch_report.json").read_text()),
        "control_center": {
            "payload_address_low": f"0x{CC_CAVE_LOW:08x}",
            "state_address_low": f"0x{CC_STATE_LOW:08x}",
            "payload_bytes": len(payload),
            "key_dispatch_hook": f"0x{KEY_DISPATCH_LOW:08x}",
            "cc_key_hook_entry": f"0x{cc_key_entry:08x}",
            "setup_menu_hook": f"0x{SETUP_PIXEL_REFRESH_LOW:08x}",
            "cc_open_entry": f"0x{cc_open_entry:08x}",
            "egl_swap_inline_hook": f"0x{EGL_SWAP_ENTRY_LOW:08x}",
            "cc_egl_swap_entry": f"0x{cc_swap_entry:08x}",
        },
        "program_sha256": sha256(prog),
    }


def build_osdover(dec_dir: Path, out_dir: Path) -> dict:
    osd = bytearray((dec_dir / "20_osdover.bin").read_bytes())
    zh_old, zh_new = "像素更新".encode("utf-16le"), "控制中心".encode("utf-16le")
    assert osd[OSDOVER_ZH_OFF:OSDOVER_ZH_OFF + len(zh_old)] == zh_old
    osd[OSDOVER_ZH_OFF:OSDOVER_ZH_OFF + len(zh_new)] = zh_new

    en_old, en_new = "Pixel Refresh".encode("utf-16le"), "ControlCenter".encode("utf-16le")
    assert osd[OSDOVER_EN_OFF:OSDOVER_EN_OFF + len(en_old)] == en_old
    osd[OSDOVER_EN_OFF:OSDOVER_EN_OFF + len(en_new)] = en_new

    (out_dir / "20_osdover.bin").write_bytes(osd)
    return {
        "zh_patch": {"offset": f"0x{OSDOVER_ZH_OFF:x}", "old": "像素更新", "new": "控制中心"},
        "en_patch": {"offset": f"0x{OSDOVER_EN_OFF:x}", "old": "Pixel Refresh", "new": "ControlCenter"},
        "sha256": sha256(osd),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build patched DC-S5 V2.9 firmware components")
    ap.add_argument("--dec-dir", type=Path, default=DEFAULT_DEC_DIR, help="Directory containing decrypted S5 V2.9 .bin files")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output directory for built components")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    prog_info = build_program_with_control_center(args.dec_dir, args.out_dir)
    pb3_info = build_postboot3(args.dec_dir, args.out_dir)
    osd_info = build_osdover(args.dec_dir, args.out_dir)
    manifest = {"03_program": prog_info, "07_postboot3": pb3_info, "20_osdover": osd_info}
    (args.out_dir / "combined_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    cc = prog_info["control_center"]
    print(f"Built 03_program.bin + Control Center ({cc['payload_bytes']}B @ {cc['payload_address_low']})")
    print(f"Built 07_postboot3.bin ({pb3_info['payload_bytes']}B payload @ {pb3_info['payload_address']})")
    print(f"Built 20_osdover.bin -> {args.out_dir / '20_osdover.bin'}")


if __name__ == "__main__":
    main()
