#!/usr/bin/env python3
# RULES-ACTIVE-GLOBAL
"""Boot patched DC-S5 V2.9 firmware in the 4-core QEMU s5-rtos emulator (LumiMod/s5-emulator)."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import struct
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EMU_ROOT = Path("/Users/lixiuqi/Documents/ChatGPT/相机/s5-emulator")
DEFAULT_STOCK_DIR = Path("/Users/lixiuqi/Documents/ChatGPT/相机/s5-v29-offline/unpacked-v29")
DEFAULT_PATCHED_DIR = REPO_ROOT / "build"
DEFAULT_OUT_DIR = REPO_ROOT / "qemu_test_run"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description="Boot patched S5 V2.9 in 4-core QEMU")
    ap.add_argument("--emu-root", type=Path, default=DEFAULT_EMU_ROOT)
    ap.add_argument("--stock-dir", type=Path, default=DEFAULT_STOCK_DIR)
    ap.add_argument("--patched-dir", type=Path, default=DEFAULT_PATCHED_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    sys.path.insert(0, str(args.emu_root))
    sys.path.insert(0, str(args.emu_root / "menu-smp"))
    import boot as smp_boot  # noqa: E402
    import display.graphics_hle as ghle  # noqa: E402

    if args.out_dir.exists():
        shutil.rmtree(args.out_dir)

    orig_init = ghle.GraphicsHLE.__init__
    orig_handle = ghle.GraphicsHLE.handle

    def patched_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        if 0x064C3CDA in self.breakpoints:
            self.breakpoints[0x064C3CDE] = self.breakpoints.pop(0x064C3CDA)

    def patched_handle(self, pc, regs):
        if pc == 0x064C3CDE:
            sp = regs["r13"] if isinstance(regs, dict) else regs[13]
            saved = struct.unpack("<6I", self.emu.read_mem(sp, 24))
            for idx, reg in enumerate(("r4", "r5", "r6", "r7", "r8", "r14")):
                self.emu.write_reg(reg, saved[idx])
                if isinstance(regs, dict):
                    regs[reg] = saved[idx]
            self.emu.write_reg("r13", sp + 24)
            if isinstance(regs, dict):
                regs["r13"] = sp + 24
        return orig_handle(self, pc, regs)

    ghle.GraphicsHLE.__init__ = patched_init
    ghle.GraphicsHLE.handle = patched_handle

    with tempfile.TemporaryDirectory(prefix="s5-patched-qemu-") as td:
        stage = Path(td) / "s5-v29-offline" / "unpacked-v29"
        stage.mkdir(parents=True)
        for src_f in args.stock_dir.glob("*.bin"):
            shutil.copy2(src_f, stage / src_f.name)
        shutil.copy2(args.patched_dir / "07_postboot3.bin", stage / "07_postboot3.bin")
        shutil.copy2(args.patched_dir / "20_osdover.bin", stage / "20_osdover.bin")

        prog = bytearray((args.patched_dir / "03_program.bin").read_bytes())
        st_off = 0x07108000 - 0x06280000
        struct.pack_into(
            "<IBBBBBBBBBBBBIII", prog, st_off,
            0x53354343, 2, 0, 1, 0, 0, 0, 1, 1, 2, 2, 0, 1, 0, 0, 1,
        )
        prog[st_off + 28:st_off + 36] = b"Fuji_NC\x00"
        prog[st_off + 48] = 1
        lst_off = 0x07108100 - 0x06280000
        prog[lst_off:lst_off + 15] = b"Kodak2383.cube\x00"
        prog[lst_off + 44:lst_off + 54] = b"Kodak2383\x00"
        prog[lst_off + 64:lst_off + 76] = b"Fuji_NC.vlt\x00"
        prog[lst_off + 108:lst_off + 116] = b"Fuji_NC\x00"
        (stage / "03_program.bin").write_bytes(prog)

        orig_verify = smp_boot.verify_firmware

        def patched_verify():
            m = orig_verify()
            for index, item in enumerate(m["components"]):
                fname = f"{index:02d}_{item['name']}.bin"
                if fname in ("03_program.bin", "07_postboot3.bin", "20_osdover.bin"):
                    item["sha256"] = sha256(stage / fname)
            return m

        emu_proxy = Path(td) / "s5-emulator"
        emu_proxy.mkdir()
        for sub in ("toolchain", "display", "probe", "qemu", "menu-smp", "firmware", "prepared"):
            src_sub = args.emu_root / sub
            if src_sub.exists():
                (emu_proxy / sub).symlink_to(src_sub)

        smp_boot.ROOT = emu_proxy
        smp_boot.verify_firmware = patched_verify

        sys.argv = [
            "boot.py", "--output", str(args.out_dir),
            "--storage-hle", "--capture-gfx", "--graphics-hle",
            "--spi0-open-bus", "--spi6-open-bus", "--adc11-sample", "0x300",
            "--isolate-display", "--isolate-display-ticks", "600000",
            "--stop-after-swaps", "1", "--seconds", "45",
        ]
        smp_boot.main()


if __name__ == "__main__":
    main()
