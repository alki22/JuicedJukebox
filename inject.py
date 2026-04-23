"""JuicedJukebox — append user audio tracks to the Juiced soundtrack.

Usage (run from any directory):
    python inject.py                           # scan ./input, append to game
    python inject.py --input C:/music/extra    # scan a different folder
    python inject.py --restore                 # restore stock bank from .bak
    python inject.py --dry-run                 # show what would happen, no writes

Filename convention for input files:
    {artist} - {title}.{mp3|flac|wav|wma|m4a|ogg}

The tool:
  1. Backs up music.cfg / music.dat / music.dsb to *.bak on first run.
  2. Re-encodes each input to wmav2 44.1kHz stereo 128kbps (stock params).
  3. Stream-concats the stock music.dsb + new WMAs into a new music.dsb.
  4. Appends 164-byte records to music.dat (count += N, size_units in seconds).
  5. Rewrites music.cfg to include new declare_stream / [BeginSS] entries.
  6. Patches Juiced.exe to lift the hardcoded 25-track cap (one-time, backed up).

Both the JuicedNowPlaying overlay and the in-game audio menu read music.cfg
at startup, so new tracks show up in both UIs automatically.
"""
from __future__ import annotations

import argparse
import re
import shutil
import struct
import sys
import tempfile
from pathlib import Path

import bankfmt
import patch_exe
import wma

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".wma", ".m4a", ".ogg"}

# The game installs music files here relative to the script's location
# (scripts/JuicedJukebox/inject.py → ../../audio/music).
THIS_DIR = Path(__file__).resolve().parent
GAME_ROOT = THIS_DIR.parent.parent
MUSIC_DIR = GAME_ROOT / "audio" / "music"
CFG = MUSIC_DIR / "music.cfg"
DAT = MUSIC_DIR / "music.dat"
DSB = MUSIC_DIR / "music.dsb"

# Cached re-encode of the stock DSB (see wma.rebuild_dsb for why this exists).
# Stored next to the script, not in the game dir, so --restore doesn't touch it.
CACHE_DIR = THIS_DIR / ".cache"
REBUILT_STOCK_DSB = CACHE_DIR / "stock_rebuilt.dsb"

FILENAME_RE = re.compile(r"^\s*(?P<artist>.+?)\s*-\s*(?P<title>.+?)\s*$")


def _patch_stock_codec_data(src: Path, dst: Path) -> None:
    """Copy stock DSB, patching the codec_data trailing 4 bytes to 00000000.

    The stock WMA SDK (c.2004) wrote codec_data=008800000f00 e55c0000.
    Modern Windows MediaTranscoder writes 008800000f00 00000000.
    Only the first 6 bytes matter for decoder configuration; the last 4 are
    a version hint.  We zero them so the header matches new-track packets.
    """
    import struct as _struct
    data = bytearray(src.read_bytes())
    SPO_GUID = bytes.fromhex("9107dcb7b7a9cf118ee600c00c205365")
    hdr_size = _struct.unpack_from("<Q", data, 16)[0]
    off = data.find(SPO_GUID, 0, hdr_size)
    if off < 0:
        raise RuntimeError("Stream Properties Object not found in stock DSB")
    wfx_off = off + 78
    cd_off = wfx_off + 18          # WAVEFORMATEX is 18 bytes, codec_data follows
    data[cd_off + 6 : cd_off + 10] = b"\x00\x00\x00\x00"
    dst.write_bytes(bytes(data))


def fail(msg: str) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def ensure_backups() -> None:
    """Create .bak copies on first run. Never overwrites an existing backup."""
    for src in (CFG, DAT, DSB):
        bak = src.with_suffix(src.suffix + ".bak")
        if not bak.exists():
            if not src.exists():
                fail(f"stock file missing: {src}")
            shutil.copy2(src, bak)
            print(f"  backup: {bak.name}")


def restore_backups() -> None:
    restored = 0
    for src in (CFG, DAT, DSB):
        bak = src.with_suffix(src.suffix + ".bak")
        if not bak.exists():
            print(f"  skip (no backup): {src.name}")
            continue
        shutil.copy2(bak, src)
        restored += 1
        print(f"  restored: {src.name}")
    if restored == 0:
        fail("no backups found. Nothing to restore.")


