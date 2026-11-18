import numpy as np
import pytest
import soundfile as sf

from jdx_backend.audio import render_timeline
from jdx_backend.audio import run_ffmpeg, video_proxy, normalize_import


@pytest.mark.asyncio
async def test_non_destructive_trim_move_gain_and_mute(tmp_path):
    source = tmp_path / "source.wav"
    rate = 48000
    sf.write(source, np.full((rate, 2), 0.2, dtype=np.float32), rate, subtype="FLOAT")
    original = source.read_bytes()
    clip = {"id": "one", "asset_id": "audio", "start_sample": rate // 2,
            "source_start_sample": 0, "source_end_sample": rate // 4, "gain_db": -6.0206,
            "fade_in_ms": 0, "fade_out_ms": 0, "muted": False, "stretch": 1}
    destination = tmp_path / "mix.wav"
    await render_timeline([clip, dict(clip, start_sample=0, muted=True)], {"audio": source}, destination, normalize=False)
    result, actual_rate = sf.read(destination, always_2d=True)
    assert actual_rate == rate
    assert abs(len(result) - rate * 0.75) < rate * 0.01
    assert np.max(np.abs(result[:rate//3])) < 1e-5
    assert 0.09 < np.mean(result[int(rate*0.56):int(rate*0.65)]) < 0.11
    assert source.read_bytes() == original


@pytest.mark.asyncio
async def test_limiter_compensates_delay_and_preserves_sample_position(tmp_path):
    source = tmp_path / "impulse.wav"
    samples = np.zeros((4800, 2), dtype=np.float32)
    samples[1200] = 0.1
    sf.write(source, samples, 48000, subtype="FLOAT")
    destination = tmp_path / "aligned.wav"
    clip = {"asset_id": "a", "start_sample": 2400, "source_start_sample": 0,
            "source_end_sample": 4800, "muted": False, "stretch": 1}
    await render_timeline([clip], {"a": source}, destination, normalize=False)
    audio, _ = sf.read(destination)
    assert np.argmax(np.abs(audio[:, 0])) == 3600
    assert len(audio) == 7200


@pytest.mark.asyncio
async def test_video_proxy_and_playlist_rejection(tmp_path):
    source = tmp_path / "synthetic.mp4"
    await run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25", "-t", "0.4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-n", str(source)])
    proxy = tmp_path / "proxy.mp4"
    result = await video_proxy(source, proxy)
    assert 350 <= result["duration_ms"] <= 450
    playlist = tmp_path / "unsafe.wav"
    playlist.write_text("#EXTM3U\nhttp://127.0.0.1:9/private\n")
    with pytest.raises(ValueError, match="媒体处理失败"):
        await normalize_import(playlist, tmp_path / "bad.wav")
