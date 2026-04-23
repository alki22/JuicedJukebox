# JuicedJukebox

A Python tool that appends custom audio tracks to **Juiced**'s (Juice Games / THQ, 2005)
stock 25-track soundtrack. New tracks show up in both the in-game audio menu and the
[JuicedNowPlaying](https://github.com/alki22/JuicedNowPlaying) overlay automatically.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Windows 10+](https://img.shields.io/badge/platform-Windows%2010%2B-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- Adds any number of tracks on top of the stock 25 — originals are untouched
- Re-encodes input audio with the Windows native WMA encoder (same codec family as stock)
- Full backup + one-command restore back to the original soundtrack
- Patches `Juiced.exe` automatically to lift the hardcoded 25-track limit
- `--dry-run` mode to preview what would happen before touching any game files
- Accepts `.mp3`, `.flac`, `.wav`, `.wma`, `.m4a`, `.ogg`

## Prerequisites

- **Python 3.10+** (Windows)
- **Windows 10 or 11** — the encoder uses the built-in WinRT `MediaTranscoder`
  (invoked automatically via `powershell.exe`; no extra installs needed)
- **Juiced** (2005 PC release) installed, with the game files under `C:\Games\Juiced\`

## Usage

```bat
cd C:\Games\Juiced\scripts\JuicedJukebox

REM 1. Drop audio files into .\input\ named "{artist} - {title}.{ext}"
REM    Supported: .mp3  .flac  .wav  .wma  .m4a  .ogg
REM    Example:  "Jamiroquai - Love Foolosophy.flac"

REM 2. (Optional) Preview what would happen — no writes.
python inject.py --dry-run

REM 3. Inject.
python inject.py

REM 4. Launch Juiced. Enter a race. New tracks are in the audio menu and the
REM    NowPlaying overlay. Use ',' / '.' to skip between tracks.

REM --- revert everything to stock ---
python inject.py --restore
```

Scan a different folder:

```bat
python inject.py --input "D:\my music\juiced extras"
```

## How it works

Juiced's soundtrack is three coupled files under `audio\music\`:

| File | Role |
|------|------|
| `music.cfg` | Human-readable. Declares `bank_size`, lists every stream name, and defines `displayname` per track. Both the game and JuicedNowPlaying read this at startup. |
| `music.dat` | Binary. 4-byte count + N × 164-byte records. Each record holds the track's duration in seconds (`size_units`), its cumulative start time (`cum_offset_units`), and a 16-byte ASCII name. |
| `music.dsb` | A single ASF/WMA container (~104 MB) holding all 25 tracks concatenated into one continuous stream. The game seeks by the per-track second offsets in `music.dat`. |

On each run the tool:

1. **Backs up** `music.cfg`, `music.dat`, and `music.dsb` to `*.bak` (first run only; never overwrites an existing backup).
2. **Re-encodes** each input through the Windows WinRT `MediaTranscoder` — WMA8, 44.1 kHz, stereo, 128 kbps — matching the stock codec family exactly.
3. **Patches the DSB header**: zeroes the trailing 4 bytes of `codec_data` in the ASF Stream Properties Object (`e55c0000` → `00000000`). Stock packets were produced by a 2004-era MS WMA SDK; new packets come from the modern Windows encoder. Only the first 6 bytes of the 10-byte `codec_data` field carry actual decoder configuration; the last 4 are a version hint that the modern decoder ignores. Zeroing them makes the header consistent for both old and new packets.
4. **Binary-appends** the new packets into the DSB, adjusting three fields in every packet header so the WM ASF Reader doesn't discard them:
   - `send_time` — shifted by the stock stream's end timestamp so playback is continuous.
   - `object_number` — continues from the last stock packet's value instead of resetting to 0.
   - `presentation_time` (in replicated data) — advanced by the same preroll offset as the stock stream.
5. **Appends records** to `music.dat` (one per new track), updating the count and cumulative offsets.
6. **Rewrites** `music.cfg` to bump `bank_size` / `instance_count` and add `declare_stream` + `[BeginSS]` blocks for the new tracks.
7. **Patches `Juiced.exe`** at 7 sites to raise the hardcoded 25-track limit — the exe is backed up to `Juiced.exe.bak` before the first patch and the operation is idempotent (re-running is safe).

`--restore` copies all four `*.bak` files back over their originals and reverts `Juiced.exe`.

Re-running `inject.py` always rebuilds from the `*.bak` originals, so running it twice with a different `input/` folder replaces the appended set rather than doubling it.

## Design notes

### Why the Windows native encoder instead of ffmpeg?

Juiced's audio engine uses DirectShow's **WM ASF Reader** filter (`quartz.dll` / `wmf`).
It initialises its WMA decoder from the `codec_private_data` embedded in the ASF header and
is strict about the bitstream format matching that configuration.

`ffmpeg`'s `wmav2` encoder at 128 kbps produces a different super-frame structure
(`block_align` = 743, single-frame per packet) that the WMF decoder rejects when the
header was written for the stock super-frame layout (`block_align` = 5945, multiple frames
bundled per packet). Forcing `ffmpeg` to reproduce the stock `block_align` requires a
~1 Mbps encoding that is incompatible in other ways.

The Windows `MediaTranscoder` (WinRT, available on Windows 10 and later) produces a
byte-level-compatible bitstream: same `WAVEFORMATEX` fields (`wFormatTag` = 0x0161,
`nBlockAlign` = 5945, `nAvgBytesPerSec` = 16002) and the same super-frame layout.

### ASF packet surgery

After encoding, each new track's ASF packets are 5974 bytes (MediaTranscoder's default),
while stock packets are 5976 bytes. `_resize_packet()` adjusts the `padding_length` field
inside the packet header and trims or pads the trailing zero bytes accordingly, without
touching any audio payload.

The three patched fields per packet (`send_time`, `object_number`, `presentation_time`)
are all in the fixed-layout single-payload packet header; their byte offsets are derived
by walking the LTF / property-flags bytes at the start of each packet.

### Exe patches (7 sites)

The 25-track limit is enforced at seven independent locations in `Juiced.exe`, each storing
the count as a single-byte x86 immediate. All seven are patched atomically (validate all →
backup → write all) and `patch_exe.py` can be run standalone:

```bat
python patch_exe.py --status    # show current values
python patch_exe.py --max 30    # raise to 30 tracks
python patch_exe.py --restore   # restore from Juiced.exe.bak
```

| Offset | Instruction | Role |
|--------|-------------|------|
| `0x00BE4667` | `CMP eax, N` | Advance-track bounds check |
| `0x00BD35C5` | `MOV ecx, N` | Audio-handle array zero-init |
| `0x000AB4EA` | `MOV eax, N` | Track-load index clamp |
| `0x000AB9D3` | `MOV ebp, N` | Conditional clamp limit |
| `0x00029365` | `CMP [esi+4], N` | Audio-event queue full check |
| `0x0002936B` | `MOV [esi+4], N−1` | Queue wrap-around value |
| `0x000AA72C` | `CMP [ebp+0x3EC], N` | Last-track / loop-around detection |

## File structure

```
JuicedJukebox/
├── inject.py           CLI entry point — orchestrates the full inject / restore flow
├── bankfmt.py          music.dat codec (decode ↔ encode, round-trip verified)
├── wma.py              ASF binary surgery (packet parsing, timestamp patching, append)
├── patch_exe.py        Juiced.exe patcher — lifts the 25-track cap at 7 call sites
├── wma_transcode.ps1   Windows PowerShell 5.1 wrapper for the WinRT MediaTranscoder
└── input/              Drop your audio files here (artist - title.ext)
```

## Caveats

- **Windows only.** The WinRT `MediaTranscoder` requires Windows 10 or later and is
  invoked via `powershell.exe` (Windows PowerShell 5.1). PowerShell 7 (`pwsh`) dropped
  the WinRT type-loading mechanism and will not work.
- **Re-run semantics.** Running `inject.py` twice replaces the previously appended tracks
  rather than appending again. Edit `input/` and re-run to change the set.
- **Filename format.** Files must be named `{artist} - {title}.{ext}`. Files that don't
  match are skipped with a warning.
- **One transcode generation.** Lossy sources (MP3, OGG, M4A) are re-encoded to WMA;
  lossless (FLAC, WAV) go through one encode. Stock tracks are never re-encoded.
- **Verified on the 2005 retail release** (unpatched binary). Other regional builds may
  have different exe offsets; `patch_exe.py --status` will report prefix mismatches if so.

## License

MIT
