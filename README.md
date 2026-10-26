# OCR PDF → DOCX: Docker-образ + REST API (OpenAPI)

Готовый сервис для конвертации PDF в редактируемый DOCX на базе
[OCR_pdf2docx](https://github.com/phuongbigbig/OCR_pdf2docx):
**Docling** (layout-анализ + TableFormer для таблиц) + **EasyOCR** (распознавание текста,
включая сканы и вьетнамскую диакритику). Поверх CLI-скрипта поднят REST API на FastAPI
с автоматической OpenAPI-спецификацией, Swagger UI и фоновым режимом для больших документов.

```
PDF  ──►  POST /convert  ──►  Docling (layout + TableFormer)  ──►  python-docx  ──►  DOCX
                 ▲                                   + EasyOCR (для сканов)
```

В DOCX восстанавливаются: заголовки (Heading 1–9), списки, абзацы, картинки
(как изображения Word) и **нативные таблицы Word** (объединённые ячейки — текстом в клетках).

---

## 1. Быстрый старт (Docker)

Сборка занимает 15–30 минут (скачиваются модели Docling и EasyOCR ~2–4 ГБ):

```bash
git clone <адрес этого репозитория> && cd <репозиторий>

docker build -t ocr-pdf2docx-api .

docker run -d --name ocr-api \
  -p 8000:8000 \
  -e OCR_API_LANGS="en vi" \
  ocr-pdf2docx-api
```

Подождите, пока модели будут прогреты (первый запрос после старта может занять 1–3 минуты):

```bash
curl -s http://localhost:8000/health
# {"status":"ok","models_ready":true,"default_languages":["en","vi"],"max_upload_mb":100}
```

Откройте документацию:

| Ссылка | Что там |
|---|---|
| http://localhost:8000/docs | Swagger UI — можно прямо в браузере загрузить PDF и скачать DOCX |
| http://localhost:8000/redoc | ReDoc — читаемая версия документации |
| http://localhost:8000/openapi.json | Машинночитаемая OpenAPI-спека (для генерации клиентов) |

### Конвертация одной командой

```bash
curl -o result.docx -F "file=@document.pdf" http://localhost:8000/convert
```

---

## 2. Эндпоинты API

### `POST /convert` — синхронная конвертация

Принимает PDF (multipart/form-data), возвращает бинарный DOCX
(`application/vnd.openxmlformats-officedocument.wordprocessingml.document`).

| Поле формы | Тип | По умолчанию | Описание |
|---|---|---|---|
| `file` | файл (PDF) | — | **Обязательное.** Входной PDF. Проверяется сигнатура `%PDF` и content-type. |
| `lang` | строка, можно несколько раз | значения `OCR_API_LANGS` (`en vi`) | Коды языков EasyOCR: `en`, `vi`, `ru`, `fr`, `de`, `ja`, `zh` … Полный список — https://www.jaided.ai/easyocr/. Повторяйте поле: `-F "lang=en" -F "lang=ru"`. |
| `pages` | строка | все страницы | Диапазон страниц: `3` или `2-5`. Некорректный формат → `422`. |
| `scanned` | bool | `false` | Принудительный полностраничный OCR — включайте для отсканированных книг/фотографий документа, где нет текстового слоя. |

Примеры:

```bash
# обычный PDF, все страницы, языки по умолчанию
curl -o out.docx -F "file=@report.pdf" http://localhost:8000/convert

# скан на вьетнамском + английском, только страницы 2–5, полный OCR
curl -o out.docx \
  -F "file=@scan.pdf" \
  -F "lang=en" -F "lang=vi" \
  -F "pages=2-5" \
  -F "scanned=true" \
  http://localhost:8000/convert
```

Коды ответов: `200` (DOCX), `413` (файл больше лимита), `422` (не PDF / битый PDF /
некорректный `pages`), `500` (ошибка конвейера).

⚠️ Конвертация ресурсоёмкая (CPU/GPU, сотни МБ RAM на документ). По умолчанию задачи
выполняются строго последовательно (`OCR_API_MAX_CONCURRENCY=1`), остальные ждут своей
очереди. Для больших файлов используйте асинхронный режим ниже.

### Асинхронный режим (рекомендуется для больших документов)

**`POST /convert/async`** — принимает те же поля, что `/convert`, мгновенно возвращает `202`:

```json
{
  "task_id": "9b2f6a1e-1234-5678-9abc-def012345678",
  "status": "queued",
  "output_filename": null,
  "download_url": null,
  "error": null
}
```

**`GET /tasks/{task_id}`** — polling статуса: `queued` → `processing` → `completed` | `failed`
(при успехе содержит `download_url`, при ошибке — `error`).

**`GET /files/{file_id}`** — скачивает готовый DOCX завершённой задачи.

Полный цикл через curl:

```bash
TASK=$(curl -s -F "file=@big.pdf" http://localhost:8000/convert/async | jq -r .task_id)

# ждём завершения
until [ "$(curl -s http://localhost:8000/tasks/$TASK | jq -r .status)" = "completed" ]; do sleep 5; done

curl -o out.docx http://localhost:8000/files/$TASK
```

### `GET /health` — готовность сервиса

`200 ok` — модели загружены; `503 warming_up` — ещё прогревается (можно использовать как
readiness/liveness probe в оркестраторах).

---

## 3. Клиенты на Python и JavaScript

Из `openapi.json` можно сгенерировать клиент автоматически
(`openapi-generator generate -i openapi.json -g python -o ./client`), но для одного
эндпоинта проще отправить запрос руками:

**Python (requests):**

```python
import requests

BASE = "http://localhost:8000"

with open("scan.pdf", "rb") as f:
    r = requests.post(
        f"{BASE}/convert",
        files={"file": ("scan.pdf", f, "application/pdf")},
        data={"lang": ["en", "vi"], "pages": "2-5", "scanned": "true"},
        timeout=600,
    )
r.raise_for_status()
open("out.docx", "wb").write(r.content)
print("saved out.docx,", len(r.content), "bytes")
```

**Python, асинхронный режим:**

```python
import requests, time

BASE = "http://localhost:8000"

with open("big.pdf", "rb") as f:
    t = requests.post(f"{BASE}/convert/async", files={"file": f}).json()

while True:
    t = requests.get(f"{BASE}/tasks/{t['task_id']}").json()
    if t["status"] == "completed":
        break
    if t["status"] == "failed":
        raise RuntimeError(t["error"])
    time.sleep(5)

docx = requests.get(f"{BASE}{t['download_url']}").content
open("out.docx", "wb").write(docx)
```

**JavaScript (fetch, Node ≥ 18 / браузер):**

```js
const form = new FormData();
form.append("file", new Blob([await fs.promises.readFile("scan.pdf")], { type: "application/pdf" }), "scan.pdf");
form.append("lang", "en");
form.append("scanned", "true");

const res = await fetch("http://localhost:8000/convert", { method: "POST", body: form });
if (!res.ok) throw new Error(await res.text());
await fs.promises.writeFile("out.docx", Buffer.from(await res.arrayBuffer()));
```

---

## 4. Запуск без Docker (локально)

```bash
python -m venv .venv && source .venv/bin/activate
pip install docling docling-core python-docx pymupdf pandas easyocr fastapi uvicorn python-multipart

# ocr.py — это оригинальный скрипт репозитория (в Docker он получается переименованием
# ocr_table_rebuild.easyOCR..py в ocr.py):
curl -O https://raw.githubusercontent.com/phuongbigbig/OCR_pdf2docx/master/ocr_table_rebuild.easyOCR..py
mv ocr_table_rebuild.easyOCR..py ocr.py

uvicorn api:app --host 0.0.0.0 --port 8000
```

Первые запуски скачают модели Docling/EasyOCR (~2–4 ГБ) в кэш; дальше всё работает офлайн.

---

## 5. Переменные окружения

| Переменная                | По умолчанию | Описание                                                        |
|---------------------------|--------------|-----------------------------------------------------------------|
| `OCR_API_LANGS`           | `en vi`      | Языки OCR по умолчанию (пробел-раздельный список кодов EasyOCR)|
| `OCR_API_MAX_UPLOAD_MB`   | `100`        | Максимальный размер загружаемого PDF (превышение → `413`)       |
| `OCR_API_MAX_CONCURRENCY` | `1`          | Число параллельных конвертаций (увеличивайте только с запасом CPU/RAM/GPU) |
| `PORT`                    | `8000`       | Порт при запуске `python api.py`                                |

Пример: русскоязычные документы, лимит 200 МБ, две параллельные конвертации:

```bash
docker run -d --name ocr-api -p 8000:8000 \
  -e OCR_API_LANGS="ru en" \
  -e OCR_API_MAX_UPLOAD_MB=200 \
  -e OCR_API_MAX_CONCURRENCY=2 \
  ocr-pdf2docx-api
```

> Если меняете языки по умолчанию — добавьте их коды и в Dockerfile (строка прогрева
> EasyOCR `easyocr.Reader(['en', 'vi'])`), иначе при первом запросе этих языков образ
> будет докачивать модель в рантайме (нужен интернет).

---

## 6. Варианты развёртывания

**GPU-ускорение EasyOCR/Docling** — соберите образ на CUDA-базе
(`FROM nvidia/cuda:...-runtime` + `torch` с соответствующим index) и запускайте с
`--gpus all`; распознавание ускоряется в разы.

**docker-compose.yml:**

```yaml
services:
  ocr-api:
    build: .
    ports: ["8000:8000"]
    environment:
      OCR_API_LANGS: "en vi ru"
      OCR_API_MAX_UPLOAD_MB: "200"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      retries: 20
      start_period: 300s   # прогрев моделей
```

**Kubernetes:** readiness-probe на `GET /health` (модели греются минуты), ограничьте
`resources.requests.memory` не менее 4Gi, replicas=1 при CPU-конверсии.

**Реверс-прокси (nginx):** увеличьте `client_max_body_size` до лимита API и
`proxy_read_timeout` под длительную синхронную конвертацию, либо используйте async-эндпоинты.

---

## 7. CLI-режим оригинального скрипта

Контейнер умеет и обычным скриптом (команда override'ом перекрывает CMD сервера):

```bash
docker run --rm -v "$PWD:/data" ocr-pdf2docx-api \
  python /app/ocr.py --no-open -i document.pdf
# создаст document_converted.docx рядом с входным файлом
```

---

## 8. Структура проекта и устройство образа

```
.
├── Dockerfile        # python:3.11-slim + зависимости + прогрев моделей + uvicorn
├── api.py            # FastAPI-приложение (все эндпоинты выше)
├── requirements.txt  # зависимости для локального запуска
└── README.md         # этот файл
```

Внутри образа: `/app/ocr.py` — исходный скрипт OCR_pdf2docx (клон репозитория на этапе
сборки), `/app/api.py` — импортирует из него функции построения конвертера и сборки таблиц
Word, поэтому качество верстки совпадает с CLI-версией.

## 9. Ограничения и известное поведение

- Первый запрос после старта медленнее остальных (ленивая инициализация пайплайна).
- Временные файлы задач хранятся в памяти процесса; при рестарте контейнера незабранные
  результаты теряются — скачивайте DOCX сразу после завершения задачи.
- Нет аутентификации — для продакшена поставьте перед сервисом auth-proxy/API-key.
- Объединённые ячейки PDF-таблиц воспроизводятся как текст в каждой клетке (полный
  merge-complexity пока не поддерживается ни TableFormer, ни python-docx без костылей).
