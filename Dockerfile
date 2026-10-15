# Debian 13 (trixie): FalkorDBLite's wheels need glibc 2.39 or newer.
FROM python:3.12-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
# The ready-made projects' snapshots (data/snapshots) load into embedded FalkorDB at startup.
CMD ["sh", "-c", "uvicorn lore.app:app --host 0.0.0.0 --port ${PORT}"]
