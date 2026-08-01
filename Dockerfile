FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 longwatch \
    && mkdir -p /app/data \
    && chown -R longwatch:longwatch /app/data
USER longwatch

EXPOSE 8765
CMD ["longwatch"]
