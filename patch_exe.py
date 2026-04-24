"""patch_exe.py -- lift the hardcoded 25-track cap in Juiced.exe.

The stock game binary has locations that hard-limit the music bank to 25
tracks.  This script patches them to allow up to N tracks (the minimum needed
for one injected song is 26; the limit can be raised further by re-running
with a larger --max value).

Usage:
    python patch_exe.py              # patch to 26 tracks (default)
    python patch_exe.py --max 30     # patch to 30 tracks
    python patch_exe.py --restore    # restore from Juiced.exe.bak
    python patch_exe.py --status     # show current patch state

The patcher backs up Juiced.exe to Juiced.exe.bak before the first patch and
never overwrites an existing backup.
"""
from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
GAME_ROOT = THIS_DIR.parent.parent
EXE      = GAME_ROOT / "Juiced.exe"
EXE_BAK  = GAME_ROOT / "Juiced.exe.bak"

# ---------------------------------------------------------------------------
# Patch table
# Each entry: (file_offset, magic_prefix, imm_offset_in_prefix, description)
# magic_prefix: N bytes immediately BEFORE the immediate (used to verify we're
# patching the right instruction).  imm_offset_in_prefix: how many bytes of
# prefix come before the byte we want to change.
# ---------------------------------------------------------------------------

# Site 1: CMP eax, N  (83 F8 <N>)  at file offset 0x00BE4665
#         We check for  "83 F8"  at offset 0x00BE4665 and patch byte at 0x00BE4667.
_SITE_CMP = {
    "offset":  0x00BE4665,
    "prefix":  bytes([0x83, 0xF8]),   # CMP eax, imm8
    "imm_off": 2,                     # immediate is the 3rd byte
    "desc":    "advance-track bounds check  (CMP eax, N; JGE)",
}

# Site 2: MOV ecx, N  (B9 <N> 00 00 00)  at file offset 0x00BD35C4
#         We check for  "B9"  at 0x00BD35C4 and patch byte at 0x00BD35C5.
_SITE_MOV = {
    "offset":  0x00BD35C4,
    "prefix":  bytes([0xB9]),         # MOV ecx, imm32
    "imm_off": 1,                     # immediate starts at byte 2 (low byte of dword)
    "desc":    "audio-handle array zero-init  (MOV ecx, N; REP STOSD)",
}

# Site 3: MOV eax, N  (B8 <N> 00 00 00)  at file offset 0x000AB4E9
#         This is the upper-bound clamp in the track-load / play path.
#         Code sequence:
#             ADD [edi+0x3EC], edx     ; advance track index
#             MOV eax, N               ; <-- this byte
#             CMP [edi+0x3EC], eax     ; compare new index against limit
#             JLE +6                   ; if new_index <= N, skip clamp
#             MOV [edi+0x3EC], eax     ; CLAMP: reset to N (bug: fires for track 26)
#         Without this patch, any track index > 25 is immediately reset to 25
#         before the audio engine starts playing it.
_SITE_CLAMP_PLAY = {
    "offset":  0x000AB4E9,
    "prefix":  bytes([0xB8]),         # MOV eax, imm32
    "imm_off": 1,
    "desc":    "track-load clamp  (MOV eax, N; CMP/JLE; clamp before play)",
}

# Site 4: MOV ebp, N  (BD <N> 00 00 00)  at file offset 0x000AB9D2
#         ebp is subsequently used as the clamp limit in a conditional
#         CMP [edi+0x3EC], ebp / JLE / MOV [edi+0x3EC], ebp sequence at
#         0x000ABA87.  Same bug: track 26 (1-based index 26) gets clamped to 25.
_SITE_CLAMP_COND = {
    "offset":  0x000AB9D2,
    "prefix":  bytes([0xBD]),         # MOV ebp, imm32
    "imm_off": 1,
    "desc":    "conditional clamp  (MOV ebp, N; used as CMP limit at 0xABA87)",
}

# Site 5: CMP [esi+4], N  (83 7E 04 <N>)  at file offset 0x00029362
#         Audio-event circular buffer full check.  The write pointer in
#         [esi+4] is compared against N; when equal the buffer is considered
#         full and the wrap/reset branch fires.  With N=25 the buffer treats
#         the 26th slot as always-full and never enqueues a track-26 event.
#         Code sequence:
#             83 46 04 01          ADD [esi+4], 1
#             83 7E 04 19          CMP [esi+4], 25    ← patch byte to N
#             75 07                JNZ +7             ; not full, skip wrap
#             C7 46 04 18 00 00 00 MOV [esi+4], 24    ← site 6
#             C2 0C 00             RETN 0x0C
_SITE_QUEUE_CHECK = {
    "offset":  0x00029362,
    "prefix":  bytes([0x83, 0x7E, 0x04]),   # CMP [esi+4], imm8
    "imm_off": 3,
    "desc":    "audio-event queue full check  (CMP [esi+4], N)",
}

