#!/usr/bin/env python3
# RULES-ACTIVE-GLOBAL
"""Verify S5 V2.9 Control Center (03_program.bin: OpenVG overlay + key hook)
and decoupled Direct-YCbCr Real-Time LUT hook (07_postboot3.bin) in Unicorn ARM emulation,
and render a real 960x640 PNG preview of the Control Center OpenVG output."""
from pathlib import Path
import json
import struct
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm_const import (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
    UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11,
    UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP,
)

import argparse
_ap = argparse.ArgumentParser(description="Verify patched S5 V2.9 components in Unicorn")
_ap.add_argument("--build-dir", type=Path, default=Path(__file__).resolve().parent.parent / "build")
DIR = _ap.parse_args().build_dir
PROGRAM_PATH = DIR / "03_program.bin"
PREVIEW_PNG = DIR / "control_center_preview.png"
manifest = json.loads((DIR / "combined_manifest.json").read_text())
pb3_info = manifest["07_postboot3"]
cc_info = manifest["03_program"]["control_center"]

patched_prog = PROGRAM_PATH.read_bytes()
patched_pb3 = (DIR / "07_postboot3.bin").read_bytes()
payload = (DIR / "payload.bin").read_bytes()

call = int(pb3_info["hook_call"], 16)
cave = int(pb3_info["payload_address"], 16)
resume = call + 4

descriptor = 0x8E000000
stack = 0x8F000000
stride = 6016
y_address, c_address = 0x90000000, 0x92000000

OCTANT_BASE_OFFSETS = (0, 125, 225, 325, 405, 505, 585, 665)