def scan_inputs(input_dir: Path) -> list[tuple[Path, str, str]]:
    """Return [(path, artist, title)] for every valid audio file in input_dir."""
    if not input_dir.exists():
        fail(f"input dir does not exist: {input_dir}")
    entries = []
    rejected = []
    for p in sorted(input_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in AUDIO_EXTS:
            continue
        m = FILENAME_RE.match(p.stem)
        if not m:
            rejected.append(p.name)
            continue
        entries.append((p, m.group("artist"), m.group("title")))
    if rejected:
        print("  warning: these files don't match '{artist} - {title}.{ext}' and were skipped:")
        for n in rejected:
            print(f"    - {n}")
    return entries


def load_stock_records() -> list[bankfmt.Record]:
    """Load records from the .bak copy of music.dat (the pristine state)."""
    bak = DAT.with_suffix(".dat.bak")
    if not bak.exists():
        fail(f"backup missing: {bak}. Run without --restore first.")
    return bankfmt.decode(bak.read_bytes())


def read_cfg_backup() -> str:
    bak = CFG.with_suffix(".cfg.bak")
    if not bak.exists():
        fail(f"backup missing: {bak}")
    return bak.read_text(encoding="ascii")


def rewrite_cfg(stock_cfg: str, new_names: list[str], new_displays: list[str]) -> str:
    """Insert new declare_stream + [BeginSS] entries into the stock cfg text."""
    total = 25 + len(new_names)

    # Bump bank_size and instance_count (stock: "bank_size 25", "instance_count 25")
    cfg = re.sub(r"(?m)^bank_size\s+\d+", f"bank_size {total}", stock_cfg)
    cfg = re.sub(r"(?m)^instance_count\s+\d+", f"instance_count {total}", cfg)

    # Inject new declare_stream lines before [EndSamples].
    new_decl_lines = "".join(f"declare_stream {n}\n" for n in new_names)
    cfg = cfg.replace("[EndSamples]", new_decl_lines + "[EndSamples]")

    # Append new [BeginSS] blocks before [End].
    blocks = []
    for name, disp in zip(new_names, new_displays):
        blocks.append(
            "[BeginSS]\n"
            f"name {name}\n"
            f'displayname "{disp}"\n'
            "type 2\n"
            f"sample\t\t  {name}\n"
            "instances 1\n"
            "[EndSS]\n \n"
        )
    appended = "".join(blocks)
    cfg = cfg.replace("[End]", appended + "[End]")
    return cfg


def inject(input_dir: Path, dry_run: bool) -> int:
    print(f"game root:     {GAME_ROOT}")
    print(f"music dir:     {MUSIC_DIR}")
    print(f"input dir:     {input_dir}")

    inputs = scan_inputs(input_dir)
    if not inputs:
        fail("no valid input files. Expected '{artist} - {title}.{mp3|flac|...}'.")

    print(f"found {len(inputs)} input track(s):")
    for _, artist, title in inputs:
        print(f'  + "{artist} - {title}"')

    if dry_run:
        print("\n--dry-run: stopping before any writes.")
        return 0

    print("\nbacking up stock files (first run only)...")
    ensure_backups()

    stock_records = load_stock_records()
    if len(stock_records) != 25:
        fail(f"stock music.dat has {len(stock_records)} tracks, expected 25. Aborting.")

    # Read stock packet size once — new WMAs must match it exactly so we can
    # binary-append packets without any re-packetization.
    stock_dsb_bak = DSB.with_suffix(".dsb.bak")
    stock_pkt_size = wma.asf_packet_size(stock_dsb_bak)
    print(f"\nstock DSB packet size: {stock_pkt_size} bytes")

    # Work in a temp dir so a failure mid-way doesn't corrupt game files.
    with tempfile.TemporaryDirectory(prefix="juicedjukebox_") as td:
        tdp = Path(td)
        print(f"encoding {len(inputs)} input(s) to WMA8 128k/44.1kHz/stereo "
              f"(Windows native encoder)...")
        encoded: list[Path] = []
        for i, (src, artist, title) in enumerate(inputs, start=1):
            dst = tdp / f"new_{i:03d}.wma"
            print(f"  [{i}/{len(inputs)}] {src.name}")
            wma.encode_to_wma(src, dst, packet_size=stock_pkt_size)
            encoded.append(dst)

        # Prepare a working copy of the stock DSB with the codec_data trailing
        # bytes patched to match what Windows MediaTranscoder emits.
        # Stock codec_data: 008800000f00 e55c0000 (MS WMA SDK 9, 2004-era)
        # MediaTranscoder:  008800000f00 00000000 (modern Windows WMA8)
        # The first 6 bytes are the critical decoder config; the last 4 are a
        # version/super-block hint that the modern decoder ignores.
        # Patching them to 00000000 makes the header match what our new packets
        # were encoded with, so the DirectShow decoder configures itself
        # correctly for both stock and new packets.
        working_stock = tdp / "stock_working.dsb"
        _patch_stock_codec_data(stock_dsb_bak, working_stock)
        print(f"\nprepared working stock DSB: {working_stock.stat().st_size:,} bytes")

        # Binary-append each new track one at a time, carrying the growing DSB
        # forward.  New packets are re-padded to stock packet size (5976) so
        # the ASF remains consistent.  Stock packet data is never modified.
        print("\nbinary-appending new tracks to stock DSB...")
        current_dsb = working_stock
        new_records = list(stock_records)
        wfx   = stock_records[0].wfx
        pad1  = stock_records[0].pad1
        pad2  = stock_records[0].pad2
        const = stock_records[0].const

        for i, (encoded_wma, (_, artist, title)) in enumerate(zip(encoded, inputs), start=1):
            next_dsb = tdp / f"music_{i:03d}.dsb"
            start_ms, end_ms = wma.append_track_to_dsb(current_dsb, encoded_wma, next_dsb)
            current_dsb = next_dsb

            # cum_offset_units is the cumulative sum of all preceding track durations,
            # matching how the stock music.dat is laid out (not derived from timestamps).
            cum = sum(r.size_units for r in new_records)
            su  = wma.duration_units((end_ms - start_ms) / 1000)
            new_records.append(bankfmt.Record(
                size_units=su, wfx=wfx, const=const,
                cum_offset_units=cum,
                name=f"track{25 + i:02d}",
                pad1=pad1, pad2=pad2,
            ))
            print(f"  track{25+i:02d}: {artist} - {title}  "
                  f"start={start_ms/1000:.1f}s  dur={su}s")

        # Build new cfg text.
        stock_cfg = read_cfg_backup()
        new_names    = [r.name for r in new_records[25:]]
        new_displays = [f"{a} - {t}" for _, a, t in inputs]
        new_cfg = rewrite_cfg(stock_cfg, new_names, new_displays)

        # Commit atomically: write cfg + dat first (small), then move DSB last.
        print("\nwriting game files...")
        CFG.write_text(new_cfg, encoding="ascii")
        print(f"  {CFG.name}  ({len(new_cfg)} bytes)")

        dat_bytes = bankfmt.encode(new_records)
        DAT.write_bytes(dat_bytes)
        print(f"  {DAT.name}  ({len(dat_bytes)} bytes, {len(new_records)} records)")

        shutil.move(str(current_dsb), DSB)
        dsb_size = DSB.stat().st_size
        print(f"  {DSB.name}  ({dsb_size:,} bytes)")

    # Patch Juiced.exe to raise the 25-track cap to match the new bank size.
    # patch_exe.py backs up the exe on first run and is idempotent (re-patching
    # to the same value is a no-op).
    new_track_count = len(new_records)
    print("\npatching Juiced.exe for %d-track bank..." % new_track_count)
    try:
        patch_exe.apply_patch(new_track_count)
    except SystemExit:
        # apply_patch calls sys.exit on fatal errors; convert to inject failure.
        fail("exe patch failed — see errors above.")

    total_secs = sum(r.size_units for r in new_records)
    print(
        f"\ndone. bank now has {len(new_records)} tracks "
        f"(~{total_secs // 60}m{total_secs % 60:02d}s total)."
    )
    print("Launch Juiced and try it in-race — NowPlaying overlay and audio menu should show the new titles.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Append custom audio tracks to Juiced's soundtrack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--input",
        type=Path,
        default=THIS_DIR / "input",
        help=f"Folder to scan for audio files (default: {THIS_DIR / 'input'})",
    )
    p.add_argument(
        "--restore",
        action="store_true",
        help="Restore stock soundtrack from .bak files.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan inputs and report what would happen; do not modify game files.",
    )
    args = p.parse_args()

    if args.restore:
        print("restoring stock soundtrack...")
        restore_backups()
        print("restoring Juiced.exe...")
        patch_exe.restore_exe()
        return 0

    return inject(args.input.resolve(), args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