# Site 6: MOV [esi+4], N-1  (C7 46 04 <N-1> 00 00 00)  at file offset 0x00029368
#         Wrap/reset: stores the last valid 0-based slot index (N-1).
#         Stock value = 24 (0x18); must become 25 (0x19) when N → 26.
#         value_offset = -1 tells apply_patch to write (new_count - 1) here.
_SITE_QUEUE_WRAP = {
    "offset":       0x00029368,
    "prefix":       bytes([0xC7, 0x46, 0x04]),   # MOV [esi+4], imm32 (low byte)
    "imm_off":      3,
    "value_offset": -1,                           # store new_count − 1
    "desc":         "audio-event queue wrap value  (MOV [esi+4], N-1)",
}

# Site 7: CMP [ebp+0x3EC], N  (83 BD EC 03 00 00 <N>)  at file offset 0x000AA726
#         Last-track detection: when [ebp+0x3EC] == N the game triggers its
#         end-of-bank handler (loop-around / crossfade).  Without this patch
#         track 26 is never recognised as the last track, so the handler never
#         fires and the player stalls at track 26.
#         Code sequence:
#             83 7A 04 04              CMP [edx+4], 4       ; precondition
#             75 4F                    JNZ → return
#             83 BD EC 03 00 00 19     CMP [ebp+0x3EC], 25  ← patch byte to N
#             75 3C                    JNZ → set global, return
#             ; special loop-around handling …
_SITE_LAST_TRACK = {
    "offset":  0x000AA726,
    "prefix":  bytes([0x83, 0xBD, 0xEC, 0x03, 0x00, 0x00]),  # CMP [ebp+0x3EC], imm8
    "imm_off": 6,
    "desc":    "last-track detection  (CMP [ebp+0x3EC], N; JNZ; loop-around)",
}

# Site 8: JE redirect in PREV function  (74 <offset>)  at file offset 0x000AAA8A
#         The PREV / retreat-track function (file offset 0x000AAA80) loads the
#         current 1-based track index into edx, then immediately does:
#             83 FA 01  CMP edx, 1
#             74 65     JE  → RETN          ; if already at track 1, do nothing
#         This is the "no wrap" early exit: pressing BACK from track 1 is a
#         silent no-op.  We redirect the JE to jump instead to the single-step
#         decrement at 0x000AAAC3 (offset 0x37 from the next instruction), so
#         that BACK from track 1 falls through to the normal decrement path.
#         Combined with Site 9 (below) the decrement then wraps to track N.
#         The target offset 0x37 is a fixed code-layout distance, not
#         track-count-dependent; fixed_value encodes it.
_SITE_PREV_EARLY_EXIT = {
    "offset":      0x000AAA86,
    "prefix":      bytes([0x83, 0xFA, 0x01, 0x74]),  # CMP edx,1 ; JE opcode
    "imm_off":     4,                                 # the JE displacement byte
    "fixed_value": 0x37,                              # jump to 000AAAC3 (decrement)
    "desc":        "PREV early-exit redirect  (JE from at-track-1 check -> decrement)",
}

# Site 9: PREV clamp-to-wrap  (C7 81 EC 03 00 00 <N> 00 00 00)  at 0x000AAAE6
#         After the single-step decrement (Site 8's target), the game checks:
#             83 B9 EC 03 00 00 01  CMP [ecx+0x3EC], 1
#             7D 0A                 JGE → RETN         ; if index >= 1, keep it
#             C7 81 EC 03 00 00     ← Site 9 starts here
#             01 00 00 00           MOV [ecx+0x3EC], 1 ; else clamp to first track
#         Changing the immediate from 1 to N makes the "clamp" become a
#         "wrap": when edx reaches 0 (decremented from track 1) the index is
#         set to N (the last track) instead of staying at 1.
_SITE_PREV_WRAP_CLAMP = {
    "offset":  0x000AAAE6,
    "prefix":  bytes([0xC7, 0x81, 0xEC, 0x03, 0x00, 0x00]),  # MOV [ecx+0x3EC], imm32
    "imm_off": 6,                                              # low byte of imm32
    "desc":    "PREV wrap-around  (MOV [ecx+0x3EC], N; wraps backward to last track)",
}

