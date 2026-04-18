import asyncio
import io
import logging
import logging.config
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
