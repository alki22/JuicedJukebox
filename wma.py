"""ffmpeg wrappers + ASF binary surgery for the Juiced DSB format.

The stock music.dsb is a single ASF/WMA container where all 25 tracks are one
continuous stream. The game seeks by send_time (milliseconds) to jump between
tracks, with per-track offsets in music.dat (size_units = seconds).

We encode new tracks with the SAME ASF packet size as the stock DSB, then
binary-append their packets (with timestamps shifted to continue from the stock
stream's end). This avoids ffmpeg re-packetizing the whole file (which would
compress all timestamps by ~2×), preserving stock track timestamps perfectly.
"""
from __future__ import annotations

import io
import struct
import shutil
import subprocess
from pathlib import Path

CODEC       = "wmav2"
BITRATE     = "128k"
SAMPLE_RATE = "44100"
CHANNELS    = "2"

# ASF GUIDs (little-endian byte order as they appear on disk)
ASF_HEADER_GUID      = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
ASF_DATA_GUID        = bytes.fromhex("3626b2758e66cf11a6d900aa0062ce6c")
ASF_FILE_PROPS_GUID  = bytes.fromhex("a1dcab8c47a9cf118ee400c00c205365")

# Offsets inside the File Properties Object payload (after GUID + 8-byte size)
_FP_FILE_SIZE    = 16   # u64
_FP_PLAY_DUR     = 40   # u64 (in 100-ns units)
_FP_DATA_PKTS    = 32   # u64
_FP_MIN_PKT      = 68   # u32
_FP_MAX_PKT      = 72   # u32

# Offset inside the Data Object header (after GUID + 8-byte size)
_DO_TOTAL_PKTS   = 24   # u64 (FileID 16 + TotalDataPackets 8)


def ffmpeg_path() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install from https://www.gyan.dev/ffmpeg/builds/ "
            "(ffmpeg-release-essentials.zip), add the bin/ folder to PATH."
        )
    return p


def ffprobe_path() -> str:
    p = shutil.which("ffprobe")
    if not p:
        raise RuntimeError("ffprobe not found on PATH (ships with ffmpeg).")
    return p


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout


