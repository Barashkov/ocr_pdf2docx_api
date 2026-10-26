FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    poppler-utils \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN git clone https://github.com/phuongbigbig/OCR_pdf2docx.git . && \
    mv "ocr_table_rebuild.easyOCR..py" ocr.py && \
    rm "ocr_table_rebuild.ocrmac.py"

RUN pip install --no-cache-dir \
    docling \
    docling-core \
    python-docx \
    pymupdf \
    pandas \
    easyocr \
    fastapi \
    uvicorn \
    python-multipart

# 1) Модели Docling (layout, tableformer) из HuggingFace
RUN python -c "from docling.document_converter import DocumentConverter; DocumentConverter()"

# 2) МОДЕЛИ EasyOCR: детектор CRAFT + распознавание для en и vi.
#    EasyOCR скачивает их лениво при первом Reader(), поэтому греем кэш на этапе сборки.
#    Если будете запускать с другими языками (-l ru en), добавьте их коды в список ниже.
RUN python -c "import easyocr; easyocr.Reader(['en', 'vi'], gpu=False)"

RUN mkdir -p /data

# REST API (FastAPI + OpenAPI). api.py импортирует ocr.py из /app.
COPY api.py /app/api.py

ENV PYTHONPATH=/app \
    OCR_API_LANGS="en vi" \
    OCR_API_MAX_UPLOAD_MB=100 \
    OCR_API_MAX_CONCURRENCY=1

EXPOSE 8000

WORKDIR /data

# Сервер по умолчанию; для CLI-режима оригинального скрипта запускайте так:
#   docker run --rm -v "$PWD:/data" image python /app/ocr.py --no-open -i file.pdf
CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
