import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DISCORD_API_SECRET", "test-secret")
os.environ.setdefault("TTS_SOURCE_WAV", "assets/morshu.wav")

from tts_api.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-secret"}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["service"] == "discord-api-morshu"


def test_synthesize_requires_auth():
    r = client.post("/tts/synthesize", json={"text": "hello"})
    assert r.status_code == 403


def test_synthesize_text_too_long():
    r = client.post("/tts/synthesize", json={"text": "a" * 501}, headers=AUTH)
    assert r.status_code == 422


def test_phonemes_requires_auth():
    r = client.get("/tts/phonemes")
    assert r.status_code == 403