def probe_duration_seconds(path: Path) -> float:
    out = _run([
        ffprobe_path(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return float(out.strip())


# ---------------------------------------------------------------------------
# ASF binary helpers
# ---------------------------------------------------------------------------

class _ASFView:
    """Lightweight read-only view into an ASF file's key fields."""

    def __init__(self, data: bytes) -> None:
        if not data.startswith(ASF_HEADER_GUID):
            raise ValueError("Not an ASF file")
        self.data = data

        # Header size
        self.header_size: int = struct.unpack_from("<Q", data, 16)[0]

        # File Properties Object (inside header)
        fp_off = data.find(ASF_FILE_PROPS_GUID, 0, self.header_size)
        if fp_off < 0:
            raise ValueError("File Properties Object not found")
        self._fp_off = fp_off
        fp_payload = fp_off + 24               # skip GUID(16) + size(8)
        self.file_size:     int = struct.unpack_from("<Q", data, fp_payload + _FP_FILE_SIZE)[0]
        self.play_dur_100ns:int = struct.unpack_from("<Q", data, fp_payload + _FP_PLAY_DUR)[0]
        self.data_pkts:     int = struct.unpack_from("<Q", data, fp_payload + _FP_DATA_PKTS)[0]
        self.min_pkt_size:  int = struct.unpack_from("<I", data, fp_payload + _FP_MIN_PKT)[0]
        self.max_pkt_size:  int = struct.unpack_from("<I", data, fp_payload + _FP_MAX_PKT)[0]
        if self.min_pkt_size != self.max_pkt_size or self.min_pkt_size == 0:
            raise ValueError(
                f"Only fixed-size ASF packets supported "
                f"(min={self.min_pkt_size}, max={self.max_pkt_size})"
            )
        self.pkt_size = self.min_pkt_size

        # Data Object
        do_off = data.find(ASF_DATA_GUID, self.header_size)
        if do_off < 0:
            raise ValueError("Data Object not found")
        self._do_off = do_off
        self.first_pkt_off = do_off + 50       # GUID(16)+size(8)+FileID(16)+TotalPkts(8)+Reserved(2)

    @property
    def last_send_time_ms(self) -> int:
        """send_time of the last packet in the data object (ms)."""
        last_idx = self.data_pkts - 1
        off = self.first_pkt_off + last_idx * self.pkt_size
        return _packet_send_time_ms(self.data[off : off + self.pkt_size])

    @property
    def end_time_ms(self) -> int:
        """Approximate stream-end time: last send_time + one packet duration."""
        last = self.last_send_time_ms
        # Packet duration = pkt_size / bytes_per_ms; cheaply estimated from two consecutive pkts.
        if self.data_pkts >= 2:
            off0 = self.first_pkt_off
            off1 = off0 + self.pkt_size
            t0 = _packet_send_time_ms(self.data[off0 : off0 + self.pkt_size])
            t1 = _packet_send_time_ms(self.data[off1 : off1 + self.pkt_size])
            pkt_dur = t1 - t0
        else:
            pkt_dur = 371  # fallback: stock Juiced packet duration
        return last + pkt_dur


def _read_len(flag: int, buf: bytes, off: int) -> tuple[int, int]:
    if flag == 0: return 0, off
    if flag == 1: return buf[off], off + 1
    if flag == 2: return struct.unpack_from("<H", buf, off)[0], off + 2
    return struct.unpack_from("<I", buf, off)[0], off + 4


def _packet_send_time_ms(pkt: bytes) -> int:
    off = 0
    first = pkt[off]
    if first & 0x80:
        ec_len = first & 0x0F
        off += 1 + ec_len
    ltf = pkt[off]; off += 1
    off += 1  # property flags
    _, off = _read_len((ltf >> 5) & 0x3, pkt, off)   # packet length
    _, off = _read_len((ltf >> 1) & 0x3, pkt, off)   # sequence
    _, off = _read_len((ltf >> 3) & 0x3, pkt, off)   # padding length
    return struct.unpack_from("<I", pkt, off)[0]


def _patch_send_time(pkt: bytes, new_ms: int) -> bytes:
    """Return a copy of pkt with send_time replaced by new_ms."""
    out = bytearray(pkt)
    off = 0
    first = pkt[off]
    if first & 0x80:
        ec_len = first & 0x0F
        off += 1 + ec_len
    ltf = pkt[off]; off += 1
    off += 1
    _, off = _read_len((ltf >> 5) & 0x3, pkt, off)
    _, off = _read_len((ltf >> 1) & 0x3, pkt, off)
    _, off = _read_len((ltf >> 3) & 0x3, pkt, off)
    struct.pack_into("<I", out, off, new_ms)
    return bytes(out)


def _parse_payload_field_offsets(pkt: bytes) -> tuple[int, int, int, int]:
    """Return (obj_num_off, pres_t_off, obj_num_val, pres_t_val) for a single-payload packet.

    Assumes standard single-payload layout: stream_number=1B, object_number=1B,
    object_offset=4B, then replicated_data where bytes [0:4]=obj_size, [4:8]=pres_t.
    """
    off = 0
    first = pkt[off]
    if first & 0x80:
        off += 1 + (first & 0x0F)
    ltf = pkt[off]; off += 1
    off += 1  # property_flags
    _, off = _read_len((ltf >> 5) & 0x3, pkt, off)  # packet_length
    _, off = _read_len((ltf >> 1) & 0x3, pkt, off)  # sequence_number
    _, off = _read_len((ltf >> 3) & 0x3, pkt, off)  # padding_length
    off += 4  # send_time
    off += 2  # duration
    off += 1  # stream_number (1 byte)
    obj_num_off = off
    obj_num_val = pkt[off]; off += 1
    off += 4  # object_offset (4 bytes)
    off += 1  # replicated_data_length byte
    pres_t_off = off + 4  # rep_data[0:4]=obj_size, rep_data[4:8]=pres_t
    pres_t_val = struct.unpack_from('<I', pkt, pres_t_off)[0]
    return obj_num_off, pres_t_off, obj_num_val, pres_t_val


def _resize_packet(pkt: bytes, target_size: int) -> bytes:
    """Resize an ASF packet to target_size by adjusting its padding_length field.

    ASF packets end with padding_length zero bytes.  We can grow or shrink the
    packet by changing that field and trimming/extending the tail accordingly.
    Only 1-byte and 2-byte padding_length encodings are supported (flag 1 or 2).
    """
    if len(pkt) == target_size:
        return pkt
    delta = target_size - len(pkt)
    out = bytearray(pkt)
    off = 0
    first = pkt[off]
    if first & 0x80:
        ec_len = first & 0x0F
        off += 1 + ec_len
    ltf_off = off
    ltf = pkt[off]; off += 1
    off += 1  # property flags
    _, off = _read_len((ltf >> 5) & 0x3, pkt, off)  # packet length
    _, off = _read_len((ltf >> 1) & 0x3, pkt, off)  # sequence
    pad_flag = (ltf >> 3) & 0x3
    pad_field_off = off
    old_pad, off = _read_len(pad_flag, pkt, off)
    new_pad = old_pad + delta
    if new_pad < 0:
        raise ValueError(f"Cannot shrink packet by {-delta}: not enough existing padding ({old_pad})")
    if pad_flag == 1:
        if new_pad > 0xFF:
            raise ValueError(f"New padding {new_pad} overflows 1-byte field")
        out[pad_field_off] = new_pad
    elif pad_flag == 2:
        struct.pack_into("<H", out, pad_field_off, new_pad)
    else:
        raise ValueError(f"Unsupported padding_length flag {pad_flag}")
    # Extend or trim the byte array
    if delta > 0:
        out.extend(b"\x00" * delta)
    else:
        del out[target_size:]
    return bytes(out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def asf_packet_size(path: Path) -> int:
    """Return the fixed packet size (bytes) used by an ASF file."""
    return _ASFView(path.read_bytes()).pkt_size


def asf_end_time_ms(path: Path) -> int:
    """Return the estimated end timestamp (ms) of the last packet."""
    return _ASFView(path.read_bytes()).end_time_ms


_PS_SCRIPT = Path(__file__).resolve().parent / "wma_transcode.ps1"


def _transcode_to_wma(src: Path, dst: Path) -> None:
    """Invoke the Windows WinRT MediaTranscoder (via Windows PowerShell 5.1)
    to produce a WMA v8 / WMA2 bitstream (fourcc 0x0161) at 128 kbps, 44.1 kHz,
    stereo — the same codec family the stock Juiced DSB was built with.

    The game's audio engine uses Windows' own WMF/DirectShow decoder, which is
    strict about codec_private_data.  ffmpeg's wmav2 encoder produces a
    different, incompatible bitstream flavor; Microsoft's native encoder gets
    us byte-identical WAVEFORMATEX fields (fmt, ba, abps) to stock and matching
    super-frame structure, differing only in the trailing version bytes.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not _PS_SCRIPT.exists():
        raise RuntimeError(f"missing helper script: {_PS_SCRIPT}")
    # Use powershell.exe (Windows PowerShell 5.1) explicitly; pwsh (7+) no
    # longer supports `-ContentType=WindowsRuntime` type loading.
    cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(_PS_SCRIPT),
        "-InPath", str(src),
        "-OutPath", str(dst),
        "-Subtype", "WMA8",
        "-Bitrate", "128000",
        "-SampleRate", "44100",
        "-Channels", "2",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(
            "WMA transcode failed (WinRT MediaTranscoder).\n"
            f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        )


def encode_to_wma(src: Path, dst: Path, packet_size: int = 0) -> None:
    """Re-encode any audio input to WMA2 128k/44.1kHz/stereo ASF using the
    Windows native encoder so the bitstream matches stock Juiced's format.

    packet_size is currently ignored — MediaTranscoder picks its own packet
    size (~5974 bytes) and produces consistent output; both stock rebuild and
    new tracks use the same encoder so their packets are mutually appendable.
    """
    _transcode_to_wma(src, dst)


def rebuild_dsb(src_dsb: Path, dst_dsb: Path, packet_size: int = 0) -> None:
    """Transcode the stock ASF/WMA container through Windows' MediaTranscoder.

    Why: the stock Juiced music.dsb was built by a Microsoft WMA encoder
    (codec_private_data ~ 008800000f00e55c0000, block_align=5945).  When new
    packets from a different encoder are appended into it, the game's WMF
    decoder — configured from the ASF header — silently rejects them.

    Fix: run the whole DSB through the same encoder we use for new tracks
    (WinRT MediaTranscoder, WMA8 subtype).  After this, all packets share one
    codec config that Windows' decoder natively supports, and new-track
    packets are byte-level compatible with the rebuilt stock packets.
    """
    _transcode_to_wma(src_dsb, dst_dsb)


def append_track_to_dsb(
    stock_dsb: Path,
    new_wma: Path,
    output: Path,
) -> tuple[int, int]:
    """Binary-append new_wma's packets into a copy of stock_dsb.

    The new track's send_time values are shifted so they begin exactly where
    the stock stream ends (stock_dsb.end_time_ms). The stock data is never
    re-encoded or re-packetized.

    Returns (new_track_start_ms, new_track_end_ms).
    """
    stock = _ASFView(stock_dsb.read_bytes())
    extra = _ASFView(new_wma.read_bytes())

    # Packet sizes may differ if the new track was encoded by a different muxer
    # (e.g. Windows MediaTranscoder produces 5974-byte packets vs stock's 5976).
    # _resize_packet adjusts padding_length inside each packet to compensate.

    offset_ms = stock.end_time_ms
    extra_pkt_count = extra.data_pkts
    extra_data = extra.data

    new_total_pkts  = stock.data_pkts + extra_pkt_count
    new_file_size   = stock.file_size + extra_pkt_count * stock.pkt_size
    # play_duration in 100-ns; add extra track duration
    extra_play_dur  = (extra.end_time_ms - _packet_send_time_ms(
                          extra.data[extra.first_pkt_off : extra.first_pkt_off + extra.pkt_size]
                      )) * 10_000   # ms → 100ns
    new_play_dur    = stock.play_dur_100ns + extra_play_dur

    # Extract obj_num and pres_t from stock's last packet so new packets continue
    # seamlessly.  WM ASF Reader discards packets whose obj_num or pres_t regress.
    last_stock_off = stock.first_pkt_off + (stock.data_pkts - 1) * stock.pkt_size
    last_stock_pkt = stock.data[last_stock_off : last_stock_off + stock.pkt_size]
    _, _, stock_last_obj_num, stock_last_pres_t = _parse_payload_field_offsets(last_stock_pkt)
    stock_last_send_ms = _packet_send_time_ms(last_stock_pkt)
    # Preroll = constant offset between pres_t and send_time in the stock stream.
    stock_preroll_ms = stock_last_pres_t - stock_last_send_ms

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as f:
        # 1. Stock header verbatim.
        f.write(stock.data[:stock.header_size])

        # 2. Patched Data Object header (GUID + size + FileID + TotalPkts + Reserved).
        do_hdr = bytearray(stock.data[stock._do_off : stock._do_off + 50])
        new_do_size = struct.unpack_from("<Q", do_hdr, 16)[0] + extra_pkt_count * stock.pkt_size
        struct.pack_into("<Q", do_hdr, 16, new_do_size)
        struct.pack_into("<Q", do_hdr, _DO_TOTAL_PKTS + 16, new_total_pkts)
        f.write(bytes(do_hdr))

        # 3. Stock packets verbatim.
        stock_pkt_blob_start = stock._do_off + 50
        stock_pkt_blob_end   = stock_pkt_blob_start + stock.data_pkts * stock.pkt_size
        f.write(stock.data[stock_pkt_blob_start : stock_pkt_blob_end])

        # 4. New packets: shift send_time, continue obj_num sequence, fix pres_t.
        for i in range(extra_pkt_count):
            pkt_off = extra.first_pkt_off + i * extra.pkt_size
            pkt = extra.data[pkt_off : pkt_off + extra.pkt_size]
            original_ms = _packet_send_time_ms(pkt)
            new_send_ms = original_ms + offset_ms
            pkt = _patch_send_time(pkt, new_send_ms)
            # Continue obj_num from where stock ended (wraps at 256).
            new_obj_num = (stock_last_obj_num + 1 + i) % 256
            # pres_t must advance with send_time using the same preroll offset.
            new_pres_t = new_send_ms + stock_preroll_ms
            obj_num_off, pres_t_off, _, _ = _parse_payload_field_offsets(pkt)
            out_pkt = bytearray(pkt)
            out_pkt[obj_num_off] = new_obj_num
            struct.pack_into('<I', out_pkt, pres_t_off, new_pres_t)
            pkt = bytes(out_pkt)
            if len(pkt) != stock.pkt_size:
                pkt = _resize_packet(pkt, stock.pkt_size)
            f.write(pkt)

    # Patch File Properties Object in the already-written header region.
    with open(output, "r+b") as f:
        fp_payload = stock._fp_off + 24
        f.seek(fp_payload + _FP_FILE_SIZE)
        f.write(struct.pack("<Q", new_file_size))
        f.seek(fp_payload + _FP_DATA_PKTS)
        f.write(struct.pack("<Q", new_total_pkts))
        f.seek(fp_payload + _FP_PLAY_DUR)
        f.write(struct.pack("<Q", new_play_dur))

    new_track_end_ms = offset_ms + extra.end_time_ms - _packet_send_time_ms(
        extra.data[extra.first_pkt_off : extra.first_pkt_off + extra.pkt_size]
    )
    return offset_ms, new_track_end_ms


def duration_units(seconds: float) -> int:
    """Convert real-valued seconds to the music.dat size_units integer (rounded)."""
    return max(1, int(round(seconds)))
