from __future__ import annotations

from pathlib import Path

import fitz


PDF_RENDER_DPI = 180


class SlideRenderError(Exception):
    """Raised when a source slide file cannot be rendered."""


class UnsupportedPdfError(SlideRenderError):
    """Raised when the uploaded PDF cannot be rendered as a slide draft."""


def render_pdf_first_page(pdf_path: Path, output_path: Path) -> Path:
    try:
        document = fitz.open(pdf_path)
    except Exception as exc:
        raise UnsupportedPdfError("Could not open the uploaded PDF.") from exc

    try:
        if document.page_count < 1:
            raise UnsupportedPdfError("The uploaded PDF does not contain any pages.")

        page = document.load_page(0)
        zoom = PDF_RENDER_DPI / 72
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pixmap.save(output_path)
        return output_path
    except UnsupportedPdfError:
        raise
    except Exception as exc:
        raise SlideRenderError("Could not render the first PDF page.") from exc
    finally:
        document.close()
