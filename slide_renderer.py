from __future__ import annotations

from pathlib import Path

import fitz


PDF_RENDER_DPI = 180


class SlideRenderError(Exception):
    """Raised when a source slide file cannot be rendered."""


class UnsupportedPdfError(SlideRenderError):
    """Raised when the uploaded PDF cannot be rendered as a slide draft."""


def render_pdf_pages(pdf_path: Path, output_dir: Path) -> list[Path]:
    try:
        document = fitz.open(pdf_path)
    except Exception as exc:
        raise UnsupportedPdfError("Could not open the uploaded PDF.") from exc

    try:
        if document.page_count < 1:
            raise UnsupportedPdfError("The uploaded PDF does not contain any pages.")

        zoom = PDF_RENDER_DPI / 72
        rendered_paths = []
        output_dir.mkdir(parents=True, exist_ok=True)
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            output_path = output_dir / f"slide-draft-page-{page_index + 1:03}.png"
            pixmap.save(output_path)
            rendered_paths.append(output_path)

        return rendered_paths
    except UnsupportedPdfError:
        raise
    except Exception as exc:
        raise SlideRenderError("Could not render the PDF pages.") from exc
    finally:
        document.close()


def render_pdf_first_page(pdf_path: Path, output_path: Path) -> Path:
    output_dir = output_path.parent / f"{output_path.stem}-pages"
    rendered_paths = render_pdf_pages(pdf_path, output_dir)
    output_path.write_bytes(rendered_paths[0].read_bytes())
    return output_path
