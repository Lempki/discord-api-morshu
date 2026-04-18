import asyncio
import io
import logging
import logging.config
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse

from .auth import require_auth
from .config import Settings, get_settings
from .models import HealthResponse, PhonemesResponse, SynthesizeRequest
from .morshutalk import Morshu, init
from .morshutalk.morshu import morshu_rec


def _configure_logging(level: str) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "formatters": {
                "json": {
                    "format": '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","message":"%(message)s"}'
                }
            },
            "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
            "root": {"level": level, "handlers": ["console"]},
        }
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    await asyncio.to_thread(init, settings.tts_source_wav)
    yield


app = FastAPI(title="discord-api-tts", version="1.0.0", lifespan=lifespan)


def _synthesize_blocking(text: str, speed: float, trim_silence: bool) -> bytes:
    m = Morshu()
    result = m.load_text(text)
    if result is False or len(result) == 0:
        return b""
    audio = result
    if speed != 1.0:
        audio = audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * speed)})
        audio = audio.set_frame_rate(result.frame_rate)
    if trim_silence:
        audio = audio.strip_silence()
    buf = io.BytesIO()
    audio.export(buf, format="wav")
    return buf.getvalue()


def _synthesize_video_blocking(text: str, source_mp4: str) -> bytes:
    m = Morshu()
    result = m.load_text(text)
    if result is False or len(result) == 0:
        return b""

    audio = result
    timings = m.audio_segment_timings
    n = len(timings)
    total_ms = len(audio)

    # Build list of (source_start_sec, source_end_sec) for each phoneme segment.
    # Silence segments (morshu == -1) are mapped to the source start so Morshu
    # shows a closed-mouth idle frame during pauses.
    segments: list[tuple[float, float]] = []
    for i in range(n):
        src_start = int(timings["morshu"][i])
        out_end = int(timings["output"][i + 1]) if i + 1 < n else total_ms
        duration_ms = out_end - int(timings["output"][i])
        if duration_ms <= 0:
            continue
        if src_start < 0:
            src_start = 0
        segments.append((src_start / 1000.0, (src_start + duration_ms) / 1000.0))

    if not segments:
        return b""

    wav_buf = io.BytesIO()
    audio.export(wav_buf, format="wav")
    wav_bytes = wav_buf.getvalue()

    tmp_files: list[str] = []
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            concat_path = f.name
            tmp_files.append(concat_path)
            for start_s, end_s in segments:
                f.write(f"file {source_mp4}\n")
                f.write(f"inpoint {start_s:.6f}\n")
                f.write(f"outpoint {end_s:.6f}\n")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            wav_path = f.name
            tmp_files.append(wav_path)

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            vid_path = f.name
            tmp_files.append(vid_path)

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            out_path = f.name
            tmp_files.append(out_path)

        # Step 1: stitch video segments from source using the concat demuxer
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_path,
                "-c:v", "libx264", "-preset", "fast",
                "-c:a", "aac",
                vid_path,
            ],
            capture_output=True, check=True, timeout=120,
        )

        # Step 2: replace the stitched audio track with the generated WAV
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", vid_path, "-i", wav_path,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac",
                "-shortest",
                out_path,
            ],
            capture_output=True, check=True, timeout=60,
        )

        with open(out_path, "rb") as f:
            return f.read()

    finally:
        for p in tmp_files:
            try:
                os.unlink(p)
            except OSError:
                pass


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="discord-api-tts", version="1.0.0")


@app.get("/tts/phonemes", response_model=PhonemesResponse, dependencies=[Depends(require_auth)])
async def phonemes(settings: Annotated[Settings, Depends(get_settings)]) -> PhonemesResponse:
    unique = sorted({p for p in morshu_rec["phoneme"].tolist() if p})
    return PhonemesResponse(phonemes=unique, source_wav=settings.tts_source_wav)


@app.post("/tts/synthesize", dependencies=[Depends(require_auth)])
async def synthesize(
    body: SynthesizeRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    if len(body.text) > settings.tts_max_text_length:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Text exceeds maximum length of {settings.tts_max_text_length} characters.",
        )

    if body.format == "video":
        if not os.path.isfile(settings.tts_source_mp4):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Video synthesis is not available: source MP4 not found. Configure TTS_SOURCE_MP4.",
            )
        mp4_bytes = await asyncio.to_thread(_synthesize_video_blocking, body.text, settings.tts_source_mp4)
        if not mp4_bytes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Could not generate video — no phoneme matches found for the given text.",
            )
        return StreamingResponse(
            io.BytesIO(mp4_bytes),
            media_type="video/mp4",
            headers={"Content-Disposition": 'attachment; filename="morshu.mp4"'},
        )

    wav_bytes = await asyncio.to_thread(_synthesize_blocking, body.text, body.speed, body.trim_silence)
    if not wav_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not generate audio — no phoneme matches found for the given text.",
        )
    return StreamingResponse(
        io.BytesIO(wav_bytes),
        media_type="audio/wav",
        headers={"Content-Disposition": 'attachment; filename="morshu.wav"'},
    )
