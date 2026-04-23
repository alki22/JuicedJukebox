"""music.dat codec for the Juiced soundtrack bank.

Layout (little-endian):
    u32  count
    count * 164-byte Record:
        [0x00] u32  size_units          # per-track length in the custom dsb unit
        [0x04] u32  cbSize (= 18)       # WAVEFORMATEX size
        [0x08] 4    tag "wmax"
        [0x0C] 18   WAVEFORMATEX payload (observed: 12*0xA1 then 12 00 in stock bank)
        [0x1E] 106  opaque pad (0xCC in stock, preserved verbatim on round-trip)
        [0x88] u32  const_0x1008
        [0x8C] 4    pad (0xCC in stock, preserved verbatim)
        [0x90] u32  cum_offset_units    # = sum(size_units[0..i-1])
        [0x94] 16   name ascii, null-padded (e.g. "track01")

For new records we emit the observed stock byte pattern for the opaque
regions so the game sees what it wrote itself.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

RECORD_SIZE = 164
HEADER_SIZE = 4
WAVEFORMATEX_SIZE = 18
TAG = b"wmax"
CONST_0x1008 = 0x1008

# Byte positions inside a record.
OFF_SIZE_UNITS = 0x00
OFF_CB_SIZE = 0x04
OFF_TAG = 0x08
OFF_WFX = 0x0C           # 18 bytes
OFF_PAD1 = 0x1E          # 106 bytes
OFF_CONST = 0x88         # u32
OFF_PAD2 = 0x8C          # 4 bytes
OFF_CUM = 0x90           # u32
OFF_NAME = 0x94          # 16 bytes

# Fills observed in the shipped bank for the opaque pad regions.
STOCK_PAD1 = b"\xCC" * 106
STOCK_PAD2 = b"\xCC" * 4


@dataclass
class Record:
    size_units: int
    wfx: bytes           # 18 bytes
    const: int           # usually CONST_0x1008
    cum_offset_units: int
    name: str
    pad1: bytes = field(default=STOCK_PAD1)
    pad2: bytes = field(default=STOCK_PAD2)

    def pack(self) -> bytes:
        if len(self.wfx) != WAVEFORMATEX_SIZE:
            raise ValueError(f"wfx must be {WAVEFORMATEX_SIZE} bytes, got {len(self.wfx)}")
        if len(self.pad1) != 106:
            raise ValueError(f"pad1 must be 106 bytes, got {len(self.pad1)}")
        if len(self.pad2) != 4:
            raise ValueError(f"pad2 must be 4 bytes, got {len(self.pad2)}")
        name_bytes = self.name.encode("ascii")
        if len(name_bytes) > 16:
            raise ValueError(f"name too long: {self.name!r}")
        name_bytes = name_bytes.ljust(16, b"\x00")
        buf = bytearray(RECORD_SIZE)
        struct.pack_into("<I", buf, OFF_SIZE_UNITS, self.size_units)
        struct.pack_into("<I", buf, OFF_CB_SIZE, WAVEFORMATEX_SIZE)
        buf[OFF_TAG:OFF_TAG + 4] = TAG
        buf[OFF_WFX:OFF_WFX + WAVEFORMATEX_SIZE] = self.wfx
        buf[OFF_PAD1:OFF_PAD1 + 106] = self.pad1
        struct.pack_into("<I", buf, OFF_CONST, self.const)
        buf[OFF_PAD2:OFF_PAD2 + 4] = self.pad2
        struct.pack_into("<I", buf, OFF_CUM, self.cum_offset_units)
        buf[OFF_NAME:OFF_NAME + 16] = name_bytes
        return bytes(buf)

    @classmethod
    def unpack(cls, blob: bytes) -> "Record":
        if len(blob) != RECORD_SIZE:
            raise ValueError(f"record must be {RECORD_SIZE} bytes, got {len(blob)}")
        size_units = struct.unpack_from("<I", blob, OFF_SIZE_UNITS)[0]
        cb_size = struct.unpack_from("<I", blob, OFF_CB_SIZE)[0]
        if cb_size != WAVEFORMATEX_SIZE:
            raise ValueError(f"cbSize expected {WAVEFORMATEX_SIZE}, got {cb_size}")
        tag = bytes(blob[OFF_TAG:OFF_TAG + 4])
        if tag != TAG:
            raise ValueError(f"tag expected {TAG!r}, got {tag!r}")
        wfx = bytes(blob[OFF_WFX:OFF_WFX + WAVEFORMATEX_SIZE])
        pad1 = bytes(blob[OFF_PAD1:OFF_PAD1 + 106])
        const = struct.unpack_from("<I", blob, OFF_CONST)[0]
        pad2 = bytes(blob[OFF_PAD2:OFF_PAD2 + 4])
        cum = struct.unpack_from("<I", blob, OFF_CUM)[0]
        name = bytes(blob[OFF_NAME:OFF_NAME + 16]).split(b"\x00", 1)[0].decode("ascii")
        return cls(
            size_units=size_units, wfx=wfx, const=const,
            cum_offset_units=cum, name=name, pad1=pad1, pad2=pad2,
        )


def decode(data: bytes) -> list[Record]:
    count = struct.unpack_from("<I", data, 0)[0]
    expected = HEADER_SIZE + count * RECORD_SIZE
    if len(data) != expected:
        raise ValueError(f"music.dat size {len(data)} != expected {expected} for count={count}")
    records = []
    for i in range(count):
        off = HEADER_SIZE + i * RECORD_SIZE
        records.append(Record.unpack(data[off:off + RECORD_SIZE]))
    return records


def encode(records: list[Record]) -> bytes:
    buf = bytearray()
    buf += struct.pack("<I", len(records))
    for r in records:
        buf += r.pack()
    return bytes(buf)


def recompute_cum_offsets(records: list[Record]) -> None:
    """Fix cum_offset_units in place so track[0]=0, track[i]=sum(size_units[0..i-1])."""
    running = 0
    for r in records:
        r.cum_offset_units = running
        running += r.size_units


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="music.dat round-trip tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="decode music.dat to a human-readable summary")
    d.add_argument("path", type=Path)

    r = sub.add_parser("roundtrip", help="decode then re-encode; verify byte-identical")
    r.add_argument("path", type=Path)

    args = p.parse_args()

    if args.cmd == "dump":
        data = args.path.read_bytes()
        records = decode(data)
        print(f"count={len(records)}")
        for i, rec in enumerate(records):
            print(
                f"[{i:02d}] size_units={rec.size_units:>6d} "
                f"cum_units={rec.cum_offset_units:>6d} "
                f"const={hex(rec.const)} name={rec.name!r}"
            )
        return 0

    if args.cmd == "roundtrip":
        original = args.path.read_bytes()
        records = decode(original)
        rebuilt = encode(records)
        if original == rebuilt:
            print(f"OK: {len(original)} bytes, {len(records)} records — byte-identical")
            return 0
        # Byte-by-byte diff of first mismatch for debugging.
        for i, (a, b) in enumerate(zip(original, rebuilt)):
            if a != b:
                print(f"FAIL: first diff at offset {i}: orig=0x{a:02X} rebuilt=0x{b:02X}")
                break
        print(f"sizes: orig={len(original)} rebuilt={len(rebuilt)}")
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
