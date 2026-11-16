"""Upload validators for seller-provided files (currently: shipping labels)."""
from rest_framework.exceptions import ValidationError

_MAX_LABEL_BYTES = 10 * 1024 * 1024  # 10 MB
_PDF_SIGNATURE = b'%PDF-'


def validate_label_file(file_obj):
    """A shipping label the seller already bought elsewhere — PDF, JPEG, or PNG.

    Raises ValidationError on an oversized or unrecognized file; otherwise
    leaves the file object positioned at 0, ready to be saved as-is (the
    existing label_to_png() pipeline in apps.shipping.labels handles both
    PDF and image bytes at render time, so nothing needs converting here).
    """
    file_obj.seek(0, 2)
    size = file_obj.tell()
    file_obj.seek(0)
    if size > _MAX_LABEL_BYTES:
        raise ValidationError(f'File too large. Maximum is 10 MB (uploaded: {size / 1024 / 1024:.1f} MB).')

    head = file_obj.read(8)
    file_obj.seek(0)
    if head.startswith(_PDF_SIGNATURE):
        return

    try:
        from PIL import Image
        Image.open(file_obj).verify()
    except Exception:
        raise ValidationError('Unsupported file — upload a PDF, JPG, or PNG shipping label.')
    finally:
        file_obj.seek(0)
