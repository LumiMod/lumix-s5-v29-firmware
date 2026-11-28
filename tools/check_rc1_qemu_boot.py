#!/usr/bin/env python3
"""Compare stock and released RC1 startup in the existing partial SMP machine.

No panel state/module is prefilled; SD loading/input is a separate function test.
The existing model supplies NAND reads, graphics HLE and explicit absent peers.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EMU = ROOT.parents[1] / 's5-emulator'
STOCK = ROOT.parents[1] / 's5-v29-offline/unpacked-v29'


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case', choices=['stock', 'rc1', 'candidate'], required=True)
    p.add_argument('--build-dir', type=Path)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        p.error('Use a fresh output directory; existing evidence is never deleted')
    sys.path[:0] = [str(EMU), str(EMU / 'menu-smp')]
    import boot
    import display.storage as storage
    import display.graphics_hle as ghle
    candidate = (a.build_dir or ROOT / 'build_input_fix').resolve()
    release_manifest = json.loads((candidate / 'loader_manifest.json').read_text())
    patch_files = ['03_program.bin', '07_postboot3.bin', '20_osdover.bin'] if a.case != 'stock' else []
    if patch_files:
        assert sha(candidate / '03_program.bin') == release_manifest['03_program']['program_sha256']
        assert sha(candidate / '07_postboot3.bin') == release_manifest['07_postboot3']['postboot3_sha256']
        assert sha(candidate / '20_osdover.bin') == release_manifest['20_osdover']['sha256']
        assert sha(candidate / 'S5_MOD.BIN') == release_manifest['s5_mod']['sha256']
        if a.case == 'rc1':
            assert sha(candidate / 'S5_MOD.BIN') == '7e49f173e58c3842bddd9b7868353f0512ccde2675ed9a53d9778f7ba6414119'

    if patch_files:
        original_storage_init = storage.StorageMap.__init__

        def storage_init(self):
            original_storage_init(self)
            for row in self.regions:
                name = f"{row['index']:02d}_{row['name']}.bin"
                if name in patch_files:
                    data = (candidate / name).read_bytes()
                    assert len(data) == row['end'] - row['start']
                    row['data'] = data
                    row['sha256'] = hashlib.sha256(data).hexdigest()
        storage.StorageMap.__init__ = storage_init

        # Let the actual loader/module EGL hook execute BEFORE the graphics HLE.
        original_init, original_handle = ghle.GraphicsHLE.__init__, ghle.GraphicsHLE.handle

        def graphics_init(self, *args, **kw):
            original_init(self, *args, **kw)
            self.breakpoints[0x064C3CDE] = self.breakpoints.pop(0x064C3CDA)

        def graphics_handle(self, pc, regs):
            if pc == 0x064C3CDE:
                sp = regs['r13']
                for reg, val in zip(['r4', 'r5', 'r6', 'r7', 'r8', 'r14'],
                                    struct.unpack('<6I', self.emu.read_mem(sp, 24))):
                    self.emu.write_reg(reg, val)
                    regs[reg] = val
                self.emu.write_reg('r13', sp + 24)
                regs['r13'] = sp + 24
            return original_handle(self, pc, regs)
        ghle.GraphicsHLE.__init__, ghle.GraphicsHLE.handle = graphics_init, graphics_handle

    with tempfile.TemporaryDirectory(prefix='s5-rc1-boot-') as td:
        stage = Path(td) / 's5-v29-offline/unpacked-v29'
        stage.mkdir(parents=True)
        for src in STOCK.glob('*.bin'):
            (stage / src.name).symlink_to(candidate / src.name if src.name in patch_files else src)
        proxy = Path(td) / 's5-emulator'
        proxy.mkdir()
        for sub in ['toolchain', 'display', 'probe', 'qemu', 'menu-smp', 'firmware', 'prepared']:
            if (EMU / sub).exists():
                (proxy / sub).symlink_to(EMU / sub)
        original_verify = boot.verify_firmware

        def verify():
            manifest = original_verify()
            for i, item in enumerate(manifest['components']):
                name = f"{i:02d}_{item['name']}.bin"
                if name in patch_files:
                    item['sha256'] = sha(candidate / name)
            manifest['test_case'] = a.case
            manifest['modified_components'] = patch_files
            return manifest
        boot.ROOT, boot.verify_firmware = proxy, verify
        sys.argv = ['boot.py', '--output', str(a.output.resolve()), '--storage-hle',
                    '--capture-gfx', '--graphics-hle', '--spi0-open-bus', '--spi6-open-bus',
                    '--adc11-sample', '0x300', '--isolate-display', '--isolate-display-ticks',
                    '600000', '--stop-after-swaps', '1', '--seconds', '45']
        boot.main()
        result_path = a.output / 'result.json'
        result = json.loads(result_path.read_text())
        result.update(original_firmware_instructions_modified=bool(patch_files),
                      released_candidate=a.case == 'rc1', sd_module_loaded_by_test=False,
                      program_sha256=sha(candidate / '03_program.bin') if patch_files else sha(STOCK / '03_program.bin'),
                      panel_state_prefilled=False,
                      test_scope='Partial RTOS startup with explicit HLE/peer isolation; not complete camera boot or SD/menu validation')
        result_path.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({k: result.get(k) for k in ['stop_reason', 'graphics_swaps',
              'graphics_draws', 'menu_rendered', 'exception', 'graphics_error']}, indent=2))


if __name__ == '__main__':
    main()
