"""Auto-optimizes uploaded display images (product photos, icons, logos — never
production/print files) to WebP at a web-friendly size, so catalog pages stay
fast without the person uploading needing to pre-convert anything themselves."""
import io

from django.core.files.base import ContentFile

from PIL import Image

MAX_DIMENSION = 1000
QUALITY = 82


def optimize_image(django_file, max_dimension=MAX_DIMENSION, quality=QUALITY):
    """Downscales (only if larger than max_dimension on the longest side) and
    re-encodes an uploaded image as WebP. Returns a ContentFile ready to pass
    to a FieldFile's .save(), or None if the file isn't a readable raster
    image (left untouched in that case rather than raising)."""
    try:
        img = Image.open(django_file)
        img.load()
    except Exception:
        return None

    has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
    img = img.convert('RGBA' if has_alpha else 'RGB')

    width, height = img.size
    if max(width, height) > max_dimension:
        scale = max_dimension / max(width, height)
        img = img.resize((round(width * scale), round(height * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format='WEBP', quality=quality, method=6)
    buf.seek(0)

    base = django_file.name.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    return ContentFile(buf.read(), name=f'{base}.webp')


def optimize_field(field_file):
    """Replaces an ImageField's pending file with its optimized WebP version,
    in place. Call before super().save() in the owning model's save().

    Only acts on a genuinely fresh upload — `_committed` is the same signal
    Django's own FileField save machinery uses to decide whether a field
    needs writing to storage at all. This matters a lot here: several print
    areas/icons are deliberately *shared* by assigning another instance's
    `field.name` string directly (no byte copy — see PrintCenterPrintArea
    cloning), and such an assignment is already `_committed=True`. Keying off
    that (rather than e.g. "doesn't already end in .webp") means a shared
    reference is never mistaken for a new upload and re-encoded into a
    duplicate file on every unrelated save of the row that references it."""
    if not field_file or field_file._committed:
        return
    processed = optimize_image(field_file)
    if processed:
        field_file.save(processed.name, processed, save=False)
