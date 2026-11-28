#!/usr/bin/env python3
"""Bounded ARM regression for BASE_LOADER native input capture (not a device test).

Execute the original raw-state -> GUI translator and resident entry trampoline.
Mock filesystem/graphics, mode queries and the downstream stock UI sink only.
The negative control restores the original UI entry: the same raw input must
reach stock UI and leave the overlay cursor stuck. Every call must RETURN,
preserve callee-saved registers/SP, and stay within an instruction bound.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from unicorn import UC_HOOK_CODE
from unicorn.arm_const import (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
    UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11,
    UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC,
)
from build_loader_and_mod import link_obj_split_bss, LOADER_CAVE_LOW
from verify_loader_and_mod import make_harness

ROOT = Path(__file__).resolve().parents[1]
UI_ENTRY = 0x066BEEA6
UI_CONT = UI_ENTRY + 4
RAW_TRANSLATOR = 0x0663EB5A
STATE = 0x07200000
TABLE = 0x070FC000
SCRATCH = 0x0A003000
CALLEE = [UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
          UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11]


class Harness:
    def __init__(self, prog, pb3, files, loader_syms, missing_hook=False):
        self.u, self.logs, self.ret = make_harness(prog, pb3, files)
        self.syms = loader_syms
        self.u.mem_map(0x08CC0000, 0x1000)
        self.u.mem_map(0x08CA9000, 0x1000)
        if missing_hook:
            self.u.mem_write(UI_ENTRY, bytes.fromhex('2de9f041'))
        # Keep the original prologue (or the trampoline replay) executable.
        # The stock event sink returns using the real function's stack shape.
        self.u.mem_write(UI_CONT, bytes.fromhex('bde8f081'))
        # Native translator mode/environment queries: ordinary nonmodal menu.
        self.query_stubs = [0x06844CCA, 0x06844F22, 0x066A7100, 0x066A3714,
                            0x0663BDDC, 0x066AB3CA, 0x066C1958, 0x066ADCFC,
                            0x066ADCBC]
        for addr in self.query_stubs:
            self.u.mem_write(addr, bytes.fromhex('00207047'))
        self.stock_events = []
        self.ui_inputs = []
        self.path = set()
        self.u.hook_add(UC_HOOK_CODE, self.observe)

    def observe(self, u, addr, size, _):
        if addr in (RAW_TRANSLATOR, 0x066BDDC8, UI_ENTRY, UI_CONT):
            self.path.add(addr)
        if addr == UI_ENTRY:
            self.ui_inputs.append(tuple(u.reg_read(reg) for reg in
                                        [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2]))
        if addr == UI_CONT:
            self.stock_events.append(tuple(u.reg_read(reg) for reg in
                                          [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2]))

    def call(self, addr, *args):
        u = self.u
        sp = 0x0A002FF0
        expected = [0x12340000 + i * 0x101 for i in range(len(CALLEE))]
        for reg, val in zip(CALLEE, expected):
            u.reg_write(reg, val)
        for reg, val in zip([UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3], args):
            u.reg_write(reg, val)
        u.reg_write(UC_ARM_REG_SP, sp)
        u.reg_write(UC_ARM_REG_LR, self.ret | 1)
        u.emu_start(addr | 1, self.ret, count=6_000_000)
        assert u.reg_read(UC_ARM_REG_PC) == self.ret, f'did not return: {u.reg_read(UC_ARM_REG_PC):08x}'
        assert u.reg_read(UC_ARM_REG_SP) == sp, 'stack imbalance'
        assert [u.reg_read(r) for r in CALLEE] == expected, 'callee-saved register corruption'
        return u.reg_read(UC_ARM_REG_R0)

    def open_panel(self):
        assert self.call(self.syms['ldr_setup_menu_handler']) == 1
        assert self.byte(4) == 1

    def byte(self, offset):
        return self.u.mem_read(STATE + offset, 1)[0]

    def ui(self, key, action=0, event=0x0F000002, extra=0):
        self.call(UI_ENTRY, event, (action << 16) | key, extra)

    def raw(self, mask, bank=0, action=0):
        raw = bytearray(0x58)
        struct.pack_into('<I', raw, bank + (4, 8, 12)[action], mask)
        self.u.mem_write(SCRATCH, bytes(raw))
        before = len(self.ui_inputs)
        self.call(RAW_TRANSLATOR, SCRATCH)
        return self.ui_inputs[before:]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--build-dir', type=Path, default=ROOT / 'build_input_fix')
    ap.add_argument('--mod-bin', type=Path)
    args = ap.parse_args()
    prog = (args.build_dir / '03_program.bin').read_bytes()
    pb3 = (args.build_dir / '07_postboot3.bin').read_bytes()
    mod = (args.mod_bin or args.build_dir / 'S5_MOD.BIN').read_bytes()
    _, _, syms = link_obj_split_bss((args.build_dir / 's5_loader.o').read_bytes(), LOADER_CAVE_LOW)
    passed = []

    def check(name):
        passed.append(name)
        print('PASS', name, flush=True)

    def fresh(files=None, **kw):
        return Harness(prog, pb3, {'A:\\S5_MOD.BIN': mod} if files is None else files, syms, **kw)

    # Restore ONLY the missing native hook; same new module and raw producer.
    old = fresh(missing_hook=True)
    old.open_panel()
    assert old.raw(8) == [(0x0F000002, 6, 0)]
    assert old.byte(5) == 0 and old.stock_events == [(0x0F000002, 6, 0)]
    check('negative control: absent native hook reproduces stuck cursor/input leakage')
    del old

    h = fresh()
    h.open_panel()
    assert h.raw(8) == [(0x0F000002, 6, 0)] and h.byte(5) == 1
    assert h.raw(8, action=1) == [(0x0F000002, 0x10006, 0)] and h.byte(5) == 2
    assert h.raw(4) == [(0x0F000002, 5, 0)] and h.byte(5) == 1
    # Joystick bank, including the alternate cardinal masks from the native code.
    assert h.raw(8, bank=0x20) == [(0x0F000002, 6, 0)] and h.byte(5) == 2
    assert h.raw(4, bank=0x20) == [(0x0F000002, 5, 0)] and h.byte(5) == 1
    assert h.raw(0x100, bank=0x20) == [(0x0F000002, 6, 0)] and h.byte(5) == 2
    assert h.raw(0x80, bank=0x20) == [(0x0F000002, 5, 0)] and h.byte(5) == 1
    assert h.path >= {RAW_TRANSLATOR, 0x066BDDC8, UI_ENTRY}
    assert not h.stock_events
    check('native raw-state directions/joystick/repeat move panel only')

    # Same original joystick translator with its native alternate UI mode query.
    h.u.mem_write(0x066C1958, bytes.fromhex('47f2e9007047'))  # movw r0,#0x70E9; bx lr
    h.u.ctl_remove_cache(0x066C1958, 0x066C195E)
    assert h.raw(8, bank=0x20) == [(0x0F000002, 0x61, 0)] and h.byte(5) == 2
    assert h.raw(4, bank=0x20) == [(0x0F000002, 0x60, 0)] and h.byte(5) == 1
    h.u.mem_write(0x066C1958, bytes.fromhex('00207047'))
    h.u.ctl_remove_cache(0x066C1958, 0x066C195E)
    assert not h.stock_events
    check('alternate native joystick GUI codes also navigate the panel')

    h.u.mem_write(STATE + 5, b'\x04')  # Theme is a reversible, observable action.
    h.raw(2)
    assert h.byte(8) == 1
    h.raw(1, bank=0x20)
    assert h.byte(8) == 0
    h.raw(0x10)
    assert h.byte(8) == 1
    h.ui(7, 1)
    h.ui(7, 2)
    assert h.byte(8) == 1 and not h.stock_events
    check('left/right/confirm have correct mapping; held confirm does not toggle again')

    # Generic key, auxiliary input, non-input events, and preserved parameters.
    h.ui(9)
    h.ui(0x1234, event=0x0F000010)
    assert not h.stock_events
    h.ui(0x1234, event=0x12345678, extra=0xABCD)
    assert h.stock_events.pop() == (0x12345678, 0x1234, 0xABCD)
    check('visible panel consumes UI input but passes non-input events and arguments')

    h.raw(0x80)  # Delete/Back: closes without sending deletion to stock.
    assert h.byte(4) == 0 and not h.stock_events
    h.ui(10, 1)
    h.ui(10, 2)
    assert not h.stock_events
    h.raw(8)
    assert h.stock_events.pop() == (0x0F000002, 6, 0)
    h.ui(10)
    assert h.stock_events.pop() == (0x0F000002, 10, 0)
    check('Back closes; trailing repeat/release swallowed; fresh stock key works')

    h.open_panel()
    h.raw(0x20)  # Playback also closes, without switching stock mode.
    assert h.byte(4) == 0 and not h.stock_events
    h.ui(8, 1)
    assert not h.stock_events
    h.ui(8)  # Native playback may omit release: next press must not stay blocked.
    assert h.stock_events.pop() == (0x0F000002, 8, 0)
    h.open_panel()
    assert h.byte(4) == 1
    check('Playback exit and re-opening; missing release cannot stick a key')

    h.u.mem_write(STATE + 5, b'\x06')
    h.raw(0x10)
    assert h.byte(4) == 0 and h.byte(10) == 2 and not h.stock_events
    h.ui(7, 1)
    h.ui(7, 2)
    assert not h.stock_events
    check('save-and-exit confirms once and suppresses closing key tail')

    # Fn LUT bar opens through its real resident handler and receives native keys.
    assert h.call(syms['ldr_fn_lut_handler']) == 1 and h.byte(4) == 2
    h.raw(2)
    h.raw(1, bank=0x20)
    assert h.logs['lv_apply'] and not h.stock_events
    h.raw(0x10)
    assert h.byte(4) == 0 and not h.stock_events
    check('Fn LUT bar native left/right and confirm-to-close')

    # Actual EGL hook: drawing does not depend on the old software-key callback.
    h.open_panel()
    h.call(0x064C3CDA, 1, 2)
    assert h.logs['clear_ops'] > 0
    check('panel still renders through the resident EGL trampoline')
    del h

    bad = bytearray(mod)
    bad[64] ^= 1
    wrong_abi = bytearray(mod)
    struct.pack_into('<I', wrong_abi, 4, 1)
    for name, files, err in [('missing SD module', {}, 1),
                             ('CRC rejection', {'A:\\S5_MOD.BIN': bytes(bad)}, 3),
                             ('ABI-1 module rejection', {'A:\\S5_MOD.BIN': bytes(wrong_abi)}, 2)]:
        h = fresh(files)
        assert h.call(syms['ldr_setup_menu_handler']) == 0
        assert h.u.mem_read(TABLE + 8, 3) == bytes([0, 1, err])
        h.raw(8)
        assert h.stock_events == [(0x0F000002, 6, 0)]
        check(name + ': stock UI input remains available')
        del h
    h = fresh({'A:\\S5_MOD.BIN': bytes(bad), 'B:\\S5_MOD.BIN': mod})
    h.open_panel()
    assert h.u.mem_read(TABLE + 8, 4) == bytes([1, 1, 0, 1])
    h.raw(8)
    assert h.byte(5) == 1 and not h.stock_events
    check('corrupt slot A falls back to valid slot B with native input capture')

    report = {'scope': __doc__, 'checks': passed,
              'program_sha256': hashlib.sha256(prog).hexdigest(),
              'mod_sha256': hashlib.sha256(mod).hexdigest(),
              'mocked_native_queries': [hex(a) for a in h.query_stubs],
              'hardware_tested': False}
    (args.build_dir / 'native_input_test_report.json').write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(f'PASS {len(passed)} groups; bounded ARM execution only, no camera test')


if __name__ == '__main__':
    main()