def build_synthetic_beef_lut9_invert() -> bytes:
    oct_off = 0x86E39EB4 - 0x86280000
    entries = [(0, 0)] * 729
    for b in range(9):
        for g in range(9):
            for r in range(9):
                tnum, idx = struct.unpack_from("<HH", patched_prog, oct_off + (g * 81 + b * 9 + r) * 4)
                slot = OCTANT_BASE_OFFSETS[tnum] + idx
                r6 = 63 - ((r * 63 + 4) // 8)
                g6 = 63 - ((g * 63 + 4) // 8)
                b6 = 63 - ((b * 63 + 4) // 8)
                w0 = (r6 & 0x3F) | ((b6 & 0x3F) << 6) | ((g6 & 0x0F) << 12)
                w1 = (g6 >> 4) & 0x03
                entries[slot] = (w0, w1)
    buf = bytearray(struct.pack("<HH", 0xBEEF, 1))
    for w0, w1 in entries:
        buf += struct.pack("<HH", w0, w1)
    assert len(buf) == 2920
    return bytes(buf)


def run_lut_case(width: int, lut_id: int, view_assist_monitor: int = 0,
                 with_slot1_buf: bool = False, max_insns: int = 120000, height: int = 4000,
                 y_in: int = 100, uv_in: int = 0x00, slot1_addr: int = 0x062B7000,
                 fill_rows: int = 2, photo_style: int = 0x0B, cc_all_styles: int = 0,
                 sd_lut_buf=None):
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    uc.mem_map(0x86000000, 0x02000000)
    uc.mem_write(0x86280000, patched_prog)
    uc.mem_write(0x87707000, patched_pb3[:0x2F0000])
    uc.mem_write(cave, payload)
    uc.mem_write(call, patched_pb3[call - 0x87707000:call - 0x87707000 + 4])

    # Map Control Center shared state @ 0x07108000..0x0710B000
    uc.mem_map(0x07108000, 0x4000)
    if cc_all_styles or sd_lut_buf is not None:
        hdr = bytearray(32)
        struct.pack_into("<I", hdr, 0, 0x53354343)
        hdr[6] = 1 if cc_all_styles else 0
        hdr[12] = 1 if sd_lut_buf is not None else 0
        uc.mem_write(0x07108000, bytes(hdr))
        if sd_lut_buf is not None:
            uc.mem_write(0x0710A200, sd_lut_buf)

    uc.mem_map(0x08BDC000, 0x1000)
    uc.mem_write(0x08BDC310, struct.pack("<II", view_assist_monitor, 0))
    uc.mem_write(0x08BDC330, struct.pack("<I", lut_id))

    uc.mem_map(0x08CC1000, 0x5000)
    uc.mem_write(0x08CC1864, bytes([photo_style]))
    uc.mem_write(0x08CC576A, bytes([lut_id]))

    uc.mem_map(0x062B0000, 0x10000)
    uc.mem_map(0x08A9D000, 0x1000)
    if with_slot1_buf:
        uc.mem_write(0x08A9D47C + 4, struct.pack("<I", slot1_addr))
        uc.mem_write(slot1_addr, build_synthetic_beef_lut9_invert())

    uc.mem_map(0x0B3D0000, 0x10000)
    uc.mem_map(descriptor, 0x20000)
    uc.mem_map(stack + 0xE000, 0x1000)  # Strict 4 KB stack guard
    uc.mem_map(y_address, 0x1800000)
    uc.mem_map(c_address, 0x1800000)

    plane_sz = stride * height
    data = bytearray(0x38)
    struct.pack_into("<IIHHIIHHHHI", data, 0, y_address, plane_sz, stride, height,
                     c_address, plane_sz, stride, height, 0, 0, 0)
    struct.pack_into("<HHI", data, 0x1C, width, height, 2)
    uc.mem_write(0x0B3D4224, bytes(data))
    uc.mem_write(y_address, bytes([y_in]) * (stride * fill_rows))
    uc.mem_write(c_address, bytes([uv_in]) * (stride * fill_rows))

    uc.reg_write(UC_ARM_REG_SP, stack + 0xEFF0)
    uc.reg_write(UC_ARM_REG_R0, 1)
    uc.reg_write(UC_ARM_REG_R1, descriptor)
    for reg in (UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
                UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11):
        uc.reg_write(reg, 0x12345678)

    stopped = []

    def stop_on_return(uc_inst, address, size, user):
        if address == resume:
            stopped.append(address)
            uc_inst.emu_stop()

    uc.hook_add(UC_HOOK_CODE, stop_on_return, begin=resume, end=resume)
    uc.emu_start(call | 1, 0, count=max_insns)
    y_all = bytes(uc.mem_read(y_address, stride * fill_rows))
    c0 = bytes(uc.mem_read(c_address, 16))
    return bool(stopped), uc.reg_read(UC_ARM_REG_R0), y_all, c0


def verify_control_center_and_render_png():
    """Emulate cc_key_hook + cc_egl_swap_buffers in low-VA 0x06280000 domain and render 960x640 PNG."""
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    uc.mem_map(0x06280000, 0x01000000)
    uc.mem_write(0x06280000, patched_prog)

    uc.mem_map(0x08A9D000, 0x1000)
    uc.mem_map(0x08AA0000, 0x2000)
    uc.mem_write(0x08A9D47C + 4, struct.pack("<I", 0x062B6C00))  # Original Slot 1 pointer (empty 0x0000)
    uc.mem_write(0x08A9D47C + 8, struct.pack("<I", 0x062B7800))
    uc.mem_write(0x08A9D47C + 12, struct.pack("<I", 0x062B8400))
    uc.mem_write(0x08A9D47C + 16, struct.pack("<I", 0x062B9000))
    uc.mem_map(0x08BDC000, 0x1000)
    uc.mem_map(0x08CC1000, 0x5000)
    uc.mem_write(0x08CC1864, bytes([0x0B]))  # Real S5 V2.9 V-Log Photo Style = 0x0B!
    uc.mem_map(0x0A000000, 0x4000)  # Stack + key event scratch

    # Seed synthetic .CUBE (with UTF-8 BOM, LUT_1D_SIZE 4 shaper, lowercase header, NO trailing newline at EOF!)
    # and .VLT (17^3 int 0..4095 WITHOUT VLT_VERSION header — tests .vlt extension detection!)
    cube_lines = [
        "\ufefftitle \"Kodak2383\"\n",
        "lut_1d_size 4\n",
        "0.0 0.0 0.0\n", "0.33 0.33 0.33\n", "0.66 0.66 0.66\n", "1.0 1.0 1.0\n",
        "lut_3d_size 17\n",
    ]
    vlt_lines = ["# Panasonic VLT\n", "LUT_3D_SIZE 17\n"]
    for b in range(17):
        for g in range(17):
            for r in range(17):
                rf, gf, bf = 1.0 - r / 16.0, 1.0 - g / 16.0, 1.0 - b / 16.0
                cube_lines.append(f"{rf:.4f} {gf:.4f} {bf:.4f}\n")
                ri, gi, bi = int(round(rf * 4095)), int(round(gf * 4095)), int(round(bf * 4095))
                vlt_lines.append(f"{ri} {gi} {bi}\n")
    # Deliberately strip trailing newline from .CUBE to test EOF flush bugfix!
    cube_blob = "".join(cube_lines).rstrip("\r\n").encode("utf-8")

    # In-memory virtual SD card filesystem backing A:\S5_CC.CFG and A:\LUT\* (with BOM + trailing spaces!)
    virtual_sd: dict[str, bytes] = {
        "A:\\LUT\\LIST.TXT": b"\xef\xbb\xbfKodak2383.cube   \r\nFuji_NC.vlt\t\r\n",
        "A:\\LUT\\Kodak2383.cube": cube_blob,
        "A:\\LUT\\Fuji_NC.vlt": "".join(vlt_lines).encode("ascii"),
    }
    open_fds: dict[int, list] = {}
    next_fd = [100]

    # Seed original Focus Beep ID1 sample count @ 0x06E35134
    orig_beep_cnt = struct.unpack_from("<I", uc.mem_read(0x06E35134, 4))[0]
    if orig_beep_cnt == 0:
        orig_beep_cnt = 0x1234
        uc.mem_write(0x06E35134, struct.pack("<I", orig_beep_cnt))

    # Stub original firmware targets:
    # Note: eglSwapBuffers @ 0x064C3CDA is inline-hooked -> jumps to 0x064C3CDE after push.w {r4..r8, lr}!
    # So stub 0x064C3CDE with `movs r0, #1; pop.w {r4, r5, r6, r7, r8, pc}` (2001 bde8f081)!
    FN_KEY_DISPATCH = 0x06643680
    FN_EGL_SWAP_CONT = 0x064C3CDE
    FN_EGL_QUERY = 0x064C2180
    FN_VG_SETI = 0x065D93D6
    FN_VG_GETI = 0x065D9450
    FN_VG_SETFV = 0x065D93F2
    FN_VG_CLEAR = 0x065D9B0C

    FN_FS_PARSE = 0x06426DA6
    FN_FS_OPEN  = 0x067DC0D4
    FN_FS_SEEK  = 0x067ADAC8
    FN_FS_READ  = 0x06432F00
    FN_FS_WRITE = 0x067302D4
    FN_FS_CLOSE = 0x067DA160
    FN_COMMIT_UI = 0x0683291C
    FN_LV_LUT_APPLY = 0x066AE6BC

    for addr in (FN_KEY_DISPATCH, FN_EGL_QUERY, FN_VG_SETI, FN_VG_GETI, FN_VG_SETFV, FN_VG_CLEAR):
        uc.mem_write(addr, struct.pack("<HH", 0x2001, 0x4770))
    uc.mem_write(FN_EGL_SWAP_CONT, bytes.fromhex("0120bde8f081"))  # movs r0, #1; pop.w {r4..r8, pc}
    for addr in (FN_FS_PARSE, FN_FS_OPEN, FN_FS_SEEK, FN_FS_READ, FN_FS_WRITE, FN_FS_CLOSE, FN_COMMIT_UI, FN_LV_LUT_APPLY):
        uc.mem_write(addr, struct.pack("<HH", 0x4770, 0x4770))

    def read_cstr(uc_inst, ptr: int) -> str:
        raw = bytes(uc_inst.mem_read(ptr, 64))
        return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")

    orig_key_calls = []
    commit_ui_calls = [0]
    lv_apply_calls = []
    swap_calls = []
    scissor_log = []
    clear_ops = []
    cur_color = [0.0, 0.0, 0.0, 1.0]
    RET_TRAP = 0x0A003F00

    def hook_code(uc_inst, address, size, user):
        if address == RET_TRAP:
            uc_inst.emu_stop()
            return
        if address == FN_KEY_DISPATCH:
            ev_ptr = uc_inst.reg_read(UC_ARM_REG_R0)
            k, a = struct.unpack_from("<II", uc_inst.mem_read(ev_ptr, 8))
            orig_key_calls.append((k, a))
        elif address == FN_COMMIT_UI:
            commit_ui_calls[0] += 1
        elif address == FN_LV_LUT_APPLY:
            lv_apply_calls.append((
                uc_inst.reg_read(UC_ARM_REG_R0),
                uc_inst.reg_read(UC_ARM_REG_R1),
                uc_inst.reg_read(UC_ARM_REG_R2),
            ))
        elif address == FN_EGL_SWAP_CONT:
            swap_calls.append((uc_inst.reg_read(UC_ARM_REG_R0), uc_inst.reg_read(UC_ARM_REG_R1)))
        elif address == FN_EGL_QUERY:
            attr = uc_inst.reg_read(UC_ARM_REG_R2)
            out_ptr = uc_inst.reg_read(UC_ARM_REG_R3)
            if attr == 0x3057:  # EGL_WIDTH
                uc_inst.mem_write(out_ptr, struct.pack("<i", 960))
            elif attr == 0x3056:  # EGL_HEIGHT
                uc_inst.mem_write(out_ptr, struct.pack("<i", 640))
        elif address == FN_VG_GETI:
            pass
        elif address == FN_VG_SETI:
            scissor_log.append((uc_inst.reg_read(UC_ARM_REG_R0), uc_inst.reg_read(UC_ARM_REG_R1)))
        elif address == FN_VG_SETFV:
            ptr = uc_inst.reg_read(UC_ARM_REG_R2)
            cur_color[:] = struct.unpack("<4f", uc_inst.mem_read(ptr, 16))
        elif address == FN_VG_CLEAR:
            x = struct.unpack("<i", struct.pack("<I", uc_inst.reg_read(UC_ARM_REG_R0)))[0]
            y = struct.unpack("<i", struct.pack("<I", uc_inst.reg_read(UC_ARM_REG_R1)))[0]
            w = struct.unpack("<i", struct.pack("<I", uc_inst.reg_read(UC_ARM_REG_R2)))[0]
            h = struct.unpack("<i", struct.pack("<I", uc_inst.reg_read(UC_ARM_REG_R3)))[0]
            clear_ops.append((x, y, w, h, tuple(cur_color)))
        elif address == FN_FS_PARSE:
            p_str = uc_inst.reg_read(UC_ARM_REG_R0)
            out_drv = uc_inst.reg_read(UC_ARM_REG_R1)
            out_rel = uc_inst.reg_read(UC_ARM_REG_R2)
            path = read_cstr(uc_inst, p_str)
            drv = 1 if path.startswith("A:") else (2 if path.startswith("B:") else 0)
            uc_inst.mem_write(out_drv, struct.pack("<I", drv))
            uc_inst.mem_write(out_rel, struct.pack("<I", p_str))
            uc_inst.reg_write(UC_ARM_REG_R0, 0 if drv else 1)
        elif address == FN_FS_OPEN:
            out_fd = uc_inst.reg_read(UC_ARM_REG_R1)
            rel_ptr = uc_inst.reg_read(UC_ARM_REG_R2)
            mode = uc_inst.reg_read(UC_ARM_REG_R3)
            path = read_cstr(uc_inst, rel_ptr)
            if mode == 0x0401:  # READ
                if path not in virtual_sd:
                    uc_inst.reg_write(UC_ARM_REG_R0, 0xA406)
                    return
            elif mode == 0x0800:  # CREATE_NEW
                if path in virtual_sd:
                    uc_inst.reg_write(UC_ARM_REG_R0, 0xA405)
                    return
                virtual_sd[path] = b""
            elif mode == 0x0200:  # TRUNC
                virtual_sd[path] = b""
            fd = next_fd[0]
            next_fd[0] += 1
            open_fds[fd] = [path, mode, 0]
            uc_inst.mem_write(out_fd, struct.pack("<I", fd))
            uc_inst.reg_write(UC_ARM_REG_R0, 0)
        elif address == FN_FS_SEEK:
            uc_inst.reg_write(UC_ARM_REG_R0, 0)
        elif address == FN_FS_READ:
            fd = uc_inst.reg_read(UC_ARM_REG_R0)
            buf_ptr = uc_inst.reg_read(UC_ARM_REG_R1)
            length = uc_inst.reg_read(UC_ARM_REG_R3)
            sp = uc_inst.reg_read(UC_ARM_REG_SP)
            stat_ptr = struct.unpack("<I", uc_inst.mem_read(sp + 4, 4))[0]
            entry = open_fds[fd]
            path, off = entry[0], entry[2]
            blob = virtual_sd.get(path, b"")[off:off + length]
            entry[2] = off + len(blob)
            if blob:
                uc_inst.mem_write(buf_ptr, blob)
            uc_inst.mem_write(stat_ptr, struct.pack("<II", 0, len(blob)))
            uc_inst.reg_write(UC_ARM_REG_R0, 0)
        elif address == FN_FS_WRITE:
            fd = uc_inst.reg_read(UC_ARM_REG_R0)
            buf_ptr = uc_inst.reg_read(UC_ARM_REG_R1)
            length = uc_inst.reg_read(UC_ARM_REG_R3)
            sp = uc_inst.reg_read(UC_ARM_REG_SP)
            stat_ptr = struct.unpack("<I", uc_inst.mem_read(sp + 4, 4))[0]
            path = open_fds[fd][0]
            blob = bytes(uc_inst.mem_read(buf_ptr, length))
            virtual_sd[path] = blob
            uc_inst.mem_write(stat_ptr, struct.pack("<II", 0, len(blob)))
            uc_inst.reg_write(UC_ARM_REG_R0, 0)
        elif address == FN_FS_CLOSE:
            fd = uc_inst.reg_read(UC_ARM_REG_R0)
            open_fds.pop(fd, None)
            uc_inst.reg_write(UC_ARM_REG_R0, 0)

    uc.hook_add(UC_HOOK_CODE, hook_code)

    key_dispatch_site = int(cc_info["key_dispatch_hook"], 16)
    swap_ptr = int(cc_info["egl_swap_inline_hook"], 16) | 1

    def send_key(key: int, action: int):
        ev_addr = 0x0A003000
        uc.mem_write(ev_addr, struct.pack("<II", key, action))
        uc.reg_write(UC_ARM_REG_SP, 0x0A002FF0)
        uc.reg_write(UC_ARM_REG_R0, ev_addr)
        uc.reg_write(UC_ARM_REG_LR, RET_TRAP | 1)
        uc.emu_start(int(cc_info["cc_key_hook_entry"], 16) | 1, RET_TRAP, count=4000000)

    def call_swap():
        uc.reg_write(UC_ARM_REG_SP, 0x0A002FF0)
        uc.reg_write(UC_ARM_REG_R0, 1)
        uc.reg_write(UC_ARM_REG_R1, 2)
        uc.reg_write(UC_ARM_REG_LR, RET_TRAP | 1)
        uc.emu_start(swap_ptr, RET_TRAP, count=2000000)

    def open_via_setup_menu():
        """Simulate user clicking Setup -> 控制中心 (ID 0x01B5 @ 0x068596AE) inside 0x0685935C."""
        sp = 0x0A002FD8
        uc.mem_write(sp, struct.pack("<6I", 0x1111, 0x2222, 0x3333, 0x4444, 0x5555, RET_TRAP | 1))
        uc.reg_write(UC_ARM_REG_SP, sp)
        uc.emu_start(int(cc_info["setup_menu_hook"], 16) | 1, RET_TRAP, count=4000000)
        assert uc.reg_read(UC_ARM_REG_R4) == 0x1111 and uc.reg_read(UC_ARM_REG_R8) == 0x5555

    # 1. Closed state: normal UP passes through, eglSwapBuffers draws 0 rects
    send_key(3, 1)
    assert orig_key_calls == [(3, 1)], f"Expected passthrough, got {orig_key_calls}"
    call_swap()
    assert len(clear_ops) == 0 and len(swap_calls) == 1

    # 2. Open Control Center from Setup Menu -> 控制中心 (0x01B5 @ 0x068596AE -> 0x068598D0 -> commit_ui!)
    orig_key_calls.clear()
    commit_ui_calls[0] = 0
    open_via_setup_menu()
    assert uc.mem_read(0x07108000 + 4, 1)[0] == 1, "Setup menu hook did not open Control Center!"
    assert commit_ui_calls[0] >= 1, "Setup menu hook did not trigger commit_ui redraw!"
    assert uc.mem_read(0x07108000 + 13, 1)[0] == 2, "Expected 2 SD LUTs from A:\\LUT\\LIST.TXT!"

    # 3. Cursor on Row 0: unpopulated 机内 1..4 (0x0000) are automatically skipped!
    #    Pressing RIGHT once jumps directly 0 (纯V-Log) -> SD01 Kodak2383.cube & syncs Live View ISP + 0x08CC576B!
    lv_apply_calls.clear()
    send_key(6, 1)
    assert uc.mem_read(0x07108000 + 12, 1)[0] == 1, "Expected sd_lut_active == 1 (Kodak2383.cube) on single RIGHT!"
    assert struct.unpack("<I", uc.mem_read(0x08A9D47C + 4, 4))[0] == 0x0710A200, "Live View Slot 1 ptr not hot-swapped!"
    assert lv_apply_calls[-1] == (1, 1, 1), f"Live View ISP not synced on SD01: {lv_apply_calls}"
    assert uc.mem_read(0x08CC576B, 2) == b"\x01\x01", "LCD/HDMI View Assist enable bytes not set!"
    assert uc.mem_read(0x08AA0CB6, 1)[0] == 1 and uc.mem_read(0x08A9D471, 2) == b"\x01\x01"
    sd_cube_beef = bytes(uc.mem_read(0x0710A200, 2920))

    def assert_beef_close(got: bytes, exp: bytes, label: str):
        assert len(got) == 2920 and got[:4] == struct.pack("<HH", 0xBEEF, 1), f"{label} bad header"
        max_diff = 0
        for s in range(729):
            gw0, gw1 = struct.unpack_from("<HH", got, 4 + s * 4)
            ew0, ew1 = struct.unpack_from("<HH", exp, 4 + s * 4)
            gr, gb, gg = gw0 & 0x3F, (gw0 >> 6) & 0x3F, ((gw0 >> 12) & 0xF) | ((gw1 & 3) << 4)
            er, eb, eg = ew0 & 0x3F, (ew0 >> 6) & 0x3F, ((ew0 >> 12) & 0xF) | ((ew1 & 3) << 4)
            max_diff = max(max_diff, abs(gr - er), abs(gg - eg), abs(gb - eb))
        assert max_diff <= 1, f"{label} 9^3 octant max_diff={max_diff} (>1)"

    exp_beef = build_synthetic_beef_lut9_invert()
    assert_beef_close(sd_cube_beef, exp_beef, ".CUBE (BOM + no-EOF-newline)")

    # Press RIGHT once more -> loads SD02 Fuji_NC.vlt & forces ISP re-upload even though slot stays 1!
    uc.mem_write(0x08AA0CB6, b"\x00")
    uc.mem_write(0x08A9D471, b"\x00\x00")
    send_key(6, 1)
    assert uc.mem_read(0x07108000 + 12, 1)[0] == 2, "Expected sd_lut_active == 2 (Fuji_NC.vlt)!"
    assert uc.mem_read(0x08AA0CB6, 1)[0] == 1 and uc.mem_read(0x08A9D471, 2) == b"\x01\x01", "Dirty flags not re-asserted on SD01->SD02!"
    sd_vlt_beef = bytes(uc.mem_read(0x0710A200, 2920))
    assert_beef_close(sd_vlt_beef, exp_beef, ".VLT")

    # 4. Move DOWN to Row 1 (LUT 快捷键: default=1 Fn:实时选LUT), then Row 2 (LUT 生效格调) -> ALL STYLES
    send_key(4, 1)  # Row 1 (LUT 快捷键)
    assert uc.mem_read(0x07108000 + 48, 1)[0] == 1
    send_key(4, 1)  # Row 2 (LUT 生效格调)
    send_key(58, 1)
    assert uc.mem_read(0x07108000 + 6, 1)[0] == 1

    # 5. Move DOWN to Row 3 (对焦提示音静音) & press SET(58) -> ON (0x06E35134 == 0)
    send_key(4, 1)
    send_key(58, 1)
    assert struct.unpack("<I", uc.mem_read(0x06E35134, 4))[0] == 0

    # 6. Navigate to Row 6 (保存配置并退出) -> press SET -> writes A:\S5_CC.CFG (v3, 80B) and closes!
    send_key(4, 1)  # Row 4 (主题配色)
    send_key(4, 1)  # Row 5 (恢复默认)
    send_key(4, 1)  # Row 6 (保存配置并退出)
    send_key(58, 1)
    assert "A:\\S5_CC.CFG" in virtual_sd and len(virtual_sd["A:\\S5_CC.CFG"]) == 80
    assert uc.mem_read(0x07108000 + 4, 1)[0] == 0

    # 6b. Test Fn -> LUT Select hook (@ 0x0686A074):
    #     - Default Mode 1 (Fn: 实时选LUT): opens S5II Live LUT Bar (visible == 2)
    #     - Anti-penetration: unrelated key (key=25) is swallowed while bar is open
    #     - LEFT switches SD02 -> SD01 (Kodak2383.cube) live!
    #     - Pressing Fn again toggles closed (visible == 0)!
    uc.mem_write(0x0683291C, struct.pack("<HH", 0x4770, 0x4770))

    def press_fn_lut_select():
        sp = 0x0A002F80
        uc.mem_write(sp + 0x1C, struct.pack("<9I", 1, 2, 3, 4, 5, 6, 7, 8, RET_TRAP | 1))
        uc.reg_write(UC_ARM_REG_SP, sp)
        uc.emu_start(0x0686A074 | 1, RET_TRAP, count=4000000)

    def write_png(out_path: Path, ops: list, bg_rgb=(24, 28, 34)):
        import zlib
        W, H = 960, 640
        buf = bytearray(bg_rgb) * (W * H)
        for x, y, w, h, (r, g, b, a) in ops:
            y0, y1 = max(0, H - y - h), min(H, H - y)
            x0, x1 = max(0, x), min(W, x + w)
            if x0 >= x1 or y0 >= y1:
                continue
            rgb = bytes((
                int(max(0, min(255, round(r * 255)))),
                int(max(0, min(255, round(g * 255)))),
                int(max(0, min(255, round(b * 255)))),
            ))
            row_fill = rgb * (x1 - x0)
            for py in range(y0, y1):
                off = (py * W + x0) * 3
                buf[off:off + len(row_fill)] = row_fill

        def png_chunk(tag: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

        raw_rows = b"".join(b"\x00" + bytes(buf[y * W * 3:(y + 1) * W * 3]) for y in range(H))
        ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
        out_path.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(raw_rows, 6))
            + png_chunk(b"IEND", b"")
        )

    press_fn_lut_select()  # Mode 1: opens S5II Live LUT Bar (visible == 2)!
    assert uc.mem_read(0x07108000 + 4, 1)[0] == 2, "Expected S5II Live LUT Bar (visible == 2)!"

    # Anti-penetration test: send unrelated key 25 while bar is open -> MUST NOT pass through!
    orig_key_calls.clear()
    send_key(25, 1)
    assert orig_key_calls == [], f"Key penetration detected while overlay open: {orig_key_calls}"

    # Render S5II-style bottom Live LUT Bar PNG preview (over simulated dark-teal viewfinder bg)!
    clear_ops.clear()
    call_swap()
    assert len(clear_ops) > 100
    write_png(DIR / "lut_bar_preview.png", clear_ops, bg_rgb=(42, 54, 62))

    # Live switch LEFT in LUT Bar: SD02 -> SD01 (Kodak2383.cube)
    send_key(5, 1)
    assert uc.mem_read(0x07108000 + 12, 1)[0] == 1, "Live LUT Bar LEFT failed to switch to SD01!"

    # Anti-reentry toggle: pressing Fn again closes the LUT bar!
    press_fn_lut_select()
    assert uc.mem_read(0x07108000 + 4, 1)[0] == 0, "Fn re-entry toggle did not close LUT bar!"

    # 7. Simulate COLD REBOOT (wipe RAM state, LUT buffer, slot ptr, VA enable, beep counter)
    #    Verify FIRST NORMAL KEY PRESS (e.g. half-shutter / UP) auto-restores S5_CC.CFG BEFORE opening any menu!
    uc.mem_write(0x07108000, b"\x00" * 0x3400)
    uc.mem_write(0x08A9D47C + 4, struct.pack("<I", 0x062B6C00))
    uc.mem_write(0x08CC576A, b"\x00\x00\x00")
    uc.mem_write(0x08BDC330, struct.pack("<I", 0))
    uc.mem_write(0x06E35134, struct.pack("<I", orig_beep_cnt))

    orig_key_calls.clear()
    send_key(3, 1)  # Normal UP press right after boot (no menu opened!)
    assert orig_key_calls == [(3, 1)], "Boot auto-restore swallowed normal key!"
    assert uc.mem_read(0x07108000 + 12, 1)[0] == 2, "Boot auto-restore failed to restore SD02 (Fuji_NC.vlt)!"
    assert bytes(uc.mem_read(0x0710A200, 2920)) == sd_vlt_beef, "SD LUT buffer not re-parsed on boot auto-restore!"
    assert uc.mem_read(0x08CC576B, 2) == b"\x01\x01", "Boot auto-restore did not enable Live View VA!"
    assert uc.mem_read(0x07108000 + 6, 1)[0] == 1, "SD load failed to restore lut_all_styles!"
    assert struct.unpack("<I", uc.mem_read(0x06E35134, 4))[0] == 0, "SD load failed to re-apply Focus Beep Mute!"
    assert uc.mem_read(0x07108000 + 10, 1)[0] == 1, "Expected sd_status == SD_STAT_LOADED (1)!"

    # Also test Live View Photo Style Gate @ 0x066AE7C4 (BL cc_lv_style_gate)
    def call_lv_style_gate(style: int) -> int:
        uc.reg_write(UC_ARM_REG_SP, 0x0A002FF0)
        uc.reg_write(UC_ARM_REG_R0, style)
        uc.reg_write(UC_ARM_REG_LR, RET_TRAP | 1)
        # Execute the patched BL at 0x066AE7C4 (4 bytes) -> returns to 0x066AE7C8 where we stop
        uc.emu_start(0x066AE7C4 | 1, 0x066AE7C8, count=10000)
        return uc.reg_read(UC_ARM_REG_R0)

    assert call_lv_style_gate(0x00) == 1, "lut_all_styles=1 should allow Standard 0x00 in Live View!"
    uc.mem_write(0x07108000 + 6, b"\x00")  # set lut_all_styles = 0
    assert call_lv_style_gate(0x0B) == 1, "V-Log 0x0B must be allowed in Live View!"
    assert call_lv_style_gate(0x1E) == 1, "My Photo Style 1 (0x1E) must be allowed in Live View!"
    assert call_lv_style_gate(0x00) == 0, "Standard 0x00 must be blocked when lut_all_styles=0!"
    uc.mem_write(0x07108000 + 6, b"\x01")  # restore lut_all_styles = 1

    open_via_setup_menu()

    # Verify hook.c bakes directly from the SD-parsed 0x0710A200 0xBEEF buffer in V-Log (0x0B)!
    _, _, y_sd_bake, _ = run_lut_case(6000, lut_id=0, max_insns=12000000, photo_style=0x0B, sd_lut_buf=sd_vlt_beef)
    assert 150 <= y_sd_bake[0] <= 160 and len(set(y_sd_bake[:16])) == 1, "hook.c failed to bake SD LUT!"

    # Verify 20_osdover.bin in-place renamed labels
    osd = (DIR / "20_osdover.bin").read_bytes()
    assert osd[0x876436:0x876436 + 8] == "控制中心".encode("utf-16le")
    assert osd[0x551654:0x551654 + 26] == "ControlCenter".encode("utf-16le")

    # Move cursor to Row 3 (对焦提示音静音) and render open Control Center with [SD卡已加载] badge!
    send_key(4, 1)
    send_key(4, 1)
    send_key(4, 1)
    clear_ops.clear()
    scissor_log.clear()
    call_swap()
    assert scissor_log == [(0x1102, 0), (0x1102, 1)], f"Scissor save/restore mismatch: {scissor_log}"
    assert len(clear_ops) > 200, f"Expected OpenVG draw ops, got {len(clear_ops)}"

    # Render PNG preview from captured vgClear ops (pure-Python stdlib zlib PNG, zero deps)
    import zlib
    W, H = 960, 640
    buf = bytearray([24, 28, 34]) * (W * H)
    for x, y, w, h, (r, g, b, a) in clear_ops:
        y0 = max(0, H - y - h)
        y1 = min(H, H - y)
        x0 = max(0, x)
        x1 = min(W, x + w)
        if x0 >= x1 or y0 >= y1:
            continue
        rgb = bytes((
            int(max(0, min(255, round(r * 255)))),
            int(max(0, min(255, round(g * 255)))),
            int(max(0, min(255, round(b * 255)))),
        ))
        row_fill = rgb * (x1 - x0)
        for py in range(y0, y1):
            off = (py * W + x0) * 3
            buf[off:off + len(row_fill)] = row_fill

    def png_chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw_rows = b"".join(b"\x00" + bytes(buf[y * W * 3:(y + 1) * W * 3]) for y in range(H))
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(raw_rows, 6))
        + png_chunk(b"IEND", b"")
    )
    PREVIEW_PNG.write_bytes(png_bytes)
    png_note = f"rendered {PREVIEW_PNG.name} ({len(clear_ops)} vgClear spans)"

    # 7. Unmute Focus Beep -> restores orig_beep_cnt, then Q closes menu
    send_key(58, 1)
    assert struct.unpack("<I", uc.mem_read(0x06E35134, 4))[0] == orig_beep_cnt
    send_key(15, 1)  # Q closes menu
    clear_ops.clear()
    call_swap()
    assert len(clear_ops) == 0, "Overlay still drawing after close!"
    orig_key_calls.clear()
    send_key(3, 1)
    assert orig_key_calls == [(3, 1)], "Keys not passed through after close!"
    print(f"PASS Case 8 (S5 Control Center key nav + LUT/Beep live control + OpenVG): {png_note}")


