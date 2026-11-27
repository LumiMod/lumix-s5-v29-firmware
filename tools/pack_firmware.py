#!/usr/bin/env python3
# RULES-ACTIVE-GLOBAL
"""Pack patched DC-S5 V2.9 components into an AES-128-CBC (5-round) encrypted .bin container,
updating component SHA-256 hashes and outer CRC32, with full round-trip decryption verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OFFICIAL_BIN = Path("/Users/lixiuqi/Documents/ChatGPT/相机/s5-v29-offline/S5___V29.bin")
DEFAULT_PATCH_DIR = REPO_ROOT / "build"
DEFAULT_OUT_BIN = REPO_ROOT / "S5___V29_ALL_PATCHED.bin"
DEFAULT_TOOL = REPO_ROOT / "tools" / "panasonic-aes5"

OFFICIAL_SHA256 = "b2ec4aeec209693b874df8dfdf11fc85b38fa4675b44ec554b7a167f430827e1"
KEY_HEX = "194364775255595d04e48de2b15e5c2c"

TABLE_BASE = 0x2EC
ENTRY_SIZE = 0x5C
DATA_BASE = 0x200

BASE_TARGETS = (
    (3, "03_program.bin"),
    (7, "07_postboot3.bin"),
    (20, "20_osdover.bin"),
)
OPTIONAL_TARGETS = (
    (19, "19_music.bin"),
)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def parse_entry(pkg: bytes | bytearray, index: int) -> dict:
    o = TABLE_BASE + index * ENTRY_SIZE
    name = pkg[o:o + 12].split(b"\x00", 1)[0].decode("ascii")
    rel_off, size, unk, enc = struct.unpack_from("<IIII", pkg, o + 0x0C)
    return {
        "index": index,
        "name": name,
        "table_off": o,
        "offset": rel_off + DATA_BASE,
        "size": size,
        "encoding": enc,
        "sha256": pkg[o + 0x1C:o + 0x3C].hex(),
        "iv": pkg[o + 0x3C:o + 0x4C].hex(),
    }


def run_aes5(tool: Path, src: Path, dst: Path, iv: str, offset: int, size: int, encrypt: bool) -> None:
    cmd = [str(tool)]
    if encrypt:
        cmd.append("--encrypt")
    cmd += [str(src), str(dst), KEY_HEX, iv, str(offset), str(size)]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=90)
    if res.returncode != 0:
        raise RuntimeError(f"panasonic-aes5 failed (rc={res.returncode}): {res.stderr}")
    if dst.stat().st_size != size:
        raise RuntimeError(f"Size mismatch from panasonic-aes5: {dst.stat().st_size} != {size}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Pack & round-trip verify S5 V2.9 firmware container")
    ap.add_argument("--official-bin", type=Path, default=DEFAULT_OFFICIAL_BIN)
    ap.add_argument("--patch-dir", type=Path, default=DEFAULT_PATCH_DIR)
    ap.add_argument("--out-bin", type=Path, default=DEFAULT_OUT_BIN)
    ap.add_argument("--aes-tool", type=Path, default=DEFAULT_TOOL)
    args = ap.parse_args()

    official = args.official_bin.read_bytes()
    assert sha256(official) == OFFICIAL_SHA256, "Official S5___V29.bin SHA-256 mismatch!"
    pkg = bytearray(official)

    targets = list(BASE_TARGETS)
    for idx, filename in OPTIONAL_TARGETS:
        if (args.patch_dir / filename).is_file():
            targets.append((idx, filename))
    targets.sort(key=lambda item: item[0])

    report = {"official_sha256": OFFICIAL_SHA256, "output": str(args.out_bin), "components": []}

    with tempfile.TemporaryDirectory(prefix=".pack-", dir=args.patch_dir) as tmp:
        tmp_dir = Path(tmp)
        for idx, filename in targets:
            rec = parse_entry(official, idx)
            assert rec["encoding"] == 3
            plain_path = args.patch_dir / filename
            plain_bytes = plain_path.read_bytes()
            assert len(plain_bytes) == rec["size"], f"{filename} size mismatch"

            enc_path = tmp_dir / f"{idx:02d}_enc.bin"
            run_aes5(args.aes_tool, plain_path, enc_path, rec["iv"], 0, rec["size"], encrypt=True)

            pkg[rec["offset"]:rec["offset"] + rec["size"]] = enc_path.read_bytes()
            plain_sha = sha256(plain_bytes)
            pkg[rec["table_off"] + 0x1C:rec["table_off"] + 0x3C] = bytes.fromhex(plain_sha)
            report["components"].append({
                "index": idx, "name": rec["name"], "file": filename,
                "size": rec["size"], "iv": rec["iv"], "plaintext_sha256": plain_sha,
            })

        crc = zlib.crc32(pkg[0x200:]) & 0xFFFFFFFF
        struct.pack_into("<I", pkg, 0x40, crc)
        args.out_bin.write_bytes(pkg)

        written = args.out_bin.read_bytes()
        assert len(written) == len(official)
        assert struct.unpack_from("<I", written, 0x40)[0] == (zlib.crc32(written[0x200:]) & 0xFFFFFFFF)
        for idx, filename in targets:
            rec = parse_entry(written, idx)
            dec_path = tmp_dir / f"{idx:02d}_dec_verify.bin"
            run_aes5(args.aes_tool, args.out_bin, dec_path, rec["iv"], rec["offset"], rec["size"], encrypt=False)
            dec_bytes = dec_path.read_bytes()
            assert dec_bytes == (args.patch_dir / filename).read_bytes()
            assert sha256(dec_bytes) == rec["sha256"]

    report["output_sha256"] = sha256(args.out_bin.read_bytes())
    report["outer_crc32"] = f"0x{crc:08x}"
    report["round_trip_verified"] = True
    (args.patch_dir / "package_manifest.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Packed & verified: {args.out_bin} ({args.out_bin.stat().st_size} bytes)")
    print(f"Output SHA-256   : {report['output_sha256']}")
    print(f"Outer CRC32      : {report['outer_crc32']}")


if __name__ == "__main__":
    main()
