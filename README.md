# pl-llm-ocr

Converts PDF documents to Markdown using a local vision-capable LLM. Integrates with [paperless-ngx](https://docs.paperless-ngx.com/) as a webhook-driven service: when a document arrives in paperless-ngx, the service receives the PDF, converts each page via the vision LLM, and writes the resulting Markdown back into the document's content field.

## Architecture

```
paperless-ngx  ──webhook──▶  pl-llm-ocr (FastAPI)  ──OpenAI-API──▶  vision LLM (Ollama/compatible)
    (Unraid)                   (Docker, thyestes)                        (Olares)
```

1. A paperless-ngx workflow triggers on document creation and POSTs the raw PDF to `/process`.
2. The service responds `202 Accepted` immediately so paperless-ngx does not time out.
3. In the background, each PDF page is rendered as JPEG and sent to the vision LLM.
4. The resulting Markdown is written back to the document via `PATCH /api/documents/{id}/`.

The service also supports batch processing: tagging documents in paperless-ngx with a marker tag and calling `/process-by-tag` will OCR all matching documents, remove the marker tag, and optionally apply a "done" tag.

## Requirements

- Docker on the host running the service
- [poppler-utils](https://poppler.freedesktop.org/) (bundled in the Docker image)
- An OpenAI-compatible vision LLM endpoint (e.g. [Ollama](https://ollama.com/) with a vision model)
- A running paperless-ngx instance with API access

## Configuration

All configuration is supplied via environment variables (or a `.env` file, never committed):

| Variable | Description |
|---|---|
| `PAPERLESS_URL` | Base URL of paperless-ngx, e.g. `http://192.168.1.10:8000` |
| `PAPERLESS_TOKEN` | paperless-ngx API token (Settings → API) |
| `VISION_API_URL` | Base URL of the vision LLM endpoint, e.g. `http://my-server/v1` |
| `VISION_MODEL` | Model name, e.g. `gemma-4-26b-mtp-vision` |
| `VISION_API_KEY` | API key for the LLM endpoint (`ollama` if none required) |

Copy `.env.example` to `.env` and fill in the values:

```
PAPERLESS_URL=http://192.168.1.10:8000
PAPERLESS_TOKEN=your-paperless-token
VISION_API_URL=http://your-llm-server/v1
VISION_MODEL=gemma-4-26b-mtp-vision
VISION_API_KEY=ollama
```

### mDNS / hostname resolution

If your LLM endpoint uses a `.local` mDNS hostname that does not resolve inside Docker containers, add a static hosts entry to `entrypoint.sh`:

```sh
#!/bin/sh
echo "192.168.1.42 your-server.local" >> /etc/hosts
exec uvicorn api:app --host 0.0.0.0 --port 8000
```

## Build and deploy

```bash
# Build the image
docker build -t pl-llm-ocr .

# Run with a .env file
docker run -d \
  --name pl-llm-ocr \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env \
  pl-llm-ocr
```

Check the service is up:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

## Prompts

OCR prompts are configured in `prompts.yml`. The file has a `default` key used as a fallback, plus optional keys named after paperless-ngx document types. When a document is processed, its document type is fetched from paperless-ngx and matched against the prompts file; if no specific prompt exists for that type the `default` is used.

```yaml
default: |
  You are a precise OCR system. Convert the content of this PDF page (page {page} of {total})
  into clean Markdown format. Preserve headings, lists, and tables.
  Return ONLY the raw Markdown, without any introduction, code fences, or explanations.

Invoice: |
  You are a precise OCR system specializing in invoices and financial documents.
  ...

Letter: |
  ...
```

The placeholders `{page}` and `{total}` are replaced with the current page number and total page count at runtime. The document type keys must match the type names in paperless-ngx exactly (case-sensitive).

To use a custom prompts file, set the `PROMPTS_FILE` environment variable:

```
PROMPTS_FILE=/path/to/my-prompts.yml
```

To mount a custom file into the container without rebuilding:

```bash
docker run -d \
  --name pl-llm-ocr \
  -p 8000:8000 \
  --env-file .env \
  -v /path/to/my-prompts.yml:/app/prompts.yml \
  pl-llm-ocr
```

## paperless-ngx workflow setup

Create a workflow in paperless-ngx (Settings → Workflows):

- **Trigger:** Document added
- **Action type:** Webhook (type 4)
- **URL:** `http://<service-host>:8000/process`
- **Send document:** enabled (sends the PDF as multipart/form-data)

The service identifies the document in paperless-ngx by matching the uploaded filename against the 10 most recently added documents. No document ID needs to be passed explicitly.

## API endpoints

### `GET /health`

Returns `{"status": "ok"}`. Use for container health checks.

### `POST /process`

Accepts either:

- **multipart/form-data** with a `file` field (raw PDF) — used by the paperless-ngx webhook
- **JSON** with a `document_id` field — useful for manual or n8n-triggered calls

Returns `202 Accepted` immediately. Conversion runs in the background.

```bash
# JSON trigger (manual)
curl -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{"document_id": 42}'
```

### `POST /process-by-tag`

Finds all documents in paperless-ngx that carry a given tag, runs OCR on each, and on success removes the source tag and optionally adds a done tag. Documents that fail conversion keep the source tag so they can be retried.

```bash
curl -X POST http://localhost:8000/process-by-tag \
  -H 'Content-Type: application/json' \
  -d '{"tag": "ocr-needed", "done_tag": "ocr-done"}'
```

| Field | Required | Description |
|---|---|---|
| `tag` | yes | Name of the tag that marks documents to process |
| `done_tag` | no | Tag to apply after successful conversion |

Both tags must already exist in paperless-ngx. Returns `202 Accepted` immediately.

## CLI usage (without Docker)

`pdf2md.py` can also be used as a standalone script:

```bash
# Install dependencies (requires poppler-utils on the system)
pip install -r requirements.txt

# Convert a PDF to a Markdown file
python pdf2md.py document.pdf -o output.md

# Override API settings on the command line
python pdf2md.py document.pdf \
  --url http://localhost:11434/v1 \
  --model llama3.2-vision \
  --key ollama \
  -o output.md
```

Settings are resolved in this order: CLI argument → `.env` variable → built-in default.

## About

This project was vibe coded with the help of [Claude Code](https://claude.ai/code). The entire service — from the initial PDF conversion script to the FastAPI webhook integration, Docker setup, and batch processing — was designed and built interactively with Claude as a pair programming partner.
