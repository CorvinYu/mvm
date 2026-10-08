"""capture.py -- record what the system is actually playing (WASAPI loopback).

Why this is needed
------------------
Fine A/V alignment requires knowing what the user is *hearing*. SMTC gives us
metadata only (title/artist/position) -- no audio. So to cross-correlate the PV
against the real playback we must capture the system audio output.

Why WASAPI loopback rather than VB-CABLE:
    A virtual cable only carries audio that the player has been routed into.
    Measured here: capturing "CABLE Output" gave -91 dB (pure silence), because
    the user's music goes to their headphones. WASAPI loopback taps the actual
    output device, so it works with no reconfiguration of the user's setup and
    no risk of changing their audio routing.

Implementation note:
    PortAudio exposes WASAPI loopback only through a flag that the `sounddevice`
    Python binding does not surface (opening the output device as an input
    errors with "Invalid number of channels"). So we call WASAPI directly via
    ctypes/COM, which is verified to work in this environment.

This is the standard WASAPI capture dance:
    IMMDeviceEnumerator -> GetDefaultAudioEndpoint(eRender) -> IAudioClient
    -> Initialize(AUDCLNT_STREAMFLAGS_LOOPBACK) -> IAudioCaptureClient
    -> GetBuffer/ReleaseBuffer loop.
"""

from __future__ import annotations

import ctypes
import sys
import wave
from ctypes import POINTER, byref, c_void_p, c_uint32, c_uint64, c_int, c_float, c_byte
from ctypes.wintypes import DWORD, LPCWSTR, WORD
from dataclasses import dataclass
from pathlib import Path

# ---------------- COM plumbing ----------------

ole32 = ctypes.windll.ole32
kernel32 = ctypes.windll.kernel32


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __repr__(self) -> str:
        d4 = "".join(f"{b:02X}" for b in self.Data4)
        return f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-{d4[:4]}-{d4[4:]}}}"


def guid(d1, d2, d3, *d4) -> GUID:
    return GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


CLSID_MMDeviceEnumerator = guid(0xBCDE0395, 0xE52F, 0x467C, 0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E)
IID_IMMDeviceEnumerator = guid(0xA95664D2, 0x9614, 0x4F35, 0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6)
IID_IAudioClient = guid(0x1CB9AD4C, 0xDBFA, 0x4C32, 0xB1, 0x78, 0xC2, 0xF5, 0x68, 0xA7, 0x03, 0xB2)
IID_IAudioCaptureClient = guid(0xC8ADBD64, 0xE71E, 0x48A0, 0xA4, 0xDE, 0x18, 0x5C, 0x39, 0x5C, 0xD3, 0x17)

# eRender = 0 (output device), eConsole = 0 (role)
E_RENDER = 0
E_CONSOLE = 0

# AUDCLNT_STREAMFLAGS_LOOPBACK -- taps the render endpoint without muting it.
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

# WAVEFORMATEX
class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", WORD),
        ("nChannels", WORD),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD),
        ("wBitsPerSample", WORD),
        ("cbSize", WORD),
    ]


WAVEFORMATEXTENSIBLE_SUBFORMAT_PCM = guid(0x00000001, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71)
WAVEFORMATEXTENSIBLE_SUBFORMAT_FLOAT = guid(0x00000003, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71)


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("wValidBitsPerSample", WORD),
        ("dwChannelMask", DWORD),
        ("SubFormat", GUID),
    ]


# 100-ns units (REFERENCE_TIME)
REFTIMES_PER_SEC = 10_000_000


def _vtbl_call(ptr, index: int, restype, *argtypes):
    """Build a callable for a COM vtable slot."""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    func_addr = vtbl[index]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(func_addr)


