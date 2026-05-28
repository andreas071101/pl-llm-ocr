import os
import tempfile
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from functools import partial

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from dotenv import load_dotenv

from pdf2md import pdf_to_markdown_string

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Required env var {key!r} is not set")
    return val


PAPERLESS_URL = _require("PAPERLESS_URL").rstrip("/")
PAPERLESS_TOKEN = _require("PAPERLESS_TOKEN")
VISION_CONFIG = {
    "url":    _require("LOKALE_API_URL"),
    "modell": _require("LOKALES_VISION_MODELL"),
    "key":    _require("LOKALER_API_KEY"),
}

logger.info("Paperless URL: %s", PAPERLESS_URL)
logger.info("Vision API URL: %s", VISION_CONFIG["url"])
logger.info("Vision model: %s", VISION_CONFIG["modell"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        headers={"Authorization": f"Token {PAPERLESS_TOKEN}"},
        timeout=60.0,
    )
    yield
    await app.state.http.aclose()


app = FastAPI(title="pdf2md-webhook", lifespan=lifespan)


async def _find_document_id_by_filename(client: httpx.AsyncClient, filename: str) -> int:
    resp = await client.get(
        f"{PAPERLESS_URL}/api/documents/",
        params={"ordering": "-added", "page_size": 10},
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Could not query paperless documents: {resp.text[:200]}")
    results = resp.json().get("results", [])
    for doc in results:
        if doc.get("original_file_name") == filename or doc.get("title") == filename.rsplit(".", 1)[0]:
            logger.info("Matched document ID %d for filename %s", doc["id"], filename)
            return doc["id"]
    if results:
        doc_id = results[0]["id"]
        logger.warning("No exact filename match, using most recently added document ID %d", doc_id)
        return doc_id
    raise HTTPException(400, f"No documents found in paperless for filename: {filename}")


async def _convert_and_patch(app: FastAPI, doc_id: int, pdf_bytes: bytes | None):
    client: httpx.AsyncClient = app.state.http

    if pdf_bytes is None:
        logger.info("Downloading PDF for document %d", doc_id)
        resp = await client.get(f"{PAPERLESS_URL}/api/documents/{doc_id}/download/")
        if resp.status_code != 200:
            logger.error("Download failed for document %d: %s", doc_id, resp.text[:200])
            return
        pdf_bytes = resp.content
        logger.info("Downloaded %d bytes", len(pdf_bytes))

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    t0 = time.monotonic()
    try:
        loop = asyncio.get_event_loop()
        logger.info("Starting conversion for document %d...", doc_id)
        markdown = await loop.run_in_executor(
            None, partial(pdf_to_markdown_string, tmp_path, VISION_CONFIG)
        )
        logger.info("Conversion complete in %.1fs", time.monotonic() - t0)
    except Exception as e:
        logger.error("Conversion failed for document %d after %.1fs: %s", doc_id, time.monotonic() - t0, e)
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    logger.info("Patching content back to paperless-ngx for document %d...", doc_id)
    patch = await client.patch(
        f"{PAPERLESS_URL}/api/documents/{doc_id}/",
        json={"content": markdown},
    )
    if patch.status_code not in (200, 201):
        logger.error("PATCH failed for document %d: %s", doc_id, patch.text[:200])
        return

    logger.info("Done: document %d, %d chars in %.1fs total", doc_id, len(markdown), time.monotonic() - t0)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/process", status_code=202)
async def process_document(request: Request, background_tasks: BackgroundTasks):
    client: httpx.AsyncClient = request.app.state.http
    content_type = request.headers.get("content-type", "")
    logger.info("Incoming Content-Type: %s", content_type)

    pdf_bytes = None
    doc_id = None

    if "multipart/form-data" in content_type:
        form = await request.form()
        file_field = form.get("file")
        if file_field is None:
            raise HTTPException(400, "No 'file' field in multipart payload")
        filename = file_field.filename
        pdf_bytes = await file_field.read()
        logger.info("Received file: %s (%d bytes)", filename, len(pdf_bytes))
        doc_id = await _find_document_id_by_filename(client, filename)
    else:
        body = await request.json()
        doc_id = body.get("document_id") or body.get("id") or body.get("pk")
        if not doc_id:
            raise HTTPException(400, "document_id, id, or pk required in JSON body")
        doc_id = int(doc_id)

    logger.info("Accepted document %d, starting background conversion", doc_id)
    background_tasks.add_task(_convert_and_patch, request.app, doc_id, pdf_bytes)
    return {"accepted": True, "document_id": doc_id}
