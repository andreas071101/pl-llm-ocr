import json as json_module
import os
import tempfile
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from functools import partial

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
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


class ProcessResponse(BaseModel):
    success: bool
    document_id: int
    content_length: int


async def _extract_document_id(request: Request) -> int:
    content_type = request.headers.get("content-type", "")
    logger.info("Incoming Content-Type: %s", content_type)

    if "multipart/form-data" in content_type:
        form = await request.form()
        logger.info("Multipart fields: %s", list(form.keys()))
        for key, value in form.multi_items():
            if isinstance(value, str):
                logger.info("Field %r (str): %s", key, value[:200])
                try:
                    data = json_module.loads(value)
                    doc_id = data.get("id") or data.get("document_id") or data.get("pk")
                    if doc_id:
                        return int(doc_id)
                except (json_module.JSONDecodeError, TypeError):
                    try:
                        return int(value)
                    except ValueError:
                        pass
            else:
                logger.info("Field %r (file): filename=%s", key, getattr(value, "filename", "?"))
        raise HTTPException(400, "Could not extract document_id from multipart payload")

    body = await request.json()
    doc_id = body.get("document_id") or body.get("id") or body.get("pk")
    if not doc_id:
        raise HTTPException(400, "document_id, id, or pk required in JSON body")
    return int(doc_id)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/process", response_model=ProcessResponse)
async def process_document(request: Request):
    doc_id = await _extract_document_id(request)
    client: httpx.AsyncClient = request.app.state.http
    logger.info("Processing document %d", doc_id)

    logger.info("Downloading PDF for document %d from %s", doc_id, PAPERLESS_URL)
    resp = await client.get(f"{PAPERLESS_URL}/api/documents/{doc_id}/download/")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Download failed: {resp.text[:200]}")
    logger.info("Downloaded %d bytes", len(resp.content))

    # delete=False: pdf2image needs the file to exist on disk during conversion
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name

    t0 = time.monotonic()
    try:
        loop = asyncio.get_event_loop()
        logger.info("Starting conversion (this may take a while while the model warms up)...")
        markdown = await loop.run_in_executor(
            None, partial(pdf_to_markdown_string, tmp_path, VISION_CONFIG)
        )
        logger.info("Conversion complete in %.1fs", time.monotonic() - t0)
    except Exception as e:
        logger.error("Conversion failed for document %d after %.1fs: %s", doc_id, time.monotonic() - t0, e)
        raise HTTPException(500, f"Conversion error: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    logger.info("Patching content back to paperless-ngx...")
    patch = await client.patch(
        f"{PAPERLESS_URL}/api/documents/{doc_id}/",
        json={"content": markdown},
    )
    if patch.status_code not in (200, 201):
        raise HTTPException(patch.status_code, f"PATCH failed: {patch.text[:200]}")

    logger.info("Done: document %d, %d chars in %.1fs total", doc_id, len(markdown), time.monotonic() - t0)
    return ProcessResponse(success=True, document_id=doc_id, content_length=len(markdown))