class LoopbackRecorder:
    """Records the default render endpoint via WASAPI loopback."""

    def __init__(self) -> None:
        self._enumerator = None
        self._device = None
        self._client = None
        self._capture = None
        self.fmt: WAVEFORMATEX | None = None
        self._com_ready = False

    # ---------------- setup ----------------

    def open(self) -> bool:
        hr = ole32.CoInitializeEx(None, 0)   # STA
        self._com_ready = hr in (0, 1)

        p_enum = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(CLSID_MMDeviceEnumerator), None, 1,
            byref(IID_IMMDeviceEnumerator), byref(p_enum),
        )
        if hr != 0:
            return False
        self._enumerator = p_enum

        # IMMDeviceEnumerator::GetDefaultAudioEndpoint (vtbl slot 4)
        GetDefaultAudioEndpoint = _vtbl_call(
            p_enum, 4, ctypes.HRESULT, c_int, c_int, POINTER(c_void_p)
        )
        p_dev = c_void_p()
        if GetDefaultAudioEndpoint(p_enum, E_RENDER, E_CONSOLE, byref(p_dev)) != 0:
            return False
        self._device = p_dev

        # IMMDevice::Activate (vtbl slot 3)
        Activate = _vtbl_call(
            p_dev, 3, ctypes.HRESULT, POINTER(GUID), DWORD, c_void_p, POINTER(c_void_p)
        )
        p_client = c_void_p()
        if Activate(p_dev, byref(IID_IAudioClient), 1, None, byref(p_client)) != 0:
            return False
        self._client = p_client

        # IAudioClient::GetMixFormat (vtbl slot 8)
        GetMixFormat = _vtbl_call(p_client, 8, ctypes.HRESULT, POINTER(c_void_p))
        p_fmt = c_void_p()
        if GetMixFormat(p_client, byref(p_fmt)) != 0:
            return False
        self._fmt_ptr = p_fmt
        self.fmt = ctypes.cast(p_fmt, POINTER(WAVEFORMATEX)).contents
        return True

    def start(self, buffer_sec: float = 0.2) -> bool:
        """Initialize the client in LOOPBACK mode and start capturing."""
        if self._client is None or self.fmt is None:
            return False

        # IAudioClient::Initialize (vtbl slot 3)
        Initialize = _vtbl_call(
            self._client, 3, ctypes.HRESULT,
            c_int, DWORD, c_uint64, c_uint64,
            c_void_p, POINTER(GUID),
        )
        hns = int(buffer_sec * REFTIMES_PER_SEC)
        if Initialize(
            self._client, AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            hns, 0, self._fmt_ptr, None,
        ) != 0:
            return False

        # IAudioClient::GetService (vtbl slot 14)
        GetService = _vtbl_call(
            self._client, 14, ctypes.HRESULT, POINTER(GUID), POINTER(c_void_p)
        )
        p_cap = c_void_p()
        if GetService(self._client, byref(IID_IAudioCaptureClient), byref(p_cap)) != 0:
            return False
        self._capture = p_cap

        # IAudioClient::Start (vtbl slot 10)
        Start = _vtbl_call(self._client, 10, ctypes.HRESULT)
        return Start(self._client) == 0

    # ---------------- reading ----------------

    def read_chunk(self) -> bytes | None:
        """Return raw bytes for whatever is currently buffered (may be empty)."""
        if self._capture is None or self.fmt is None:
            return None

        # IAudioCaptureClient::GetNextPacketSize (slot 5)
        GetNextPacketSize = _vtbl_call(self._capture, 5, ctypes.HRESULT, POINTER(c_uint32))
        # GetBuffer (slot 3)
        GetBuffer = _vtbl_call(
            self._capture, 3, ctypes.HRESULT,
            POINTER(c_void_p), POINTER(c_uint32), POINTER(DWORD), POINTER(c_uint64), POINTER(c_uint64),
        )
        # ReleaseBuffer (slot 4)
        ReleaseBuffer = _vtbl_call(self._capture, 4, ctypes.HRESULT, c_uint32)

        out = bytearray()
        for _ in range(64):      # bounded, so a stall cannot spin forever
            n = c_uint32()
            if GetNextPacketSize(self._capture, byref(n)) != 0 or n.value == 0:
                break
            p_data = c_void_p()
            frames = c_uint32()
            flags = DWORD()
            devpos = c_uint64()
            qpcpos = c_uint64()
            if GetBuffer(
                self._capture, byref(p_data), byref(frames), byref(flags),
                byref(devpos), byref(qpcpos),
            ) != 0:
                break
            nbytes = frames.value * self.fmt.nBlockAlign
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                out.extend(b"\x00" * nbytes)
            elif p_data and nbytes:
                out.extend(ctypes.string_at(p_data, nbytes))
            ReleaseBuffer(self._capture, frames.value)
        return bytes(out)

    def close(self) -> None:
        try:
            if self._client is not None:
                Stop = _vtbl_call(self._client, 11, ctypes.HRESULT)
                Stop(self._client)
        except Exception:  # noqa: BLE001
            pass
        for attr in ("_capture", "_client", "_device", "_enumerator"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    release = _vtbl_call(obj, 2, ctypes.c_ulong)
                    release(obj)
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)
        if self._com_ready:
            ole32.CoUninitialize()

    # ---------------- helpers ----------------

    def format_info(self) -> dict:
        if self.fmt is None:
            return {}
        tag = self.fmt.wFormatTag
        is_float = tag == WAVE_FORMAT_IEEE_FLOAT
        if tag == WAVE_FORMAT_EXTENSIBLE:
            ext = ctypes.cast(self._fmt_ptr, POINTER(WAVEFORMATEXTENSIBLE)).contents
            is_float = bytes(ext.SubFormat) == bytes(WAVEFORMATEXTENSIBLE_SUBFORMAT_FLOAT)
        return {
            "channels": self.fmt.nChannels,
            "rate": self.fmt.nSamplesPerSec,
            "bits": self.fmt.wBitsPerSample,
            "float": is_float,
            "block_align": self.fmt.nBlockAlign,
        }


