import os
import tempfile
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from functools import partial

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
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


async def _convert_and_patch(app: FastAPI, doc_id: int, pdf_bytes: bytes | None) -> bool:
    client: httpx.AsyncClient = app.state.http

    if pdf_bytes is None:
        logger.info("Downloading PDF for document %d", doc_id)
        resp = await client.get(f"{PAPERLESS_URL}/api/documents/{doc_id}/download/")
        if resp.status_code != 200:
            logger.error("Download failed for document %d: %s", doc_id, resp.text[:200])
            return False
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
        return False
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
        return False

    logger.info("Done: document %d, %d chars in %.1fs total", doc_id, len(markdown), time.monotonic() - t0)
    return True


async def _get_tag_id(client: httpx.AsyncClient, tag_name: str) -> int | None:
    resp = await client.get(f"{PAPERLESS_URL}/api/tags/", params={"name__iexact": tag_name})
    if resp.status_code != 200:
        return None
    for tag in resp.json().get("results", []):
        if tag.get("name", "").lower() == tag_name.lower():
            return tag["id"]
    return None


async def _get_documents_by_tag(client: httpx.AsyncClient, tag_id: int) -> list[dict]:
    documents: list[dict] = []
    url: str | None = f"{PAPERLESS_URL}/api/documents/"
    params: dict = {"tags__id__in": tag_id, "page_size": 100}
    while url:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.error("Failed to fetch documents for tag %d: %s", tag_id, resp.text[:200])
            break
        data = resp.json()
        documents.extend(data.get("results", []))
        url = data.get("next")
        params = {}
    return documents


async def _update_document_tags(
    client: httpx.AsyncClient,
    doc_id: int,
    remove_tag_id: int,
    add_tag_id: int | None,
):
    resp = await client.get(f"{PAPERLESS_URL}/api/documents/{doc_id}/")
    if resp.status_code != 200:
        logger.error("Could not fetch document %d to update tags", doc_id)
        return
    current_tags: list[int] = resp.json().get("tags", [])
    new_tags = [t for t in current_tags if t != remove_tag_id]
    if add_tag_id is not None and add_tag_id not in new_tags:
        new_tags.append(add_tag_id)
    patch = await client.patch(f"{PAPERLESS_URL}/api/documents/{doc_id}/", json={"tags": new_tags})
    if patch.status_code not in (200, 201):
        logger.error("Tag update failed for document %d: %s", doc_id, patch.text[:200])
    else:
        logger.info("Tags updated for document %d", doc_id)


async def _process_documents_by_tag(app: FastAPI, tag_name: str, done_tag_name: str | None):
    client: httpx.AsyncClient = app.state.http

    source_tag_id = await _get_tag_id(client, tag_name)
    if source_tag_id is None:
        logger.error("Tag %r not found in paperless-ngx", tag_name)
        return
    logger.info("Source tag %r → ID %d", tag_name, source_tag_id)

    done_tag_id: int | None = None
    if done_tag_name:
        done_tag_id = await _get_tag_id(client, done_tag_name)
        if done_tag_id is None:
            logger.warning("Done tag %r not found, proceeding without it", done_tag_name)
        else:
            logger.info("Done tag %r → ID %d", done_tag_name, done_tag_id)

    documents = await _get_documents_by_tag(client, source_tag_id)
    logger.info("Found %d document(s) with tag %r", len(documents), tag_name)

    succeeded = 0
    for doc in documents:
        doc_id = doc["id"]
        logger.info("Processing document %d (%s)", doc_id, doc.get("title", ""))
        ok = await _convert_and_patch(app, doc_id, None)
        if ok:
            await _update_document_tags(client, doc_id, source_tag_id, done_tag_id)
            succeeded += 1
        else:
            logger.warning("Skipping tag update for document %d due to conversion failure", doc_id)

    logger.info("Batch complete: %d/%d succeeded", succeeded, len(documents))


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


class ProcessByTagRequest(BaseModel):
    tag: str
    done_tag: str | None = None


@app.post("/process-by-tag", status_code=202)
async def process_by_tag(req: ProcessByTagRequest, background_tasks: BackgroundTasks):
    logger.info("Accepted batch job: tag=%r, done_tag=%r", req.tag, req.done_tag)
    background_tasks.add_task(_process_documents_by_tag, app, req.tag, req.done_tag)
    return {"accepted": True, "tag": req.tag, "done_tag": req.done_tag}
