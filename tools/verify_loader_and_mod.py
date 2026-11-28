#!/usr/bin/env python3
# RULES-ACTIVE-GLOBAL
"""Verify the split S5 V2.9 Base Loader + SD-card S5_MOD.BIN architecture in Unicorn ARM emulation:
- Scenario A: No SD card / no S5_MOD.BIN -> 100% factory stock fallback on all 6 hooks
- Scenario B: Corrupted S5_MOD.BIN (1 bit flipped) -> CRC32 rejection, safe stock fallback
- Scenario C: Valid A:\\S5_MOD.BIN -> hot-loaded into 0x07100000, full Control Center + S5II Live LUT Bar + 100 SD LUTs
- Scenario D: Cross-domain 07_postboot3.bin (0x879F7900) -> S5ModTable -> S5_MOD.BIN (0x0710xxxx) JPEG bake!
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm_const import (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R4, UC_ARM_REG_R8, UC_ARM_REG_LR, UC_ARM_REG_SP,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OCTANT_BASE_OFFSETS = (0, 125, 225, 325, 405, 505, 585, 665)


def make_harness(prog_bytes: bytes, pb3_bytes: bytes, virtual_sd: dict[str, bytes]):
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    uc.mem_map(0x06280000, 0x01000000)
    uc.mem_write(0x06280000, prog_bytes)

    uc.mem_map(0x87700000, 0x00400000)
    uc.mem_write(0x87707000, pb3_bytes[:0x2F8000])

    uc.mem_map(0x08A9D000, 0x1000)
    uc.mem_map(0x08AA0000, 0x2000)
    uc.mem_write(0x08A9D47C + 4, struct.pack("<I", 0x062B6C00))
    uc.mem_map(0x08BDC000, 0x1000)
    uc.mem_map(0x08CC1000, 0x5000)
    uc.mem_write(0x08CC1864, bytes([0x0B]))  # V-Log 0x0B
    uc.mem_map(0x0A000000, 0x4000)  # Stack + event scratch

    # High-VA JPEG plane buffers for Scenario D
    uc.mem_map(0x8E000000, 0x10000)
    uc.mem_map(0x90000000, 0x01000000)
    uc.mem_map(0x92000000, 0x01000000)

    FN_KEY_DISPATCH = 0x06643680
    FN_ORIG_PIXEL   = 0x06825F18
    FN_ORIG_FN_LUT  = 0x06465A02
    FN_EGL_SWAP_CONT = 0x064C3CDE
    FN_EGL_QUERY    = 0x064C2180
    FN_VG_SETI      = 0x065D93D6
    FN_VG_GETI      = 0x065D9450
    FN_VG_SETFV     = 0x065D93F2
    FN_VG_CLEAR     = 0x065D9B0C
    FN_FS_PARSE     = 0x06426DA6
    FN_FS_OPEN      = 0x067DC0D4
    FN_FS_SEEK      = 0x067ADAC8
    FN_FS_READ      = 0x06432F00
    FN_FS_WRITE     = 0x067302D4
    FN_FS_CLOSE     = 0x067DA160
    FN_COMMIT_UI    = 0x0683291C
    FN_LV_LUT_APPLY = 0x066AE6BC
    FN_PB3_GET_DESC = 0x87864ACC

    for addr in (FN_KEY_DISPATCH, FN_EGL_QUERY, FN_VG_SETI, FN_VG_GETI, FN_VG_SETFV, FN_VG_CLEAR):
        uc.mem_write(addr, struct.pack("<HH", 0x2001, 0x4770))
    uc.mem_write(FN_EGL_SWAP_CONT, bytes.fromhex("0120bde8f081"))
    for addr in (FN_ORIG_PIXEL, FN_ORIG_FN_LUT, FN_FS_PARSE, FN_FS_OPEN, FN_FS_SEEK, FN_FS_READ,
                 FN_FS_WRITE, FN_FS_CLOSE, FN_COMMIT_UI, FN_LV_LUT_APPLY):
        uc.mem_write(addr, struct.pack("<HH", 0x4770, 0x4770))
    uc.mem_write(FN_PB3_GET_DESC, struct.pack("<HH", 0x2000, 0x4770))

    # Stub 0x068596B2 & 0x0686A078 continuation traps for stock fallback checks
    RET_TRAP = 0x0A003F00
    logs = {
        "orig_key": [], "orig_pixel": 0, "orig_fn_lut": 0,
        "lv_apply": [], "clear_ops": 0, "commit_ui": 0,
    }
    open_fds: dict[int, list] = {}
    next_fd = [100]

    def read_cstr(u, ptr: int) -> str:
        return bytes(u.mem_read(ptr, 64)).split(b"\x00", 1)[0].decode("ascii", errors="replace")

    def hook_code(u, address, size, user):
        if address == RET_TRAP:
            u.emu_stop()
            return
        if address == FN_KEY_DISPATCH:
            ev = u.reg_read(UC_ARM_REG_R0)
            logs["orig_key"].append(struct.unpack("<II", u.mem_read(ev, 8)))
        elif address == FN_ORIG_PIXEL:
            logs["orig_pixel"] += 1
            u.emu_stop()
        elif address == FN_ORIG_FN_LUT:
            logs["orig_fn_lut"] += 1
            u.emu_stop()
        elif address == FN_COMMIT_UI:
            logs["commit_ui"] += 1
        elif address == FN_LV_LUT_APPLY:
            logs["lv_apply"].append((u.reg_read(UC_ARM_REG_R0), u.reg_read(UC_ARM_REG_R1), u.reg_read(UC_ARM_REG_R2)))
        elif address == FN_VG_CLEAR:
            logs["clear_ops"] += 1
        elif address == FN_EGL_QUERY:
            attr, out_p = u.reg_read(UC_ARM_REG_R2), u.reg_read(UC_ARM_REG_R3)
            if attr == 0x3057:
                u.mem_write(out_p, struct.pack("<i", 960))
            elif attr == 0x3056:
                u.mem_write(out_p, struct.pack("<i", 640))
        elif address == FN_FS_PARSE:
            p_str, out_drv, out_rel = u.reg_read(UC_ARM_REG_R0), u.reg_read(UC_ARM_REG_R1), u.reg_read(UC_ARM_REG_R2)
            path = read_cstr(u, p_str)
            drv = 1 if path.startswith("A:") else (2 if path.startswith("B:") else 0)
            u.mem_write(out_drv, struct.pack("<I", drv))
            u.mem_write(out_rel, struct.pack("<I", p_str))
            u.reg_write(UC_ARM_REG_R0, 0 if drv else 1)
        elif address == FN_FS_OPEN:
            out_fd, rel_p, mode = u.reg_read(UC_ARM_REG_R1), u.reg_read(UC_ARM_REG_R2), u.reg_read(UC_ARM_REG_R3)
            path = read_cstr(u, rel_p)
            if mode == 0x0401:
                if path not in virtual_sd:
                    u.reg_write(UC_ARM_REG_R0, 0xA406)
                    return
            elif mode == 0x0800:
                if path in virtual_sd:
                    u.reg_write(UC_ARM_REG_R0, 0xA405)
                    return
                virtual_sd[path] = b""
            elif mode == 0x0200:
                virtual_sd[path] = b""
            fd = next_fd[0]
            next_fd[0] += 1
            open_fds[fd] = [path, mode, 0]
            u.mem_write(out_fd, struct.pack("<I", fd))
            u.reg_write(UC_ARM_REG_R0, 0)
        elif address == FN_FS_READ:
            fd, buf_p, length = u.reg_read(UC_ARM_REG_R0), u.reg_read(UC_ARM_REG_R1), u.reg_read(UC_ARM_REG_R3)
            sp = u.reg_read(UC_ARM_REG_SP)
            stat_p = struct.unpack("<I", u.mem_read(sp + 4, 4))[0]
            ent = open_fds[fd]
            blob = virtual_sd.get(ent[0], b"")[ent[2]:ent[2] + length]
            ent[2] += len(blob)
            if blob:
                u.mem_write(buf_p, blob)
            u.mem_write(stat_p, struct.pack("<II", 0, len(blob)))
            u.reg_write(UC_ARM_REG_R0, 0)
        elif address == FN_FS_WRITE:
            fd, buf_p, length = u.reg_read(UC_ARM_REG_R0), u.reg_read(UC_ARM_REG_R1), u.reg_read(UC_ARM_REG_R3)
            sp = u.reg_read(UC_ARM_REG_SP)
            stat_p = struct.unpack("<I", u.mem_read(sp + 4, 4))[0]
            blob = bytes(u.mem_read(buf_p, length))
            virtual_sd[open_fds[fd][0]] = blob
            u.mem_write(stat_p, struct.pack("<II", 0, len(blob)))
            u.reg_write(UC_ARM_REG_R0, 0)
        elif address == FN_FS_CLOSE:
            open_fds.pop(u.reg_read(UC_ARM_REG_R0), None)
            u.reg_write(UC_ARM_REG_R0, 0)

    uc.hook_add(UC_HOOK_CODE, hook_code)
    return uc, logs, RET_TRAP


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify S5 Base Loader + SD S5_MOD.BIN in Unicorn")
    ap.add_argument("--build-dir", type=Path, default=REPO_ROOT / "build_loader")
    ap.add_argument("--mod-bin", type=Path, default=REPO_ROOT / "S5_MOD.BIN")
    args = ap.parse_args()

    prog = (args.build_dir / "03_program.bin").read_bytes()
    pb3 = (args.build_dir / "07_postboot3.bin").read_bytes()
    mod_blob = args.mod_bin.read_bytes()
    man = json.loads((args.build_dir / "loader_manifest.json").read_text())

    ldr_key = int(man["03_program"]["ldr_key_hook"], 16) | 1

    # --- Scenario A: Empty SD card (no S5_MOD.BIN) -> 100% stock fallback! ---
    uc, logs, RET_TRAP = make_harness(prog, pb3, {})

    def send_key(u, k: int, a: int):
        u.mem_write(0x0A003000, struct.pack("<II", k, a))
        u.reg_write(UC_ARM_REG_SP, 0x0A002FF0)
        u.reg_write(UC_ARM_REG_R0, 0x0A003000)
        u.reg_write(UC_ARM_REG_LR, RET_TRAP | 1)
        u.emu_start(ldr_key, RET_TRAP, count=4000000)

    send_key(uc, 3, 1)
    assert logs["orig_key"] == [(3, 1)]
    assert uc.mem_read(0x070FC000 + 8, 2) == b"\x00\x01"  # loaded=0, probed=1

    # Setup menu & Fn LUT Select both fall back cleanly to stock!
    uc.reg_write(UC_ARM_REG_SP, 0x0A002FD8)
    uc.emu_start(0x068596AE | 1, RET_TRAP, count=2000000)
    assert logs["orig_pixel"] == 1
    uc.reg_write(UC_ARM_REG_SP, 0x0A002F80)
    uc.emu_start(0x0686A074 | 1, RET_TRAP, count=2000000)
    assert logs["orig_fn_lut"] == 1

    # eglSwapBuffers draws 0 rects
    uc.reg_write(UC_ARM_REG_SP, 0x0A002FF0)
    uc.reg_write(UC_ARM_REG_R0, 1)
    uc.reg_write(UC_ARM_REG_R1, 2)
    uc.reg_write(UC_ARM_REG_LR, RET_TRAP | 1)
    uc.emu_start(0x064C3CDA | 1, RET_TRAP, count=500000)
    assert logs["clear_ops"] == 0
    print("PASS Scenario A (No SD card / no S5_MOD.BIN): 100% stock fallback on all hooks")

    # --- Verify all updater version & SHA-256 dispatch patches in 03_program.bin ---
    for va, exp in (
        (0x8681E9A2, 0xBF00),
        (0x8681E9DE, 0xE009),
        (0x86867D12, 0xBF00),
        (0x868C704E, 0xE023),
        (0x868C70A4, 0xE7F8),
    ):
        got = struct.unpack_from("<H", prog, va - 0x86280000)[0]
        assert got == exp, f"Patch check failed @ 0x{va:08X}: got 0x{got:04X}, expected 0x{exp:04X}"

    # --- Scenario B: Corrupted S5_MOD.BIN (1 bit flipped) -> CRC32 rejection! ---
    bad_mod = bytearray(mod_blob)
    bad_mod[64] ^= 0x01
    uc_b, logs_b, RET_B = make_harness(prog, pb3, {"A:\\S5_MOD.BIN": bytes(bad_mod)})
    send_key(uc_b, 3, 1)
    assert uc_b.mem_read(0x070FC000 + 8, 3) == b"\x00\x01\x03"  # loaded=0, probed=1, load_err=3 (bad_crc)
    assert logs_b["orig_key"] == [(3, 1)]
    print("PASS Scenario B (Corrupted S5_MOD.BIN): CRC32 mismatch rejected safely (load_err=3)")

    # --- Scenario B2: Corrupted A:\S5_MOD.BIN + valid B:\S5_MOD.BIN -> Dual-slot fallback to B:! ---
    uc_b2, logs_b2, RET_B2 = make_harness(
        prog, pb3, {"A:\\S5_MOD.BIN": bytes(bad_mod), "B:\\S5_MOD.BIN": mod_blob}
    )
    send_key(uc_b2, 3, 1)
    assert uc_b2.mem_read(0x070FC000 + 8, 4) == b"\x01\x01\x00\x01", "Dual-card fallback to B: failed!"
    assert logs_b2["orig_key"] == [(3, 1)]
    print("PASS Scenario B2 (Corrupted A:\\ + Valid B:\\): Dual-card fallback loaded B:\\S5_MOD.BIN (drive_idx=1)")

    # --- Scenario C & D: Valid A:\S5_MOD.BIN + SD .CUBE LUT -> full hot-load & cross-domain JPEG bake! ---
    cube_lines = ["TITLE \"Kodak2383\"\n", "LUT_3D_SIZE 17\n"]
    for b in range(17):
        for g in range(17):
            for r in range(17):
                cube_lines.append(f"{1.0 - r/16.0:.4f} {1.0 - g/16.0:.4f} {1.0 - b/16.0:.4f}\n")
    sd_c = {
        "A:\\S5_MOD.BIN": mod_blob,
        "A:\\LUT\\LIST.TXT": b"Kodak2383.cube\r\n",
        "A:\\LUT\\Kodak2383.cube": "".join(cube_lines).encode("ascii"),
    }
    uc_c, logs_c, RET_C = make_harness(prog, pb3, sd_c)

    # First key press hot-loads S5_MOD.BIN from SD into 0x07100000!
    send_key(uc_c, 3, 1)
    assert uc_c.mem_read(0x070FC000 + 8, 3) == b"\x01\x01\x00", "S5_MOD.BIN failed to load!"
    assert logs_c["orig_key"] == [(3, 1)]

    # Click Setup -> 控制中心 -> opens Control Center (visible == 1 @ 0x07200000 + 4)
    uc_c.mem_write(0x0A002FD8, struct.pack("<6I", 0x11, 0x22, 0x33, 0x44, 0x55, RET_C | 1))
    uc_c.reg_write(UC_ARM_REG_SP, 0x0A002FD8)
    uc_c.emu_start(0x068596AE | 1, RET_C, count=4000000)
    assert uc_c.mem_read(0x07200000 + 4, 1)[0] == 1, "Hot-loaded Control Center did not open!"
    assert logs_c["commit_ui"] >= 1

    # Press RIGHT -> loads SD01 Kodak2383.cube into 0x07202200 & syncs Live View ISP!
    send_key(uc_c, 6, 1)
    assert uc_c.mem_read(0x07200000 + 12, 1)[0] == 1
    assert struct.unpack("<I", uc_c.mem_read(0x08A9D47C + 4, 4))[0] == 0x07202200
    assert logs_c["lv_apply"][-1] == (1, 1, 1)

    # Render OpenVG overlay through ldr_egl_swap_trampoline -> S5_MOD.BIN
    uc_c.reg_write(UC_ARM_REG_SP, 0x0A002FF0)
    uc_c.reg_write(UC_ARM_REG_R0, 1)
    uc_c.reg_write(UC_ARM_REG_R1, 2)
    uc_c.reg_write(UC_ARM_REG_LR, RET_C | 1)
    uc_c.emu_start(0x064C3CDA | 1, RET_C, count=2000000)
    assert logs_c["clear_ops"] > 200
    print(f"PASS Scenario C (Hot-loaded S5_MOD.BIN): Control Center + SD LUT + Live View ISP + OpenVG ({logs_c['clear_ops']} ops)")

    # Scenario D: Call 07_postboot3.bin @ 0x877537A2 -> pb3_image_bridge -> S5ModTable -> S5_MOD.BIN JPEG bake!
    desc = 0x8E000000
    stride, width, height = 6016, 6000, 4000
    plane_sz = stride * height
    data = bytearray(0x38)
    struct.pack_into("<IIHHIIHHHHI", data, 0, 0x90000000, plane_sz, stride, height,
                     0x92000000, plane_sz, stride, height, 0, 0, 0)
    struct.pack_into("<HHI", data, 0x1C, width, height, 2)
    uc_c.mem_write(desc, bytes(data))
    uc_c.mem_write(0x90000000, bytes([100]) * (stride * 2))
    uc_c.mem_write(0x92000000, bytes([0]) * (stride * 2))

    uc_c.reg_write(UC_ARM_REG_SP, 0x0A002FF0)
    uc_c.reg_write(UC_ARM_REG_R0, 1)
    uc_c.reg_write(UC_ARM_REG_R1, desc)
    uc_c.reg_write(UC_ARM_REG_LR, RET_C | 1)
    uc_c.emu_start(0x877537A2 | 1, 0x877537A6, count=12000000)
    y_out = list(uc_c.mem_read(0x90000000, 8))
    assert 150 <= y_out[0] <= 160 and len(set(y_out)) == 1, f"Cross-domain JPEG bake failed: {y_out}"
    print(f"PASS Scenario D (Cross-domain 07_postboot3 -> S5_MOD.BIN JPEG bake): Y={y_out[:4]}")


if __name__ == "__main__":
    main()
