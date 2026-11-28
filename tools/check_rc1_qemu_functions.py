#!/usr/bin/env python3
"""Execute released RC1 ARM code in QEMU; explicit FS/query/UI boundary mocks.

This is a bounded function test, independent of Unicorn and of RTOS startup.
No real camera, SD controller, GPU, DMA timing or multicore cache model.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
EMU = ROOT.parents[1] / 's5-emulator'
sys.path.insert(0, str(EMU))
from probe.qemu_arm import QemuArm
from qemu.generate_board_setup import code_from_object
from build_loader_and_mod import link_obj_split_bss, LOADER_CAVE_LOW

STATE, TABLE, RAW, UI = 0x07200000, 0x070FC000, 0x0663EB5A, 0x066BEEA6
STACK, BUFFER, RET = 0x0A002FF0, 0x0A003000, 0x0A003F00
PARSE, OPEN, READ, WRITE, CLOSE, SEEK = 0x06426DA6, 0x067DC0D4, 0x06432F00, 0x067302D4, 0x067DA160, 0x067ADAC8
COMMIT, ISP, GET_DESC = 0x0683291C, 0x066AE6BC, 0x87864ACC
QUERIES = [0x06844CCA, 0x06844F22, 0x066A7100, 0x066A3714, 0x0663BDDC,
           0x066AB3CA, 0x066C1958, 0x066ADCFC, 0x066ADCBC]


class Harness:
    def __init__(self, build, output, mod=None):
        self.build, self.output = build, output
        self.mod = mod or (build / 'S5_MOD.BIN').read_bytes()
        self.files = {'A:\\S5_MOD.BIN': self.mod}
        self.fds, self.fd = {}, 100
        self.readonly = False
        self.write_fault = None  # 'error' or 'short'; both slots affected
        self.fs_events = []
        self.stock, self.isp, self.fs_calls, self.commits = [], [], 0, 0
        self.calls, self.path = [], set()
        _, _, self.syms = link_obj_split_bss((build / 's5_loader.o').read_bytes(), LOADER_CAVE_LOW)
        self.emu = QemuArm(EMU / 'toolchain/build/qemu-system-arm', output, ram='3G',
                          loads=[(build / '03_program.bin', 0x06280000),
                                 (build / '07_postboot3.bin', 0x87707000)])

    def __enter__(self):
        self.e = self.emu.__enter__()
        for addr in [RET, UI + 4, PARSE, OPEN, READ, WRITE, CLOSE, SEEK, COMMIT, ISP, GET_DESC] + QUERIES:
            self.e.breakpoint_add(addr)
        for addr in [4, 12, 16]:
            self.e.breakpoint_add(addr, kind=4)
        # Cortex-A7 MMU-off memory is strongly ordered. The compiled module uses
        # legal unaligned Normal-RAM accesses, as does the original RTOS. Supply
        # explicit identity Normal-RAM mappings, like the existing boot probe.
        asm = self.output / 'normal-ram.S'
        obj = self.output / 'normal-ram.o'
        asm.write_text(''' .syntax unified
.arm
.text
.global _start
_start:
 mov r0, #0
 mcr p15, 0, r0, c2, c0, 2
 mov r0, #0x100000
 mcr p15, 0, r0, c2, c0, 0
 mov r0, #1
 mcr p15, 0, r0, c3, c0, 0
 mov r0, #0
 mcr p15, 0, r0, c8, c7, 0
 dsb sy
 mrc p15, 0, r0, c1, c0, 0
 orr r0, r0, #1
 mcr p15, 0, r0, c1, c0, 0
 isb
 bx lr
''')
        subprocess.run(['clang', '--target=arm-none-eabi', '-march=armv7-a', '-marm',
                        '-c', str(asm), '-o', str(obj)], check=True)
        self.e.write_mem(0x10000, code_from_object(obj.read_bytes()))
        self.e.write_mem(0x100000, struct.pack('<4096I', *[(i << 20) | 0x11C02 for i in range(4096)]))
        self.e.write_reg('cpsr', 0x600001D3)
        self.e.write_reg('lr', RET | 1)
        self.jump(0x10000)
        self.e.resume(timeout=5)
        assert self.e.read_reg('pc') == RET, 'Normal-RAM setup failed'
        self.e.write_mem(0x08CC1864, b'\x0b')
        self.e.write_mem(0x08A9D480, struct.pack('<I', 0x062B6C00))
        return self

    def __exit__(self, *args):
        return self.emu.__exit__(*args)

    def jump(self, target):
        self.e.write_reg('pc', target & ~1)
        cpsr = self.e.read_reg('cpsr')
        self.e.write_reg('cpsr', (cpsr | 0x20) if target & 1 else (cpsr & ~0x20))

    def ret(self, value=None):
        if value is not None:
            self.e.write_reg(0, value & 0xFFFFFFFF)
        self.jump(self.e.read_reg('lr'))

    def str(self, ptr):
        return self.e.read_mem(ptr, 128).split(b'\0', 1)[0].decode('ascii')

    def handle(self, pc):
        e = self.e
        self.path.add(pc)
        if pc in [4, 12, 16]:
            raise AssertionError(f'ARM exception vector {pc:x}: {e.registers()}')
        if pc in QUERIES:
            self.ret(0)
        elif pc == UI + 4:
            self.stock.append([e.read_reg(i) for i in range(3)])
            sp = e.read_reg('sp')
            vals = struct.unpack('<6I', e.read_mem(sp, 24))
            for r, v in zip([4, 5, 6, 7, 8, 14], vals):
                e.write_reg(r, v)
            e.write_reg('sp', sp + 24)
            self.ret()
        elif pc == COMMIT:
            self.commits += 1
            self.ret()
        elif pc == ISP:
            self.isp.append([e.read_reg(i) for i in range(3)])
            self.ret()
        elif pc == GET_DESC:
            self.ret(0)  # Caller supplied a synthetic descriptor; native getter mocked.
        else:
            self.fs_calls += 1
            r0, r1, r2, r3 = [e.read_reg(i) for i in range(4)]
            self.fs_events.append({'pc': hex(pc), 'args': [r0, r1, r2, r3]})
            if pc == PARSE:
                path = self.str(r0)
                e.write_mem(r1, struct.pack('<I', 1 if path.startswith('A:') else 2))
                e.write_mem(r2, struct.pack('<I', r0))
                e.write_mem(r3, b'\0\0')
                self.ret(0)
            elif pc == OPEN:
                path = self.str(r2)
                if r3 == 0x401 and path not in self.files:
                    self.ret(0xA406)
                    return
                if r3 != 0x401 and self.readonly:
                    self.ret(0xA406)
                    return
                if r3 == 0x800 and path in self.files:
                    self.ret(0xA405)
                    return
                if r3 != 0x401:
                    self.files[path] = b''
                self.fd += 1
                self.fds[self.fd] = [path, 0]
                e.write_mem(r1, struct.pack('<I', self.fd))
                self.ret(0)
            elif pc == CLOSE:
                self.fds.pop(r0)
                self.ret(0)
            elif pc == SEEK:
                self.fds[r0][1] = r1
                self.ret(0)
            elif pc in [READ, WRITE]:
                stat = struct.unpack('<I', e.read_mem(e.read_reg('sp') + 4, 4))[0]
                path, offset = self.fds[r0]
                if pc == READ:
                    data = self.files[path][offset:offset + r3]
                    e.write_mem(r1, data)
                else:
                    if self.write_fault == 'error':
                        e.write_mem(stat, struct.pack('<II', 0, 0))
                        self.ret(0xA406)
                        return
                    data = e.read_mem(r1, r3)
                    if self.write_fault == 'short':
                        data = data[:-1]
                    self.files[path] = self.files[path][:offset] + data + self.files[path][offset + len(data):]
                self.fds[r0][1] += len(data)
                e.write_mem(stat, struct.pack('<II', 0, len(data)))
                self.ret(0)
            else:
                raise AssertionError(f'Unhandled boundary: {pc:x}')

    def call(self, entry, *args, timeout=30):
        e = self.e
        saved = [0x12340000 + 0x101 * i for i in range(8)]
        for r, val in zip(range(4, 12), saved):
            e.write_reg(r, val)
        for r, val in enumerate(args):
            e.write_reg(r, val)
        e.write_reg('sp', STACK)
        e.write_reg('lr', RET | 1)
        e.write_reg('cpsr', 0x600001D3)
        self.jump(entry | 1)
        start = time.monotonic()
        hits = 0
        while True:
            left = timeout - (time.monotonic() - start)
            if left <= 0 or hits >= 3000:
                raise TimeoutError('Call exceeded its explicit execution boundary')
            stop = e.resume(timeout=min(left, 30))
            pc = e.read_reg('pc')
            if pc == RET:
                break
            assert stop.startswith(('T05', 'S05')), stop
            self.handle(pc)
            hits += 1
        assert e.read_reg('sp') == STACK, 'SP not restored'
        assert [e.read_reg(i) for i in range(4, 12)] == saved, 'callee-saved registers corrupted'
        self.calls.append({'entry': hex(entry), 'returned': True, 'boundary_hits': hits,
                           'seconds': round(time.monotonic() - start, 4)})
        return e.read_reg(0)

    def byte(self, offset):
        return self.e.read_mem(STATE + offset, 1)[0]

    def raw(self, mask, bank=0, action=0):
        raw = bytearray(0x58)
        struct.pack_into('<I', raw, bank + (4, 8, 12)[action], mask)
        self.e.write_mem(BUFFER, raw)
        self.call(RAW, BUFFER)

    def ui(self, key, action=0, event=0x0F000002):
        self.call(UI, event, (action << 16) | key, 0)

    def open(self):
        assert self.call(self.syms['ldr_setup_menu_handler']) == 1
        assert self.byte(4) == 1

    def select(self, item):
        for _ in range(7):
            if self.byte(5) == item:
                return
            self.raw(8)
        assert self.byte(5) == item


def run_jpeg(h, output, check):
    # A 9^3 inverse RGB LUT, parsed by the actual SD module.
    cube = ['TITLE "Inverse"\n', 'LUT_3D_SIZE 9\n']
    cube += [f'{1-r/8:.4f} {1-g/8:.4f} {1-b/8:.4f}\n'
             for b in range(9) for g in range(9) for r in range(9)]
    h.files['A:\\LUT\\LIST.TXT'] = b'Inverse.cube\n'
    h.files['A:\\LUT\\Inverse.cube'] = ''.join(cube).encode('ascii')
    h.open()
    h.raw(2)
    assert h.byte(12) == 1 and h.isp[-1] == [1, 1, 1]
    check('QEMU parses SD CUBE LUT and calls ISP with expected arguments')
    width, height, stride = 6000, 4000, 6016
    plane_size = stride * height
    y_addr, c_addr, desc_addr = 0x90000000, 0x92000000, 0x8E000000
    desc = bytearray(0x38)
    struct.pack_into('<IIHHIIHHHHI', desc, 0, y_addr, plane_size, stride, height,
                     c_addr, plane_size, stride, height, 0, 0, 0)
    struct.pack_into('<HHI', desc, 0x1C, width, height, 2)
    h.e.write_mem(desc_addr, desc)
    h.e.write_mem(y_addr, bytes([100]) * plane_size)
    h.e.write_mem(c_addr, bytes(plane_size))
    h.call(0x879F7900, 1, desc_addr, timeout=30)
    check('24MP JPEG hook completes and restores SP/r4-r11 in QEMU')
    y_path, c_path = output / 'output-Y.bin', output / 'output-C.bin'
    h.e.dump_memory(y_addr, plane_size, y_path)
    h.e.dump_memory(c_addr, plane_size, c_path)
    y, c = y_path.read_bytes(), c_path.read_bytes()
    # Checks all active pixels and row padding, not only an early tile.
    assert 150 <= y[0] <= 160
    expected_y = (bytes([y[0]]) * width + bytes([100]) * (stride - width)) * height
    expected_c = (c[:2] * (width // 2) + bytes(stride - width)) * height
    assert y == expected_y and c == expected_c
    check('all 24MP pixels processed uniformly; both plane paddings unchanged')
    result = {'width': width, 'height': height, 'stride': stride,
                      'output_y_cb_cr': [y[0], c[0], c[1]],
                      'y_sha256': hashlib.sha256(y).hexdigest(),
                      'c_sha256': hashlib.sha256(c).hexdigest(),
                      'calls': h.calls,
                      'scope': 'constant synthetic NV16 + inverse LUT; no JPEG encoder or image quality proof'}
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build-dir', type=Path, default=ROOT / 'build_input_fix')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    report = {'scope': __doc__, 'checks': [], 'findings': [], 'hardware_tested': False}
    def check(name):
        report['checks'].append(name)
        print('PASS', name, flush=True)
    def finding(name, details):
        report['findings'].append({'name': name, **details})
        print('REPRODUCED', name, flush=True)
    try:
        with Harness(a.build_dir, a.output / 'input') as h:
            fs_before = h.fs_calls
            h.raw(8)
            assert h.stock == [[0x0F000002, 6, 0]]
            assert h.e.read_mem(TABLE + 8, 2) == b'\0\0' and h.fs_calls == fs_before
            finding('physical first key does not auto-load module/config',
                    {'loaded': 0, 'probed': 0, 'sd_api_calls': 0, 'scope': 'Existing loader behavior; Setup/Fn loads later'})
            h.stock.clear()
            h.open()
            assert h.e.read_mem(TABLE + 8, 3) == b'\1\1\0'
            check('real resident SD loader CRC/entry/open returns in QEMU')
            h.raw(8)
            h.raw(8, bank=0x20)
            assert h.byte(5) == 2 and not h.stock
            h.raw(4, action=1)
            assert h.byte(5) == 1 and not h.stock
            h.select(4)
            h.raw(2)
            assert h.byte(8) == 1
            h.raw(0x10)
            assert h.byte(8) == 2
            h.ui(7, 1)
            h.ui(7, 2)
            assert h.byte(8) == 2 and not h.stock
            check('native directions/joystick/repeat/confirm operate only the panel')
            h.raw(0x80)
            assert h.byte(4) == 0 and not h.stock
            h.ui(10, 1)
            h.ui(10, 2)
            assert not h.stock
            h.raw(8)
            assert h.stock.pop() == [0x0F000002, 6, 0]
            check('close tail captured; stock input resumes on fresh press')
            h.open()
            h.select(6)
            h.readonly = True
            h.raw(0x10)
            assert 'A:\\S5_CC.CFG' not in h.files and 'B:\\S5_CC.CFG' not in h.files
            finding('failed save closes panel', {'visible': h.byte(4), 'sd_status': h.byte(10),
                    'expected': 'stay visible and expose save failure', 'write_fault': 'both drives reject create/truncate'})
            assert h.byte(4) == 0 and h.byte(10) == 0
            h.readonly = False
            h.open()
            h.select(6)
            h.raw(1)
            assert 'A:\\S5_CC.CFG' in h.files and h.byte(4) == 0
            finding('LEFT on save row writes config and exits', {'trigger': 'raw LEFT while cursor=6', 'saved': True})
            h.open()
            h.select(4)
            if h.byte(8) == 0:
                h.raw(2)
            theme = h.byte(8)
            assert theme != 0
            h.select(5)
            h.raw(1)
            assert h.byte(8) == 0
            finding('LEFT on reset row resets settings without SET', {'theme_before': theme, 'theme_after': h.byte(8)})
            report['input_calls'] = h.calls
            report['input_queries_mocked'] = [hex(x) for x in QUERIES]
        with Harness(a.build_dir, a.output / 'jpeg') as h:
            report['jpeg'] = run_jpeg(h, a.output, check)
        report['completed'] = True
    except Exception as exc:
        report.update(completed=False, error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        report['program_sha256'] = hashlib.sha256((a.build_dir / '03_program.bin').read_bytes()).hexdigest()
        report['mod_sha256'] = hashlib.sha256((a.build_dir / 'S5_MOD.BIN').read_bytes()).hexdigest()
        (a.output / 'verification.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(a.output / 'verification.json', flush=True)


if __name__ == '__main__':
    main()
