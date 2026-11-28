#!/usr/bin/env python3
"""RC2 QEMU regressions: execute built ARM payloads, never host reimplementations.

Real receiver 0x06475886 -> patched physical callsite -> loader -> original raw
translator/UI hook. The earlier native raw-handler environment is bypassed at
0x06642D22, with arguments recorded, by forwarding to 0x0663EB5A. Filesystem
operations and hardware queries are explicit boundaries; drive readiness runs
the actual native validator with synthetic handles. This is not a device test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from check_rc1_qemu_functions import Harness, ROOT, TABLE, STATE, RAW, BUFFER, OPEN, run_jpeg

RECEIVER, RAW_DISPATCH, CHECK_DRIVE = 0x06475886, 0x06642D22, 0x0668794A
DRIVES = [0x8C2C41C0 + i * 0x120F10 + 0x98ED8 for i in range(3)]


class PhysicalHarness(Harness):
    def __enter__(self):
        super().__enter__()
        self.forwarded = []
        self.e.breakpoint_add(RAW_DISPATCH)
        return self

    def ready(self, index, ready=True):
        self.e.write_mem(DRIVES[index], struct.pack('<4H', index, 0, 1, 2 if ready else 0))

    def handle(self, pc):
        if pc == RAW_DISPATCH:
            source, ptr = self.e.read_reg(0), self.e.read_reg(1)
            self.forwarded.append({'source': source, 'ptr': hex(ptr),
                                   'payload': self.e.read_mem(ptr, 0x58).hex(),
                                   'loaded': self.e.read_mem(TABLE + 8, 1)[0],
                                   'theme_before_native_key': self.byte(8)})
            self.e.write_reg(0, ptr)
            self.jump(RAW | 1)
        else:
            super().handle(pc)

    def physical(self, mask=8, bank=0, action=0):
        raw = bytearray(0x58)
        struct.pack_into('<I', raw, bank + (4, 8, 12)[action], mask)
        self.e.write_mem(BUFFER, raw)
        self.e.write_mem(BUFFER + 0x100, struct.pack('<4I', 0, 0, 0x11F001, BUFFER))
        before = len(self.forwarded)
        assert self.call(RECEIVER, BUFFER + 0x100) == 1
        assert len(self.forwarded) == before + 1
        assert self.forwarded[-1]['source'] == 0
        assert self.forwarded[-1]['ptr'] == hex(BUFFER)
        assert self.forwarded[-1]['payload'] == raw.hex()
        assert self.e.read_mem(BUFFER, 0x58) == raw

    def opens(self):
        return sum(c['pc'] == hex(OPEN) for c in self.fs_events)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build-dir', type=Path, default=ROOT / 'build_rc2')
    p.add_argument('--baseline-dir', type=Path, default=ROOT / 'build_input_fix')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    report = {'scope': __doc__, 'checks': [], 'hardware_tested': False}

    def check(name):
        report['checks'].append(name)
        print('PASS', name, flush=True)

    def settings(h):
        return [h.byte(i) for i in [6, 7, 8, 48]]

    try:
        # RC1 negative controls use the SAME receiver/harness and fault injection.
        with PhysicalHarness(a.baseline_dir, a.output / 'rc1-negative') as h:
            h.ready(0)
            h.physical()
            assert h.e.read_mem(TABLE + 8, 2) == b'\0\0' and h.fs_calls == 0
            h.open()
            h.select(6)
            h.readonly = True
            h.raw(0x10)
            assert h.byte(4) == 0 and h.byte(10) == 0
            h.readonly = False
            h.open()
            h.select(4)
            h.raw(2)
            assert h.byte(8) != 0
            h.select(5)
            h.raw(1)
            assert h.byte(8) == 0
            h.select(6)
            h.raw(2)
            assert h.byte(4) == 0 and len(h.files['A:\\S5_CC.CFG']) == 80
            report['negative_control_calls'] = h.calls
            check('RC1 reproduces all three bugs under the same QEMU harness (four cases)')

        with PhysicalHarness(a.build_dir, a.output / 'actions') as h:
            h.open()
            h.select(2)
            h.raw(2)
            h.select(3)
            h.raw(2)
            h.select(4)
            h.raw(2)
            expected = settings(h)
            assert expected[:3] == [1, 1, 1]
            for row in [5, 6]:
                h.select(row)
                before = h.e.read_mem(STATE, 50)
                fs_before = h.fs_calls
                for bank in [0, 0x20]:
                    for key in [1, 2]:
                        for action in [0, 1, 1, 2]:
                            h.raw(key, bank=bank, action=action)
                assert h.e.read_mem(STATE, 50) == before
                assert h.fs_calls == fs_before and not h.stock
            check('LEFT/RIGHT and joystick press/repeat/release neither reset nor save')
            for fault in ['create', 'error', 'short']:
                h.readonly = fault == 'create'
                h.write_fault = None if fault == 'create' else fault
                h.raw(0x10)
                assert h.byte(4) == 1 and h.byte(5) == 6 and h.byte(10) == 4
                assert settings(h) == expected and not h.fds
                fs_before = h.fs_calls
                h.ui(7, 1)
                h.ui(7, 2)
                assert h.fs_calls == fs_before and h.byte(4) == 1
                check(f'save {fault} failure stays visible with error, preserves settings, closes FDs; held SET does not retry')
            h.readonly, h.write_fault = False, None
            h.raw(0x10)
            assert h.byte(4) == 0 and h.byte(10) == 2 and not h.fds
            cfg = h.files['A:\\S5_CC.CFG']
            assert len(cfg) == 80
            (a.output / 'saved-S5_CC.CFG').write_bytes(cfg)
            h.ui(7, 1)
            h.ui(7, 2)
            assert not h.stock
            h.raw(8)
            assert h.stock.pop() == [0x0F000002, 6, 0]
            check('SET retries after write recovery, saves 80-byte config and exits; closing tail consumed')
            h.open()
            assert settings(h) == expected
            h.select(5)
            h.raw(0x10)
            assert settings(h)[:3] == [0, 0, 0] and h.byte(4) == 1 and h.byte(10) == 3
            check('SET still resets settings without closing the panel')
            report['action_calls'] = h.calls

        with PhysicalHarness(a.build_dir, a.output / 'delayed-sd') as h:
            h.files['A:\\S5_CC.CFG'] = cfg
            # Execute the actual native validator, including rejection cases.
            assert h.call(CHECK_DRIVE, 0, 1) == 0xA401
            assert h.call(CHECK_DRIVE, DRIVES[0], 9) == 0xA101
            for i in range(3):
                h.ready(i, False)
                assert h.call(CHECK_DRIVE, DRIVES[i], 1) == 0xA502
                h.ready(i)
                assert h.call(CHECK_DRIVE, DRIVES[i], 1) == 0
                h.ready(i, False)
            check('original native readiness validator rejects unmounted drives and accepts all three known handles')
            h.physical()
            assert h.e.read_mem(TABLE + 8, 2) == b'\0\0' and h.fs_calls == 0
            assert h.stock.pop() == [0x0F000002, 6, 0]
            h.ready(0)
            for action in [1, 2]:
                h.physical(action=action)
            h.physical(mask=0)
            h.ui(0x1234, event=0x12345678)
            h.call(h.syms['ldr_egl_swap_overlay'], 1, 2)
            assert h.fs_calls == 0
            check('unready press passes through; idle/repeat/release/UI/graphics never initiate file I/O')
            h.stock.clear()
            h.physical(bank=0x20)
            assert h.e.read_mem(TABLE + 8, 3) == b'\1\1\0'
            assert h.byte(4) == 0 and h.byte(10) == 1 and settings(h) == expected
            assert h.forwarded[-1]['theme_before_native_key'] == expected[2]
            assert h.stock == [[0x0F000002, 6, 0]] and not h.fds
            check('first fresh joystick press after SD readiness loads module/config before forwarding; panel stays hidden')
            fs_before = h.fs_calls
            h.physical()
            assert h.fs_calls == fs_before
            check('loaded fast path performs no further filesystem probing')
            report['autoload_calls'] = h.calls
            report['forwarded_physical_messages'] = h.forwarded

        with PhysicalHarness(a.build_dir, a.output / 'readiness-change') as h:
            h.files.clear()
            h.ready(0)
            h.physical()
            assert h.e.read_mem(TABLE + 8, 3) == b'\0\1\1'
            fs_before = h.fs_calls
            for _ in range(5):
                h.physical()
            assert h.fs_calls == fs_before
            check('missing module is probed once for a stable ready mask; ordinary input remains usable')
            h.files['B:\\S5_MOD.BIN'] = h.mod
            h.files['B:\\S5_CC.CFG'] = cfg
            h.ready(1)
            h.physical()
            assert h.e.read_mem(TABLE + 8, 4) == b'\1\1\0\1' and settings(h) == expected
            check('newly ready second drive retries and loads B-slot module/config')

        with PhysicalHarness(a.build_dir, a.output / 'reinsert') as h:
            h.files.clear()
            h.ready(0)
            h.physical()
            h.ready(0, False)
            fs_before = h.fs_calls
            h.physical()
            assert h.fs_calls == fs_before and h.e.read_mem(TABLE + 9, 1) == b'\0'
            h.ready(0)
            h.files['A:\\S5_MOD.BIN'] = h.mod
            h.physical()
            assert h.e.read_mem(TABLE + 8, 1) == b'\1'
            check('observed removal/reinsert readiness transition permits automatic retry')

        with PhysicalHarness(a.build_dir, a.output / 'bad-crc') as h:
            bad = bytearray(h.mod)
            bad[64] ^= 1
            h.files['A:\\S5_MOD.BIN'] = bytes(bad)
            h.ready(0)
            h.physical()
            assert h.e.read_mem(TABLE + 8, 3) == b'\0\1\3'
            assert h.stock == [[0x0F000002, 6, 0]] and not h.fds
            fs_before = h.fs_calls
            h.physical()
            assert h.fs_calls == fs_before
            h.files['A:\\S5_MOD.BIN'] = h.mod
            h.open()
            assert h.e.read_mem(TABLE + 8, 3) == b'\1\1\0'
            check('bad CRC falls back to stock without retry storm; explicit Setup retries a replaced module')

        with PhysicalHarness(a.build_dir, a.output / 'busy-loader') as h:
            h.ready(0)
            h.e.write_mem(TABLE + 0x40, struct.pack('<I', 1))
            h.physical()
            assert h.fs_calls == 0 and h.e.read_mem(TABLE + 8, 2) == b'\0\0'
            h.e.write_mem(TABLE + 0x40, bytes(4))
            h.physical()
            assert h.e.read_mem(TABLE + 8, 1) == b'\1'
            check('busy loader lock neither blocks input nor prevents the next press retry')

        with Harness(a.build_dir, a.output / 'jpeg') as h:
            report['jpeg'] = run_jpeg(h, a.output, check)
            assert report['jpeg']['output_y_cb_cr'] == [155, 0, 0]
            assert report['jpeg']['y_sha256'] == '167563e70c82f4a1554221bb113e532c3e8a62b6602bccfe05b15d05ac76b6e5'
            assert report['jpeg']['c_sha256'] == '100cbd429d04e7ed1dd0f342f60ca44507f4dd53402e873083f9e116bbaab11c'
            check('full 24MP output matches RC1 golden hashes exactly')
        report['completed'] = True
    except Exception as exc:
        report.update(completed=False, error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        for label, build in [('candidate', a.build_dir), ('baseline', a.baseline_dir)]:
            report[label + '_hashes'] = {name: hashlib.sha256((build / name).read_bytes()).hexdigest()
                                        for name in ['03_program.bin', 'S5_MOD.BIN']}
        (a.output / 'verification.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(a.output / 'verification.json', flush=True)


if __name__ == '__main__':
    main()
