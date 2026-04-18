FROM python:3.12-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home appuser
WORKDIR /app
COPY --from=builder /build/dist/*.whl .
RUN pip install --no-cache-dir *.whl && rm *.whl
# Download required NLTK data at build time
RUN python -c "import nltk; nltk.download('averaged_perceptron_tagger_eng', quiet=True); nltk.download('punkt_tab', quiet=True)"
USER appuser
EXPOSE 8000
CMD ["uvicorn", "tts_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