PATCH_SITES = [
    _SITE_CMP, _SITE_MOV, _SITE_CLAMP_PLAY, _SITE_CLAMP_COND,
    _SITE_QUEUE_CHECK, _SITE_QUEUE_WRAP, _SITE_LAST_TRACK,
    _SITE_PREV_EARLY_EXIT, _SITE_PREV_WRAP_CLAMP,
]

STOCK_TRACK_COUNT = 25   # never patch below this
MAX_TRACK_COUNT   = 255  # 8-bit immediate ceiling


def fail(msg: str) -> None:
    print("error: %s" % msg, file=sys.stderr)
    sys.exit(1)


def read_exe() -> bytearray:
    if not EXE.exists():
        fail("Juiced.exe not found at %s" % EXE)
    return bytearray(EXE.read_bytes())


def backup_exe() -> None:
    if not EXE_BAK.exists():
        shutil.copy2(EXE, EXE_BAK)
        print("  backup created: %s" % EXE_BAK.name)
    else:
        print("  backup already exists: %s  (not overwritten)" % EXE_BAK.name)


def restore_exe() -> None:
    if not EXE_BAK.exists():
        fail("no backup found at %s — cannot restore." % EXE_BAK)
    shutil.copy2(EXE_BAK, EXE)
    print("restored %s from backup." % EXE.name)


def _check_site(data: bytearray, site: dict) -> int:
    """Return the current immediate value at site, or raise ValueError if prefix mismatches."""
    off = site["offset"]
    prefix = site["prefix"]
    imm_off = site["imm_off"]
    actual_prefix = bytes(data[off : off + len(prefix)])
    if actual_prefix != prefix:
        raise ValueError(
            "prefix mismatch at 0x%08X: expected %s got %s" % (
                off,
                " ".join("%02X" % b for b in prefix),
                " ".join("%02X" % b for b in actual_prefix),
            )
        )
    return data[off + imm_off]


def _site_actual(site: dict, new_count: int) -> int:
    """Return the byte value to write for this site given new_count."""
    if "fixed_value" in site:
        return site["fixed_value"]
    return new_count + site.get("value_offset", 0)


def _apply_site(data: bytearray, site: dict, new_count: int) -> None:
    _check_site(data, site)  # validate prefix
    data[site["offset"] + site["imm_off"]] = _site_actual(site, new_count)


def status(data: bytearray) -> None:
    print("Juiced.exe patch status:")
    for site in PATCH_SITES:
        try:
            cur = _check_site(data, site)
            tag = " (fixed)" if "fixed_value" in site else ""
            print("  0x%08X  current=0x%02X (%d)%s  -- %s" % (
                site["offset"] + site["imm_off"], cur, cur, tag, site["desc"]))
        except ValueError as e:
            print("  0x%08X  UNRECOGNISED -- %s" % (site["offset"], e))
    bak_state = "present" if EXE_BAK.exists() else "absent"
    print("  backup (%s): %s" % (EXE_BAK.name, bak_state))


def apply_patch(new_count: int) -> None:
    if new_count <= STOCK_TRACK_COUNT:
        fail("--max %d is not larger than the stock track count (%d)." % (
            new_count, STOCK_TRACK_COUNT))
    if new_count > MAX_TRACK_COUNT:
        fail("--max %d exceeds the 8-bit immediate ceiling (%d)." % (
            new_count, MAX_TRACK_COUNT))

    data = read_exe()

    # Validate all sites before touching anything.
    for site in PATCH_SITES:
        try:
            _check_site(data, site)
        except ValueError as e:
            fail("unexpected bytes — is this the correct Juiced.exe?\n  %s" % e)

    backup_exe()

    for site in PATCH_SITES:
        byte_off = site["offset"] + site["imm_off"]
        old = data[byte_off]
        actual = _site_actual(site, new_count)
        _apply_site(data, site, new_count)
        print("  patched 0x%08X: 0x%02X -> 0x%02X  (%s)" % (byte_off, old, actual, site["desc"]))

    EXE.write_bytes(data)
    print("\nJuiced.exe patched for %d-track bank." % new_count)
    print("Run inject.py to build the bank, then launch Juiced.")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Lift the hardcoded 25-track cap in Juiced.exe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--max", type=int, default=26,
        metavar="N",
        help="New track-count limit (default: 26, must be >25 and <=255).",
    )
    p.add_argument(
        "--restore", action="store_true",
        help="Restore Juiced.exe from Juiced.exe.bak.",
    )
    p.add_argument(
        "--status", action="store_true",
        help="Print current patch state without modifying anything.",
    )
    args = p.parse_args()

    if args.restore:
        restore_exe()
        return 0

    if args.status:
        status(read_exe())
        return 0

    apply_patch(args.max)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
