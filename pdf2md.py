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


def _convert_pdf_to_markdown(pdf_path: str, config: dict) -> str:
    logger.info("Vision API URL: %s", config["url"])
    logger.info("Model: %s", config["modell"])

    pages = convert_from_path(pdf_path)
    anzahl_seiten = len(pages)
    logger.info("PDF loaded: %d page(s)", anzahl_seiten)

    client = OpenAI(base_url=config["url"], api_key=config["key"])
    gesamt_markdown = []

    for i, page in enumerate(pages):
        buffered = BytesIO()
        page.save(buffered, format="JPEG", quality=80)
        img_bytes = buffered.getvalue()
        img_base64 = base64.b64encode(img_bytes).decode("utf-8")
        logger.info("Page %d/%d — image size: %d bytes, sending to vision API...", i + 1, anzahl_seiten, len(img_bytes))

        messages_content = [
            {
                "type": "text",
                "text": (
                    f"You are a precise OCR system. Convert the content of this PDF page (page {i+1}) "
                    "into clean Markdown format. Preserve headings, lists, and tables. "
                    "Return ONLY the raw Markdown, without any introduction, code fences, or explanations."
                )
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

            seite_markdown = response.choices[0].message.content

            if seite_markdown.startswith("```markdown"):
                seite_markdown = seite_markdown.split("```markdown")[1].rsplit("```", 1)[0].strip()
            elif seite_markdown.startswith("```"):
                seite_markdown = seite_markdown.split("```")[1].rsplit("```", 1)[0].strip()

            logger.info("Page %d/%d — done in %.1fs, %d chars returned", i + 1, anzahl_seiten, elapsed, len(seite_markdown))
            gesamt_markdown.append(seite_markdown)

        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error("Page %d/%d — failed after %.1fs: %s", i + 1, anzahl_seiten, elapsed, e)
            gesamt_markdown.append(f"\n* Error on page {i+1}: {e} *\n")

    return "\n\n---\n\n".join(gesamt_markdown)


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

    # Resolution order: CLI argument -> .env variable -> built-in default
    config = {
        "url": args.url or os.getenv("LOKALE_API_URL", "http://localhost:11434/v1"),
        "modell": args.model or os.getenv("LOKALES_VISION_MODELL", "llama3.2-vision"),
        "key": args.key or os.getenv("LOKALER_API_KEY", "ollama")
    }

    print("\n--- Configuration ---")
    print(f"Input PDF:   {args.pdf}")
    print(f"Output MD:   {args.output}")
    print(f"API URL:     {config['url']}")
    print(f"Model:       {config['modell']}")
    print(f"API key:     {'***' + config['key'][-3:] if len(config['key']) > 3 else 'set'}")
    print("---------------------\n")

    if os.path.exists(args.pdf):
        pdf_to_markdown_local(args.pdf, args.output, config)
    else:
        print(f"❌ Error: file '{args.pdf}' not found.")