@dataclass(frozen=True)
class Recording:
    """A finished loopback capture, with the WALL-CLOCK interval it covers.

    WHY THIS TYPE EXISTS (2026-10-07, session 8)
    --------------------------------------------
    The old `record()` returned only a path, so callers had to ASSUME how much
    audio it held -- follow.py used the nominal constant `ALIGN_CAPTURE_SEC`
    (10.0s) in the absolute alignment formula

        target = position_in_pv + ALIGN_CAPTURE_SEC + elapsed_after_capture

    That assumption is wrong in two independent ways, and both shift the seek:

      1. The record loop is bounded by a WALL-CLOCK deadline
         (`deadline = time.time() + seconds`), so the number of samples that
         actually arrive depends on scheduling, the audio engine's period and
         how much the decoder/resampler gets to run. The captured audio is
         therefore routinely a little SHORTER or LONGER than `seconds`.
      2. `record()` keeps working AFTER the last sample was captured: the
         decode + mono downmix + resample + WAV write (`_finalise_samples`)
         take real time and produce NO audio. The previous code stamped
         `t_end = time.monotonic()` only after `record()` RETURNED
         (follow.py:707), so that tail was counted as "audio played here" and
         the computed target was too far ahead.

    So we now measure and return both facts instead of guessing:
        * `covered_sec` -- the audio duration actually written to the file
          (frames / rate). This is what the alignment formula must add.
        * `t_audio_start` / `t_audio_end` -- monotonic timestamps bracketing the
          live-audio window: start = just before the first chunk was pulled,
          end   = when the last chunk arrived. `t_audio_end` is LATER than
          `t_audio_start + covered_sec` only by the small sleep gaps between
          empty reads; the decode/resample tail is excluded by construction.
    """

    path: Path
    covered_sec: float
    t_audio_start: float
    t_audio_end: float
    requested_sec: float
    frames: int
    sample_rate: int

    def __fspath__(self) -> str:
        """Behave like the Path the old API returned.

        `record()` used to return a bare Path, and callers outside this task's
        write scope (align_probe.py) still pass the result straight into
        `wave.open()` / `ffprobe`-style helpers. Implementing os.PathLike keeps
        those working unchanged while new callers get the timing facts.
        `str(rec)` works too, so log lines that interpolate the result stay
        readable.
        """
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    @property
    def slack_sec(self) -> float:
        """How far the nominal request was from what we really captured.

        Positive = we captured MORE than asked. Diagnostics only; nothing in
        the alignment math reads it, because the math no longer uses the
        nominal value at all.
        """
        return self.covered_sec - self.requested_sec


