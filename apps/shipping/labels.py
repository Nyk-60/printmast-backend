"""Shipping-label file handling shared by every provider.

Providers hand labels back as PDF (ByeLabel; ShipStation by default) or an image.
The production slip embeds the label as a picture — and only the label: the order
details around it must stay real text so the PDF is searchable (Google Drive
finds an order number inside a PDF's text layer, not inside an image).
"""
import base64
import io
import logging

from PIL import Image, ImageChops

logger = logging.getLogger(__name__)

LABEL_DPI = 300


def decode_label_data(data):
    """Provider label payloads are base64 text; tolerate already-raw bytes."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    return base64.b64decode(data)


def _first_page_image(raw, dpi):
    if raw[:5] == b'%PDF-':
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(raw)
        try:
            return pdf[0].render(scale=dpi / 72).to_pil().convert('RGB')
        finally:
            pdf.close()
    return Image.open(io.BytesIO(raw)).convert('RGB')


def label_to_png(raw, dpi=LABEL_DPI, pad_px=12):
    """Any label file (PDF or image) -> PNG bytes, tightly cropped, landscape.

    Labels usually sit on a mostly empty page, so the whitespace is cropped away.
    Portrait labels (the usual 4x6) are turned to landscape so they fill the slip's
    width; the label is stuck on a parcel as a whole, so its orientation on the slip
    does not matter.
    """
    img = _first_page_image(raw, dpi)

    bbox = ImageChops.difference(img, Image.new('RGB', img.size, (255, 255, 255))).getbbox()
    if bbox:
        left, top, right, bottom = bbox
        img = img.crop((
            max(0, left - pad_px), max(0, top - pad_px),
            min(img.width, right + pad_px), min(img.height, bottom + pad_px),
        ))
    if img.height > img.width:
        img = img.rotate(90, expand=True)

    # Most labels are pure black-and-white: store them as grayscale (a fraction of
    # the size, identical for barcodes) and keep colour only when there is some.
    if not ImageChops.difference(img, img.convert('L').convert('RGB')).getbbox():
        img = img.convert('L')

    out = io.BytesIO()
    img.save(out, format='PNG')
    return out.getvalue()


def load_label_png(order):
    """Label picture for an order from its stored file, or None if unavailable."""
    if not getattr(order, 'label_file', None):
        return None
    try:
        with order.label_file.open('rb') as fh:
            return label_to_png(fh.read())
    except Exception:
        logger.exception('Could not load label file for order %s', order.pk)
        return None


def store_label_file(order, raw, tracking=''):
    """Persist the provider's original label bytes on the order. Never raises: the
    label is already bought, a storage hiccup must not undo that."""
    from django.core.files.base import ContentFile

    try:
        ext = 'pdf' if raw[:5] == b'%PDF-' else 'png'
        name = f'order_{order.pk}_{tracking or "label"}.{ext}'
        order.label_file.save(name, ContentFile(raw), save=False)
        order.save(update_fields=['label_file'])
        return True
    except Exception:
        logger.exception('Could not store label file for order %s', order.pk)
        return False
