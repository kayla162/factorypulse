FROM python:3.12-slim

# RapidOCR 用到的 OpenCV 需要這兩個系統函式庫
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只用 pyproject 安裝相依套件：改程式碼時這一層會沿用快取，不必重裝 OCR 等大型套件
COPY pyproject.toml README.md ./
RUN mkdir -p src/factorypulse && touch src/factorypulse/__init__.py \
 && pip install --no-cache-dir -e ".[mssql,ocr,llm,api]"

COPY src ./src
COPY sql ./sql
RUN useradd --create-home --uid 1000 fp && mkdir -p data outbox reports && chown -R fp /app
USER fp

EXPOSE 8000
CMD ["uvicorn", "factorypulse.api:app", "--host", "0.0.0.0", "--port", "8000"]
