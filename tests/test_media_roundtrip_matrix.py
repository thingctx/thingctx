"""Property-style round-trip matrix for the media plane: generate synthetic
clips with ffmpeg across the axes that break re-encoders (frame rate, VFR, audio
rate, single-track), run a ``frames() -> publish()`` round trip, and assert the
timing invariants on the written file with ffprobe.

The invariant the media plane must hold for an ingest: a round trip preserves the
source duration and A/V sync, keeps every frame, and preserves the source frame
rate (for VFR, where a single fps is ill-defined, duration is the invariant).

Network free and deterministic: every input is generated locally. Skipped when
ffmpeg/ffprobe or the codecs (av/numpy) are missing."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading

import pytest

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")

from thingctx.bindings.builtin.media import MediaBinding  # noqa: E402
from thingctx.bindings.builtin.media.backends import PyAVBackend  # noqa: E402
from thingctx.thing import WoTAction, WoTForm  # noqa: E402

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    not (_FFMPEG and _FFPROBE), reason="ffmpeg/ffprobe required for the round-trip matrix"
)

_ACTION = WoTAction(
    name="ingest",
    thing_id="urn:thingctx:studio:test",
    description="",
    input_schema={},
    output_schema=None,
    idempotent=False,
    forms=(),
)


def _gen(
    path,
    *,
    fps: str = "30",
    dur: float = 2.0,
    video: bool = True,
    audio_rate: int | None = None,
    vfr: bool = False,
) -> None:
    """Generate a synthetic clip. ``fps`` may be fractional (e.g. 30000/1001)."""
    args = [_FFMPEG, "-y", "-v", "error"]
    if video:
        args += ["-f", "lavfi", "-i", f"testsrc=size=320x240:rate={fps}:duration={dur}"]
    if audio_rate:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={audio_rate}:duration={dur}"]
    if video and vfr:
        # Drop every 5th frame so the timestamps are irregular (variable frame
        # rate); -vsync vfr keeps the real (non-uniform) pts rather than padding
        # back to CFR.
        args += ["-vf", r"select=not(eq(mod(n\,5)\,0))", "-vsync", "vfr"]
    if video:
        args += ["-pix_fmt", "yuv420p"]
    args += [str(path)]
    subprocess.check_call(args)


def _probe(path) -> dict:
    out = subprocess.check_output(
        [_FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    )
    return json.loads(out)


def _stream(info: dict, kind: str) -> dict | None:
    for s in info.get("streams", []):
        if s.get("codec_type") == kind:
            return s
    return None


def _fps(stream: dict) -> float:
    num, _, den = stream.get("r_frame_rate", "0/1").partition("/")
    den = den or "1"
    return float(num) / float(den) if float(den) else 0.0


def _roundtrip(src, out, *, video: bool, audio: bool) -> None:
    # The default (latest) backpressure is used on purpose: an ingest of a finite
    # source must keep every frame even under the live-streaming default.
    inv = MediaBinding(backends=[PyAVBackend()], max_queue=4)
    form = WoTForm(href=str(src), raw={"x-thingctx-media": {}})
    outform = WoTForm(href=str(out), raw={"x-thingctx-media": {}})

    async def run():
        if video and audio:
            await inv.publish(
                _ACTION,
                outform,
                inv.frames(_ACTION, form, track="video"),
                audio=inv.frames(_ACTION, form, track="audio"),
            )
        elif video:
            await inv.publish(_ACTION, outform, inv.frames(_ACTION, form, track="video"))
        else:
            await inv.publish(
                _ACTION, outform, inv.frames(_ACTION, form, track="audio"), track="audio"
            )

    asyncio.run(run())


# (fps argument to ffmpeg, expected output fps) across the axes that trip encoders.
_FPS_CASES = [
    ("24", 24.0),
    ("25", 25.0),
    ("30", 30.0),
    ("50", 50.0),
    ("60", 60.0),
    ("30000/1001", 29.97),  # 29.97 NTSC
    ("24000/1001", 23.976),  # 23.976 film
]


@pytest.mark.parametrize("fps_arg,expected_fps", _FPS_CASES)
@pytest.mark.parametrize("audio_rate", [None, 44100, 48000])
def test_roundtrip_preserves_fps_duration_and_sync(tmp_path, fps_arg, expected_fps, audio_rate):
    src = tmp_path / "in.mp4"
    out = tmp_path / "out.mp4"
    dur = 2.0
    _gen(src, fps=fps_arg, dur=dur, audio_rate=audio_rate)
    src_v = _stream(_probe(src), "video")
    src_nb = int(src_v["nb_frames"])

    _roundtrip(src, out, video=True, audio=audio_rate is not None)

    info = _probe(out)
    v = _stream(info, "video")
    assert v is not None
    out_fps = _fps(v)
    out_vdur = float(v["duration"])
    out_nb = int(v["nb_frames"])
    frame = 1.0 / expected_fps

    # Source frame rate is preserved (no resample to a fixed default).
    assert out_fps == pytest.approx(expected_fps, abs=1.0)
    # Duration is preserved within a frame.
    assert out_vdur == pytest.approx(dur, abs=max(0.1, 2 * frame))
    # Every frame survives the ingest (no shedding); fractional rates may round by one.
    assert abs(out_nb - src_nb) <= 1
    if audio_rate is not None:
        a = _stream(info, "audio")
        assert a is not None
        # A/V stay in sync: the two stream durations agree.
        assert float(a["duration"]) == pytest.approx(out_vdur, abs=max(0.15, 2 * frame))


@pytest.mark.parametrize(
    "fps_arg,expected_fps", [("24", 24.0), ("30000/1001", 29.97), ("60", 60.0)]
)
def test_roundtrip_very_short_preserves_duration_and_sync(tmp_path, fps_arg, expected_fps):
    # Very short clips (a handful of frames) are the case that trips a re-encoder's
    # pts/time-base bookkeeping: few frames, fractional rates, A/V both tiny. The
    # invariants still hold: duration, frame survival, and A/V sync.
    src = tmp_path / "short_in.mp4"
    out = tmp_path / "short_out.mp4"
    dur = 0.3
    _gen(src, fps=fps_arg, dur=dur, audio_rate=48000)
    src_v = _stream(_probe(src), "video")
    src_nb = int(src_v["nb_frames"])

    _roundtrip(src, out, video=True, audio=True)

    info = _probe(out)
    v = _stream(info, "video")
    a = _stream(info, "audio")
    assert v is not None and a is not None
    frame = 1.0 / expected_fps
    assert float(v["duration"]) == pytest.approx(dur, abs=max(0.1, 2 * frame))
    assert abs(int(v["nb_frames"]) - src_nb) <= 1
    assert float(a["duration"]) == pytest.approx(float(v["duration"]), abs=max(0.15, 2 * frame))


def test_roundtrip_vfr_preserves_duration(tmp_path):
    # Variable frame rate: a single output fps is ill-defined, so duration (and
    # frame survival) is the invariant, not a fixed rate.
    src = tmp_path / "vfr_in.mp4"
    out = tmp_path / "vfr_out.mp4"
    dur = 3.0
    _gen(src, fps="30", dur=dur, vfr=True)
    src_v = _stream(_probe(src), "video")
    src_nb = int(src_v["nb_frames"])
    src_dur = float(src_v["duration"])

    _roundtrip(src, out, video=True, audio=False)

    v = _stream(_probe(out), "video")
    assert v is not None
    assert float(v["duration"]) == pytest.approx(src_dur, abs=0.2)
    assert abs(int(v["nb_frames"]) - src_nb) <= 1


def test_roundtrip_audio_only_preserves_duration(tmp_path):
    src = tmp_path / "audio_in.m4a"
    out = tmp_path / "audio_out.m4a"
    dur = 2.0
    _gen(src, video=False, audio_rate=48000, dur=dur)

    _roundtrip(src, out, video=False, audio=True)

    a = _stream(_probe(out), "audio")
    assert a is not None
    assert float(a["duration"]) == pytest.approx(dur, abs=0.15)


def test_roundtrip_video_only_no_audio_track(tmp_path):
    src = tmp_path / "noaudio_in.mp4"
    out = tmp_path / "noaudio_out.mp4"
    _gen(src, fps="30", dur=2.0, audio_rate=None)

    _roundtrip(src, out, video=True, audio=False)

    info = _probe(out)
    assert _stream(info, "video") is not None
    assert _stream(info, "audio") is None


# ---- save() (stream copy): the same matrix, with the stronger bit-exact invariant ----


def _save_copy(src, out, *, track=None) -> None:
    inv = MediaBinding(backends=[PyAVBackend()], max_queue=4)
    form = WoTForm(href=str(src), raw={"x-thingctx-media": {}})
    asyncio.run(inv.save(_ACTION, form, str(out), track=track))


@pytest.mark.parametrize("fps_arg,expected_fps", _FPS_CASES)
@pytest.mark.parametrize("audio_rate", [None, 48000])
def test_save_is_bit_exact_across_matrix(tmp_path, fps_arg, expected_fps, audio_rate):
    src = tmp_path / "in.mp4"
    out = tmp_path / "out.mp4"
    _gen(src, fps=fps_arg, dur=2.0, audio_rate=audio_rate)
    si = _probe(src)
    sv = _stream(si, "video")

    _save_copy(src, out)

    oi = _probe(out)
    ov = _stream(oi, "video")
    assert ov is not None
    # Stream copy is bit exact: identical codec, exact frame-rate string (not a
    # rounded approximation), exact frame count.
    assert ov["codec_name"] == sv["codec_name"]
    assert ov["r_frame_rate"] == sv["r_frame_rate"]
    assert int(ov["nb_frames"]) == int(sv["nb_frames"])
    if audio_rate is not None:
        sa, oa = _stream(si, "audio"), _stream(oi, "audio")
        assert oa is not None
        assert oa["codec_name"] == sa["codec_name"]
        # A/V stay in sync.
        assert float(oa["duration"]) == pytest.approx(float(ov["duration"]), abs=0.1)


@pytest.mark.parametrize("dur", [0.3, 4.0])
def test_save_bit_exact_short_and_long(tmp_path, dur):
    src = tmp_path / "in.mp4"
    out = tmp_path / "out.mp4"
    _gen(src, fps="30", dur=dur, audio_rate=48000)
    sv = _stream(_probe(src), "video")

    _save_copy(src, out)

    ov = _stream(_probe(out), "video")
    assert ov["r_frame_rate"] == sv["r_frame_rate"]
    assert int(ov["nb_frames"]) == int(sv["nb_frames"])


def test_save_vfr_is_bit_exact(tmp_path):
    # Stream copy preserves VFR perfectly: the irregular timestamps and the exact
    # frame count carry across unchanged (no re-encode rate decision at all).
    src = tmp_path / "vfr.mp4"
    out = tmp_path / "vfr_out.mp4"
    _gen(src, fps="30", dur=3.0, vfr=True)
    sv = _stream(_probe(src), "video")

    _save_copy(src, out)

    ov = _stream(_probe(out), "video")
    assert int(ov["nb_frames"]) == int(sv["nb_frames"])
    assert float(ov["duration"]) == pytest.approx(float(sv["duration"]), abs=0.05)


def test_save_audio_only_and_video_only(tmp_path):
    a_src, a_out = tmp_path / "a_in.m4a", tmp_path / "a_out.m4a"
    _gen(a_src, video=False, audio_rate=48000, dur=1.0)
    _save_copy(a_src, a_out)
    assert _stream(_probe(a_out), "audio") is not None

    v_src, v_out = tmp_path / "v_in.mp4", tmp_path / "v_out.mp4"
    _gen(v_src, fps="30", dur=1.0, audio_rate=None)
    _save_copy(v_src, v_out)
    oi = _probe(v_out)
    assert _stream(oi, "video") is not None
    assert _stream(oi, "audio") is None


# ---- merged remux: two separate streams muxed into one (the DASH bestvideo+
# bestaudio shape), the same bit-exact invariant per stream, across the matrix ----


@pytest.mark.parametrize("fps_arg,expected_fps", _FPS_CASES)
def test_remux_merged_streams_bit_exact_across_matrix(tmp_path, fps_arg, expected_fps):
    # A merged source is a video-only stream and an audio-only stream muxed into
    # one container (what bestvideo+bestaudio resolves to). Local sources are
    # muxed straight through, so each stream stays bit exact and the two land in
    # sync. This guards the merged path the way the single-source matrix guards
    # save(): a gap once showed only on a second clip (60fps worked, shorts did
    # not), so the rate axis is exercised here too.
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.m4a"
    out = tmp_path / "merged.mp4"
    _gen(video, fps=fps_arg, dur=2.0, audio_rate=None)
    _gen(audio, video=False, audio_rate=48000, dur=2.0)
    sv = _stream(_probe(video), "video")
    sa = _stream(_probe(audio), "audio")

    PyAVBackend()._remux(
        [(str(video), None), (str(audio), None)],
        str(out),
        options={},
        stop=threading.Event(),
    )

    oi = _probe(out)
    ov = _stream(oi, "video")
    oa = _stream(oi, "audio")
    assert ov is not None and oa is not None
    # Each stream is copied bit exact: codec, exact frame-rate string, frame count.
    assert ov["codec_name"] == sv["codec_name"]
    assert ov["r_frame_rate"] == sv["r_frame_rate"]
    assert int(ov["nb_frames"]) == int(sv["nb_frames"])
    assert oa["codec_name"] == sa["codec_name"]
    # The two muxed streams agree in duration (A/V sync).
    assert float(oa["duration"]) == pytest.approx(float(ov["duration"]), abs=0.1)