def main():
    global stride

    # Case 1: Standard (0x00) & HLG (0x0C) -> bypassed cleanly when lut_all_styles=0 (0 overhead)
    ok, r0, y_all, c0 = run_lut_case(6000, lut_id=1, with_slot1_buf=True, photo_style=0x00)
    assert ok and r0 == 0 and y_all[:16] == bytes([100]) * 16 and c0 == bytes([0]) * 16
    ok_hlg, _, y_hlg, _ = run_lut_case(6000, lut_id=1, with_slot1_buf=True, photo_style=0x0C)
    assert ok_hlg and y_hlg[:16] == bytes([100]) * 16, "HLG 0x0C must NOT be mistaken for V-Log!"
    print("PASS Case 1 (Standard 0x00 & HLG 0x0C bypassed cleanly, scope=V-Log 0x0B + My 0x1E..0x27)")

    # Case 2: Thumbnail 640x480 in V-Log (0x0B) -> skipped cleanly
    ok, r0, y_all, c0 = run_lut_case(640, lut_id=1, with_slot1_buf=True, height=480, photo_style=0x0B)
    assert ok and r0 == 0 and y_all[:16] == bytes([100]) * 16 and c0 == bytes([0]) * 16
    print("PASS Case 2 (thumbnail 640x480): skipped cleanly")

    # Case 3: V-Log (0x0B) + Slot 0 (OFF / Pure V-Log) -> bypass (pixels untouched!)
    ok, r0, y_slot0, c_slot0 = run_lut_case(6000, lut_id=0, view_assist_monitor=0, photo_style=0x0B)
    assert ok and r0 == 0 and y_slot0[:16] == bytes([100]) * 16 and c_slot0 == bytes([0]) * 16
    print("PASS Case 3 (V-Log 0x0B + slot 0 = Pure V-Log bypass): returned cleanly, pixels untouched")

    # Case 4: V-Log (0x0B) + slot 1 via 0x08CC576A (SD imported 9^3 0xBEEF) + speed benchmark
    _, _, y_bench, _ = run_lut_case(6000, lut_id=1, view_assist_monitor=0, with_slot1_buf=True, max_insns=12000000, fill_rows=45, photo_style=0x0B)
    y_slot1 = y_bench[:16]
    assert 150 <= y_slot1[0] <= 160 and len(set(y_slot1)) == 1
    done_px = 0
    for r in range(45):
        row_bytes = y_bench[r * stride:r * stride + 6000]
        if row_bytes[0] == 100:
            break
        done_px += sum(1 for b in row_bytes if b != 100)
    insns_per_px = max(1, 12000000 - 4500000) / max(1, done_px)
    print(f"PASS Case 4 (V-Log 0x0B + slot 1 via 0x08CC576A, ViewAssist OFF): Y={list(y_slot1[:4])}, {done_px:,} px (~{insns_per_px:.1f} insns/px)")

    # Case 5: Real My Photo Style 1..10 (0x1E..0x27) + slot 1 (65:24 & 1:1)
    _, _, y_65, _ = run_lut_case(6000, lut_id=1, with_slot1_buf=True, max_insns=12000000, height=2208, photo_style=0x1E)
    assert y_65[:16] == y_slot1
    stride = 4032
    _, _, y_11, _ = run_lut_case(4000, lut_id=1, with_slot1_buf=True, max_insns=12000000, height=4000, photo_style=0x27)
    assert y_11[:16] == y_slot1
    stride = 6016
    print(f"PASS Case 5 (MyPhotoStyle 1..10 [0x1E..0x27] + slot 1, 65:24 & 1:1): Y={list(y_65[:4])}")

    # Case 6: Playback double-LUT fix @ 0x866AE770 == 0xE013 & native Fn LUT Select @ 0x8686A07A
    pb_insn = struct.unpack_from("<H", patched_prog, 0x866AE770 - 0x86280000)[0]
    assert pb_insn == 0xE013
    fn_hw1, fn_hw2 = struct.unpack_from("<HH", patched_prog, 0x8686A07A - 0x86280000)
    assert (fn_hw1, fn_hw2) == (0xF247, 0x00B0)
    print("PASS Case 6 (playback double-LUT fix @ 0x866AE770 + native Fn LUT Select 0x70B0 intact)")

    # Case 7: Control Center lut_all_styles=1 enables LUT baking even in Standard Photo Style (0x00)
    _, _, y_all_s, _ = run_lut_case(6000, lut_id=1, with_slot1_buf=True, max_insns=12000000, photo_style=0x00, cc_all_styles=1)
    assert y_all_s[:16] == y_slot1
    print(f"PASS Case 7 (Control Center ALL STYLES flag + Standard 0x00): Y={list(y_all_s[:4])}")

    # Case 8: Full Control Center key navigation, live LUT/Beep mutation, and OpenVG PNG render
    verify_control_center_and_render_png()


if __name__ == "__main__":
    main()
