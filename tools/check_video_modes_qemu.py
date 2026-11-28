#!/usr/bin/env python3
"""Execute native S5 menu validation and recording-profile conversion in QEMU.

This verifies ARM parameter paths, not encoded frames. Camera globals and an
already allocated RCP handle are explicit fixtures. No sensor, video encoder,
SD write bandwidth, or live UI/record-start sequencing is emulated.
"""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import struct

from check_rc1_qemu_functions import Harness, STACK

ROOT = Path(__file__).resolve().parents[1]
# QID: (UI ROM row, recording mode, RCP modes for native variant 0/1,
#       coded dimensions, nominal bps, RCP GOP enum, native name fragment).
# These are assertions against the shipped ARM functions, not replacements for them.
MODES = {
    0x8B: (0x070C5FB0, 0x2AF, (0x1049, 0x104A), (4096, 2160), 200_000_000, 1, 'HEVC_420_IPPP_200M'),
    0x90: (0x070C6078, 0x2B6, (0x2039, 0x203A), (4096, 2160), 400_000_000, 0, 'H264_422_Intra_400M'),
    0x98: (0x070C61E0, 0x2BA, (0x2024, 0x2024), (4096, 2160), 400_000_000, 0, 'H264_422_Intra_400M'),
    0xA9: (0x070C64B0, 0x2C4, (0x2009, 0x2009), (3840, 2160), 400_000_000, 0, 'H264_422_Intra_400M'),
    0xB8: (0x070C66E0, 0x2CE, (0x2011, 0x2011), (3328, 2496), 400_000_000, 0, 'H264_422_Intra_400M'),
    0xD0: (0x070C6A50, 0x2DB, (0x201B, 0x201B), (1920, 1088), 200_000_000, 0, 'H264_422_Intra_200M'),
}


def run(build, output):
    program = (build / '03_program.bin').read_bytes()
    pb = build / '07_postboot3.bin'
    harness = Harness(build, output / 'qemu')
    # Original postboot code uses low link addresses. Supply the same bytes at
    # that alias for these function tests; this is not a boot/MMU validation.
    harness.emu.loads.append((pb, 0x07707000, pb.stat().st_size))
    rows, checks = [], 0
    with harness as h:
        table = b''.join(program[a-0x06280000:a-0x06280000+40]
                         for a, *_ in MODES.values())
        for index, qid in enumerate(MODES):
            assert struct.unpack_from('<I', table, index*40)[0] == qid
        h.e.write_mem(0x08AA45E0, table + struct.pack('<10I', 0x8100, *([0]*9)))
        h.e.write_mem(0x08CC4B76, b'\x06')  # MOV
        h.e.write_mem(0x08CC59E6, b'\0')     # normal recording
        h.e.write_mem(0x08CC59EC, b'\0')     # NTSC menu whitelist
        for qid, (rom, mode, encs, size, bps, gop, name_fragment) in MODES.items():
            for alias in (qid, qid | 0x8000):
                assert h.call(0x064629B2, alias) == 1, f'QID {alias:#x} rejected'
                checks += 1
                # Exercise both values of every extra converter input. These
                # ALL-I branches return the same recording mode independently.
                for flags in itertools.product((0, 1), repeat=4):
                    h.e.write_mem(STACK, struct.pack('<I', flags[3]))
                    actual_mode = h.call(0x06825296, alias, *flags[:3])
                    assert actual_mode == mode, (hex(alias), flags, hex(actual_mode))
                    checks += 1
            for variant, enc in enumerate(encs):
                actual_enc = h.call(0x0656DDF0, mode, variant)
                assert actual_enc == enc
                profile_ptr = h.call(0x065AF25C, enc)
                words = list(struct.unpack('<29I', h.e.read_mem(profile_ptr, 116)))
                assert words[8:10] == list(size)
                assert words[6] == bps and words[14] == gop
                name_ptr = h.call(0x0778CFAC, enc)
                name = h.e.read_mem(name_ptr, 160).split(b'\0', 1)[0].decode('ascii')
                assert name_fragment in name, name
                stc_gop = h.call(0x077C21B0, words[14])
                assert stc_gop == (5 if gop == 0 else 4)
                h.e.write_mem(0x08AEF97C, struct.pack('<II', 1, enc))
                h.e.write_mem(0x08AEF97C + 0x1278, struct.pack('<I', 100))
                active_ptr = h.call(0x06547BA4, 1)
                active = list(struct.unpack('<29I', h.e.read_mem(active_ptr, 116)))
                assert active == words, 'Profile unexpectedly changed at 100% rate fixture'
                checks += 1
                rows.append(dict(qid=hex(qid), ui_rom=hex(rom), recording_mode=hex(mode),
                                 variant=variant, encoder_mode=hex(enc), profile=hex(profile_ptr),
                                 native_name=name, stc_gop_enum=stc_gop, words=words,
                                 active_profile_words=active))
                print(f'{qid:04X} -> {mode:04X} -> {enc:04X}: {name}', flush=True)
        # The shared harness also supports UI/FS mocks for other tests. None
        # may be hit here: every tested conversion must execute native code.
        assert all(call['boundary_hits'] == 0 for call in h.calls), h.calls
    report = dict(status='PASS', checks=checks, native_calls=len(h.calls),
                  mocked_boundary_calls=0,
                  program_sha256=hashlib.sha256(program).hexdigest(),
                  postboot3_sha256=hashlib.sha256(pb.read_bytes()).hexdigest(),
                  limitations=__doc__, cases=rows)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'PASS: {checks} cases, {len(h.calls)} native ARM function calls')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build-dir', type=Path, default=ROOT / 'build_rc2')
    p.add_argument('--output-dir', type=Path, required=True)
    args = p.parse_args()
    run(args.build_dir.resolve(), args.output_dir.resolve())