def _finalise_samples(raw: bytearray, info: dict, target_rate: int,
                      t_audio_start: float, t_audio_end: float,
                      requested_sec: float, path: Path) -> Recording | None:
    """Decode/downmix/resample `raw` and write the mono WAV, then describe it.

    Split out of `record()` so the timing fields are computed from the SAMPLE
    COUNT (`raw`) and the audio-window timestamps -- never from the nominal
    `seconds` argument. See the `Recording` docstring for why that matters.
    """
    import struct

    rate = info.get("rate", 48000)
    channels = info.get("channels", 2)
    is_float = info.get("float", True)
    bits = info.get("bits", 32)

    # Decode to float mono.
    samples: list[float] = []
    if is_float and bits == 32:
        count = len(raw) // 4
        vals = struct.unpack(f"<{count}f", bytes(raw[: count * 4]))
    elif bits == 16:
        count = len(raw) // 2
        vals = tuple(v / 32768.0 for v in struct.unpack(f"<{count}h", bytes(raw[: count * 2])))
    else:
        # Unsupported sample format: refuse rather than emit garbage.
        return None

    # Downmix to mono.
    for i in range(0, len(vals) - channels + 1, channels):
        samples.append(sum(vals[i:i + channels]) / channels)

    # Naive resample to target_rate (adequate for correlation).
    if rate != target_rate and samples:
        ratio = rate / target_rate
        n_out = int(len(samples) / ratio)
        resampled = [samples[min(int(i * ratio), len(samples) - 1)] for i in range(n_out)]
        samples = resampled
        rate = target_rate

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", max(-32768, min(32767, int(s * 32767)))) for s in samples
        ))

    frames = len(samples)
    return Recording(
        path=path,
        # The ONE number the alignment formula needs. Derived from frames, so
        # it describes the file itself rather than our intention.
        covered_sec=frames / rate if rate else 0.0,
        t_audio_start=t_audio_start,
        t_audio_end=t_audio_end,
        requested_sec=requested_sec,
        frames=frames,
        sample_rate=rate,
    )


def record(path: Path, seconds: float = 12.0, target_rate: int = 16000) -> Recording | None:
    """Record system output to a mono 16-bit WAV at `target_rate`.

    Returns a `Recording` (path + the wall-clock interval the audio REALLY
    covers), or None if capture is unavailable.

    The timestamps are what makes fine alignment honest: the caller can compute
    "how long ago did this audio play" from `t_audio_end` instead of assuming it
    equals "when record() returned".
    """
    import time

    rec = LoopbackRecorder()
    if not rec.open():
        return None
    info = rec.format_info()
    if not rec.start():
        rec.close()
        return None

    raw = bytearray()
    # Timestamps bracket the LIVE audio only. `t_audio_start` is taken just
    # after the client started and before the first read, `t_audio_end` at the
    # last iteration that produced samples -- so the decode/resample/write tail
    # below is NOT inside [start, end] and cannot be mistaken for audio time.
    t_audio_start = time.monotonic()
    t_audio_end = t_audio_start
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            chunk = rec.read_chunk()
            if chunk:
                raw.extend(chunk)
                t_audio_end = time.monotonic()
            else:
                time.sleep(0.02)
    finally:
        rec.close()

    if not raw:
        return None

    return _finalise_samples(
        raw, info, target_rate, t_audio_start, t_audio_end, float(seconds), path
    )


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("state/_capture.wav")

    rec = LoopbackRecorder()
    if not rec.open():
        print("FAIL: could not open default render endpoint")
        raise SystemExit(1)
    print("format:", rec.format_info())
    if not rec.start():
        print("FAIL: could not start loopback capture")
        raise SystemExit(1)
    print(f"recording {secs}s ...")

    import struct, time
    raw = bytearray()
    deadline = time.time() + secs
    while time.time() < deadline:
        c = rec.read_chunk()
        if c:
            raw.extend(c)
        else:
            time.sleep(0.02)
    rec.close()

    info = rec.format_info()
    print(f"captured {len(raw)} bytes")
    if raw and info.get("float"):
        count = len(raw) // 4
        vals = struct.unpack(f"<{count}f", bytes(raw[:count*4]))
        peak = max((abs(v) for v in vals), default=0.0)
        print(f"peak amplitude: {peak:.5f}  -> {'HAS SIGNAL' if peak > 0.001 else 'SILENT'}")
    print(f"note: raw bytes are {'non-empty' if raw else 'EMPTY'}")
