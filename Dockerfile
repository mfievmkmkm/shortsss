FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY shorts_bot.py .
ENV PYTHONUNBUFFERED=1 STATE_DIR=/app/state
CMD ["python3", "shorts_bot.py"]
