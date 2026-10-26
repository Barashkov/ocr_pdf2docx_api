"""
REST API (FastAPI) над OCR_pdf2docx: принимает PDF, возвращает DOCX.

Автоматически предоставляет OpenAPI-спецификацию:
  - /openapi.json  - машинночитаемая спека
  - /docs          - Swagger UI
  - /redoc         - ReDoc

Запуск локально:
  uvicorn api:app --host 0.0.0.0 --port 8000
"""

import asyncio
import io
import os
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

# ocr.py - скрипт конвертации из репозитория OCR_pdf2docx
# (в docker-образе он лежит в /app, клонируется на этапе сборки)
import ocr

MAX_UPLOAD_BYTES = int(os.environ.get("OCR_API_MAX_UPLOAD_MB", "100")) * 1024 * 1024

# Ссылка на скачанные файлы: file_id -> путь во временном каталоге
_downloaded_files: dict = {}

# Конвертация ресурсоёмкая (Docling + EasyOCR), поэтому отдаём
# тяжёлые задачи строго последовательно через семафор.
_convert_semaphore = asyncio.Semaphore(int(os.environ.get("OCR_API_MAX_CONCURRENCY", "1")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Прогреваем модели при старте, чтобы первый запрос не был самым долгим.
    langs = _default_langs()
    try:
        app.state.converter = ocr.build_converter(langs, force_full_page_ocr=False)
        app.state.ready = True
    except Exception as e:  # pragma: no cover
        app.state.converter = None
        app.state.ready = False
        app.state.warmup_error = str(e)
    yield
    # Убираем за собой временные файлы
    for path in _downloaded_files.values():
        try:
            os.remove(path)
        except OSError:
            pass
    _downloaded_files.clear()


def _default_langs() -> List[str]:
    env = os.environ.get("OCR_API_LANGS", "").strip()
    if env:
        return env.split()
    return ["en", "vi"]


def _safe_filename(name: str, default: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[^A-Za-z0-9._\-]+", "_", name).strip("._")
    return name or default


app = FastAPI(
    title="OCR PDF to DOCX API",
    version="1.0.0",
    description=(
        "REST API для конвертации PDF в редактируемый DOCX на базе "
        "[OCR_pdf2docx](https://github.com/phuongbigbig/OCR_pdf2docx): "
        "Docling (layout + TableFormer) + EasyOCR.\n\n"
        "Эндпоинты:\n"
        "- `POST /convert` — загрузка PDF, ответ — готовый .docx (binary).\n"
        "- `POST /convert/async` — фоновая конвертация, polling статуса через `GET /tasks/{id}`.\n"
        "- `GET /tasks/{id}` — статус/результат асинхронной задачи.\n"
        "- `GET /files/{id}` — скачать результат асинхронной задачи.\n"
        "- `GET /health` — проверка готовности (модели загружены)."
    ),
    lifespan=lifespan,
)

# ----------------------------- Схемы ответов ------------------------------

RESPONSE_422 = {
    "description": "Некорректный входной файл или параметры",
    "content": {"application/json": {"example": {"detail": "Invalid page range format"}}},
}

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

CONVERT_RESPONSE_DESC = {
    200: {
        "description": "Сконвертированный документ DOCX",
        "content": {DOCX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
    },
    413: {"description": "Файл превышает максимальный размер"},
    422: RESPONSE_422,
    500: {"description": "Ошибка конвейера конвертации"},
}

TASK_SCHEMA_EXAMPLE = {
    "task_id": "9b2f6a1e-1234-5678-9abc-def012345678",
    "status": "completed",
    "output_filename": "report.docx",
    "download_url": "/files/9b2f6a1e-1234-5678-9abc-def012345678",
    "error": None,
}


def _validate_pages(pages: Optional[str]) -> None:
    """Проверяет формат диапазона страниц ('3' или '2-5') до тяжёлой работы."""
    if pages is None:
        return
    if not re.fullmatch(r"\s*\d+\s*(-\s*\d+\s*)?", pages):
        raise HTTPException(
            status_code=422,
            detail="Invalid page range format. Use 'N' or 'START-END', e.g. '3' or '2-5'.",
        )
    parts = [int(p) for p in re.findall(r"\d+", pages)]
    if len(parts) == 2 and parts[0] > parts[1]:
        raise HTTPException(status_code=422, detail="Page range start must be <= end.")


def _read_pdf_upload(file: UploadFile) -> bytes:
    if file.content_type not in (None, "application/pdf", "application/x-pdf"):
        raise HTTPException(
            status_code=422,
            detail=f"Expected a PDF file, got content-type '{file.content_type}'.",
        )
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the maximum upload size of {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="The uploaded file is not a valid PDF.")
    return data


def _run_conversion_sync(
    pdf_bytes: bytes,
    output_name: str,
    languages: List[str],
    pages: Optional[str],
    scanned: bool,
) -> bytes:
    """Синхронная конвертация PDF -> DOCX (вызывается в worker-потоке)."""
    with tempfile.TemporaryDirectory(prefix="ocr_api_") as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        docx_path = os.path.join(tmpdir, "output.docx")

        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        total_pages = None
        try:
            import fitz

            d = fitz.open(pdf_path)
            total_pages = len(d)
            d.close()
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Cannot read PDF structure: {e}")

        page_range = ocr.parse_pages_to_range_tuple(pages, total_pages) if pages else None

        converter = ocr.build_converter(languages, force_full_page_ocr=scanned)
        convert_kwargs = {}
        if page_range:
            convert_kwargs["page_range"] = page_range

        result = converter.convert(pdf_path, **convert_kwargs)
        docling_doc = result.document

        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches
        from docling_core.types.doc import (
            TableItem,
            TextItem,
            SectionHeaderItem,
            ListItem,
            PictureItem,
        )

        doc = Document()

        for item, level in docling_doc.iterate_items():
            if isinstance(item, TableItem):
                ocr.build_docx_table(doc, item, docling_doc)
            elif isinstance(item, SectionHeaderItem):
                heading_text = item.text.strip()
                if heading_text:
                    heading_level = max(1, min(getattr(item, "level", level), 9))
                    doc.add_heading(heading_text, level=heading_level)
            elif isinstance(item, ListItem):
                list_text = item.text.strip()
                if list_text:
                    try:
                        doc.add_paragraph(list_text, style="List Bullet")
                    except KeyError:
                        doc.add_paragraph(f"• {list_text}")
            elif isinstance(item, TextItem):
                text_content = item.text.strip()
                if text_content:
                    doc.add_paragraph(text_content)
            elif isinstance(item, PictureItem):
                try:
                    pil_image = item.get_image(docling_doc)
                    if pil_image is not None:
                        img_buffer = io.BytesIO()
                        pil_image.save(img_buffer, format="PNG")
                        img_buffer.seek(0)
                        doc.add_picture(img_buffer, width=Inches(5.0))
                        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                except Exception:
                    caption = getattr(item, "caption", None)
                    if caption:
                        doc.add_paragraph(f"[Figure: {caption}]")

        doc.save(docx_path)
        with open(docx_path, "rb") as f:
            return f.read()


def _docx_response(docx_bytes: bytes, filename: str) -> StreamingResponse:
    buf = io.BytesIO(docx_bytes)
    return StreamingResponse(
        buf,
        media_type=DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------ Эндпоинты ---------------------------------


@app.get("/health", tags=["system"], summary="Готовность сервиса")
async def health():
    ready = getattr(app.state, "ready", False)
    body = {
        "status": "ok" if ready else "warming_up",
        "models_ready": ready,
        "default_languages": _default_langs(),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
    }
    if not ready:
        return JSONResponse(status_code=503, content=body)
    return body


@app.post(
    "/convert",
    tags=["conversion"],
    summary="Конвертировать PDF в DOCX (синхронно)",
    responses=CONVERT_RESPONSE_DESC,
)
async def convert(
    file: UploadFile = File(..., description="Входной PDF-файл"),
    lang: Optional[List[str]] = Form(
        None,
        description=(
            "Коды языков OCR (EasyOCR), например `vi`, `en`, `ru`. "
            "Можно несколько раз: `lang=en&lang=vi`. По умолчанию: en vi."
        ),
    ),
    pages: Optional[str] = Form(
        None,
        description="Диапазон страниц, например `3` или `2-5`. По умолчанию — все страницы.",
    ),
    scanned: bool = Form(
        False,
        description="Принудительный полностраничный OCR (для сканов/изображений).",
    ),
):
    """
    Принимает PDF, прогоняет его через Docling + EasyOCR и возвращает
    файл DOCX со восстановленными заголовками, списками, картинками
    и нативными таблицами Word.

    Ответ — бинарный файл `application/vnd.openxmlformats-officedocument.wordprocessingml.document`.
    """
    _validate_pages(pages)
    pdf_bytes = _read_pdf_upload(file)
    languages = lang if lang else _default_langs()
    base = _safe_filename(os.path.splitext(file.filename or "")[0], "converted")
    out_name = f"{base}.docx"

    try:
        async with _convert_semaphore:
            docx_bytes = await asyncio.to_thread(
                _run_conversion_sync, pdf_bytes, out_name, languages, pages, scanned
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Conversion failed: {e}")

    return _docx_response(docx_bytes, out_name)


# ------------------------- Асинхронные задачи -----------------------------

_tasks: dict = {}


async def _process_task(task_id: str, pdf_bytes: bytes, out_name: str,
                       languages: List[str], pages: Optional[str], scanned: bool):
    task = _tasks[task_id]
    task["status"] = "processing"
    try:
        async with _convert_semaphore:
            docx_bytes = await asyncio.to_thread(
                _run_conversion_sync, pdf_bytes, out_name, languages, pages, scanned
            )
        fd, tmp_path = tempfile.mkstemp(suffix=".docx", prefix="ocr_api_out_")
        with os.fdopen(fd, "wb") as f:
            f.write(docx_bytes)
        _downloaded_files[task_id] = tmp_path
        task["status"] = "completed"
        task["output_filename"] = out_name
        task["download_url"] = f"/files/{task_id}"
    except HTTPException as e:
        task["status"] = "failed"
        task["error"] = str(e.detail)
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)


@app.post(
    "/convert/async",
    tags=["conversion"],
    status_code=202,
    summary="Конвертировать PDF в DOCX (асинхронно)",
    responses={
        202: {
            "description": "Задача принята в обработку",
            "content": {"application/json": {"example": TASK_SCHEMA_EXAMPLE}},
        },
        413: {"description": "Файл превышает максимальный размер"},
        422: RESPONSE_422,
    },
)
async def convert_async(
    file: UploadFile = File(..., description="Входной PDF-файл"),
    lang: Optional[List[str]] = Form(None, description="Коды языков OCR (см. /convert)."),
    pages: Optional[str] = Form(None, description="Диапазон страниц, напр. `2-5`."),
    scanned: bool = Form(False, description="Режим полного OCR для сканов."),
):
    """
    То же, что `/convert`, но конвертация выполняется в фоне.
    Возвращает `task_id`; статус можно опрашивать через `GET /tasks/{task_id}`,
    результат скачивать через `GET /files/{task_id}`.
    """
    _validate_pages(pages)
    pdf_bytes = _read_pdf_upload(file)
    languages = lang if lang else _default_langs()
    base = _safe_filename(os.path.splitext(file.filename or "")[0], "converted")
    out_name = f"{base}.docx"

    task_id = str(uuid.uuid4())
    _tasks[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "output_filename": None,
        "download_url": None,
        "error": None,
    }
    asyncio.create_task(_process_task(task_id, pdf_bytes, out_name, languages, pages, scanned))
    return _tasks[task_id]


@app.get(
    "/tasks/{task_id}",
    tags=["conversion"],
    summary="Статус асинхронной задачи конвертации",
    responses={
        200: {"description": "Информация о задаче",
              "content": {"application/json": {"example": TASK_SCHEMA_EXAMPLE}}},
        404: {"description": "Задача не найдена"},
    },
)
async def get_task(task_id: str):
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


@app.get(
    "/files/{file_id}",
    tags=["conversion"],
    summary="Скачать DOCX завершённой задачи",
    responses={
        200: CONVERT_RESPONSE_DESC[200],
        404: {"description": "Файл не найден или задача ещё не завершена"},
    },
)
async def download_file(file_id: str):
    path = _downloaded_files.get(file_id)
    task = _tasks.get(file_id, {})
    if path is None or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found or task not completed.")
    filename = task.get("output_filename") or "converted.docx"
    with open(path, "rb") as f:
        data = f.read()
    return _docx_response(data, filename)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
