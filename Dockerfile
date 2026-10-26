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
    easyocr

# 1) Модели Docling (layout, tableformer) из HuggingFace
RUN python -c "from docling.document_converter import DocumentConverter; DocumentConverter()"

# 2) МОДЕЛИ EasyOCR: детектор CRAFT + распознавание для en и vi.
#    EasyOCR скачивает их лениво при первом Reader(), поэтому греем кэш на этапе сборки.
#    Если будете запускать с другими языками (-l ru en), добавьте их коды в список ниже.
RUN python -c "import easyocr; easyocr.Reader(['en', 'vi'], gpu=False)"

RUN mkdir -p /data
WORKDIR /data

ENTRYPOINT ["python", "/app/ocr.py", "--no-open"]
CMD ["--help"]
