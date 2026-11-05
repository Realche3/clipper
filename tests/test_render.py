import json
import subprocess

import pytest

from clipper import config, render
from clipper.models import Clip, Job, JobStatus, Segment, Transcript, Word
from clipper.render import encode


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    """A 6 s 1280x720 testsrc video with a sine tone."""
    path = tmp_path_factory.mktemp("src") / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=s=1280x720:d=6:r=25",
            "-f", "lavfi", "-i", "sine=f=440:d=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


@pytest.fixture
def job(source, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    clips = [Clip(id="c01", start=1.0, end=4.0), Clip(id="c02", start=3.0, end=5.0)]
    return Job(
        id="j1",
        source_url="https://youtu.be/x",
        status=JobStatus.captioned,
        source_file=source,
        clips=clips,
    )


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def test_ffmpeg_args_seek_before_input_and_relative_output(tmp_path):
    args = encode.ffmpeg_args(tmp_path / "source.mp4", 12.5, 40.0, "c01.tmp.mp4")
    assert args.index("-ss") < args.index("-i")
    assert args[args.index("-ss") + 1] == "12.500" and args[args.index("-t") + 1] == "27.500"
    assert "loudnorm=I=-14:TP=-1.5" in args and "+faststart" in args
    assert args[-1] == "c01.tmp.mp4"


def test_render_makes_vertical_30fps_clips_with_audio(job):
    job = render.run(job)

    assert job.status == JobStatus.rendered
    info = probe(job.clip("c01").file)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264" and video["r_frame_rate"] == "30/1"
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(3.0, abs=0.15)
    assert not list(job.clip("c01").file.parent.glob("*.tmp.mp4"))


def test_rerun_skips_and_clip_option_renders_one(job):
    job = render.run(job)
    c01, c02 = job.clip("c01").file, job.clip("c02").file
    t1, t2 = c01.stat().st_mtime_ns, c02.stat().st_mtime_ns

    render.run(job)
    assert c01.stat().st_mtime_ns == t1  # skipped
    render.run(job, force=True, clip_id="c02")
    assert c01.stat().st_mtime_ns == t1 and c02.stat().st_mtime_ns != t2

    with pytest.raises(ValueError, match="c09"):
        render.run(job, clip_id="c09")


def test_subtitles_and_hook_are_burned_from_a_relative_ass(job, tmp_path):
    transcript = Transcript(
        segments=[Segment(start=1.5, end=2.5, text="NO WAY", words=[
            Word(start=1.5, end=2.0, text="NO"), Word(start=2.0, end=2.5, text="WAY"),
        ])]
    )  # fmt: skip
    job.transcript_file = tmp_path / "transcript.json"
    job.transcript_file.write_text(transcript.model_dump_json(), encoding="utf-8")
    job.clips[0].hook_text = "Watch this"

    job = render.run(job)
    clips_dir = job.clip("c01").file.parent
    assert "NO" in (clips_dir / "c01.ass").read_text(encoding="utf-8")
    assert not (clips_dir / "c02.ass").exists()  # no words, no hook: no subtitles filter
    assert probe(job.clip("c01").file)["format"]["duration"]


def test_ffmpeg_args_add_subtitles_only_when_given(tmp_path):
    plain = encode.ffmpeg_args(tmp_path / "s.mp4", 0, 5, "o.mp4")
    subbed = encode.ffmpeg_args(tmp_path / "s.mp4", 0, 5, "o.mp4", "c01.ass")
    assert "subtitles" not in plain[plain.index("-filter_complex") + 1]
    assert subbed[subbed.index("-filter_complex") + 1].endswith(",subtitles=c01.ass[v]")


def test_ffmpeg_failure_raises_with_stderr(job, tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    job.source_file = bad
    with pytest.raises(encode.RenderError, match="c01"):
        render.run(job)
