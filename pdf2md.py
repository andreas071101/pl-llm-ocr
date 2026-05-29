import argparse
import base64
import logging
import os
import time
from io import BytesIO
from openai import OpenAI
from pdf2image import convert_from_path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = (
    "You are a precise OCR system. Convert the content of this PDF page (page {page} of {total}) "
    "into clean Markdown format. Preserve headings, lists, and tables. "
    "Return ONLY the raw Markdown, without any introduction, code fences, or explanations."
)


def _convert_pdf_to_markdown(pdf_path: str, config: dict) -> str:
    logger.info("Vision API URL: %s", config["url"])
    logger.info("Model: %s", config["modell"])

    pages = convert_from_path(pdf_path)
    total = len(pages)
    logger.info("PDF loaded: %d page(s)", total)

    prompt_template = config.get("prompt") or DEFAULT_PROMPT
    client = OpenAI(base_url=config["url"], api_key=config["key"])
    result_pages = []

    for i, page in enumerate(pages):
        buffered = BytesIO()
        page.save(buffered, format="JPEG", quality=80)
        img_bytes = buffered.getvalue()
        img_base64 = base64.b64encode(img_bytes).decode("utf-8")
        logger.info("Page %d/%d — image size: %d bytes, sending to vision API...", i + 1, total, len(img_bytes))

        prompt_text = prompt_template.format(page=i + 1, total=total)
        messages_content = [
            {
                "type": "text",
                "text": prompt_text,
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{img_base64}"
                }
            }
        ]

        t0 = time.monotonic()
        try:
            response = client.chat.completions.create(
                model=config["modell"],
                messages=[{"role": "user", "content": messages_content}],
                max_tokens=2048
            )
            elapsed = time.monotonic() - t0

            page_markdown = response.choices[0].message.content

            if page_markdown.startswith("```markdown"):
                page_markdown = page_markdown.split("```markdown")[1].rsplit("```", 1)[0].strip()
            elif page_markdown.startswith("```"):
                page_markdown = page_markdown.split("```")[1].rsplit("```", 1)[0].strip()

            logger.info("Page %d/%d — done in %.1fs, %d chars returned", i + 1, total, elapsed, len(page_markdown))
            result_pages.append(page_markdown)

        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error("Page %d/%d — failed after %.1fs: %s", i + 1, total, elapsed, e)
            result_pages.append(f"\n* Error on page {i+1}: {e} *\n")

    return "\n\n---\n\n".join(result_pages)


def pdf_to_markdown_local(pdf_path: str, output_md_path: str, config: dict):
    print(f"🔄 Reading PDF: {pdf_path}")

    try:
        result = _convert_pdf_to_markdown(pdf_path, config)
    except Exception as e:
        print(f"❌ PDF conversion failed (is Poppler installed?): {e}")
        return

    with open(output_md_path, "w", encoding="utf-8") as f:
        f.write(result)

    print(f"✅ Done! Document saved to '{output_md_path}'.")


def pdf_to_markdown_string(pdf_path: str, config: dict) -> str:
    """Returns the markdown string. Raises on error."""
    return _convert_pdf_to_markdown(pdf_path, config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Converts a PDF to a Markdown document using a local vision LLM."
    )

    # Required arguments
    parser.add_argument(
        "pdf",
        type=str,
        help="Path to the source PDF file"
    )

    # File options
    parser.add_argument(
        "-o", "--output",
        type=str,
        default="output.md",
        help="Path to the output Markdown file (default: output.md)"
    )
    parser.add_argument(
        "-e", "--env",
        type=str,
        default=None,
        help="Path to a specific .env file (optional)"
    )

    # Prompt options
    parser.add_argument(
        "--prompt-file",
        type=str,
        default=None,
        help="Path to a YAML prompts file (default: prompts.yml if present)"
    )
    parser.add_argument(
        "--document-type",
        type=str,
        default=None,
        help="Document type name to select a prompt from the prompts file"
    )

    # API options (override .env values)
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="Base URL of the OpenAI-compatible API (e.g. http://localhost:11434/v1)"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Name of the local vision model (e.g. llama3.2-vision)"
    )
    parser.add_argument(
        "--key",
        type=str,
        default=None,
        help="API key for authentication at the endpoint (if required)"
    )

    args = parser.parse_args()

    # Load .env file if present
    if args.env and os.path.exists(args.env):
        load_dotenv(dotenv_path=args.env)
    else:
        load_dotenv()

    # Load prompts file
    prompts: dict = {}
    prompts_path = args.prompt_file or "prompts.yml"
    if os.path.exists(prompts_path):
        import yaml
        with open(prompts_path) as f:
            prompts = yaml.safe_load(f) or {}
        print(f"Loaded prompts from {prompts_path}")

    prompt: str | None = None
    if args.document_type:
        prompt = prompts.get(args.document_type) or prompts.get("default")
        print(f"Prompt: {args.document_type!r} ({'matched' if prompts.get(args.document_type) else 'using default'})")
    elif prompts.get("default"):
        prompt = prompts["default"]

    # Resolution order: CLI argument -> .env variable -> built-in default
    config = {
        "url":    args.url or os.getenv("VISION_API_URL", "http://localhost:11434/v1"),
        "modell": args.model or os.getenv("VISION_MODEL", "llama3.2-vision"),
        "key":    args.key or os.getenv("VISION_API_KEY", "ollama"),
        "prompt": prompt,
    }

    print("\n--- Configuration ---")
    print(f"Input PDF:      {args.pdf}")
    print(f"Output MD:      {args.output}")
    print(f"API URL:        {config['url']}")
    print(f"Model:          {config['modell']}")
    print(f"API key:        {'***' + config['key'][-3:] if len(config['key']) > 3 else 'set'}")
    print(f"Document type:  {args.document_type or '(none)'}")
    print("---------------------\n")

    if os.path.exists(args.pdf):
        pdf_to_markdown_local(args.pdf, args.output, config)
    else:
        print(f"❌ Error: file '{args.pdf}' not found.")
