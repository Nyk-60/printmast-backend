"""File validation for reship-request photo evidence — jpg/png/pdf, unlike
apps/designs/serializers.py::_validate_and_trim_png which is PNG-only and
auto-trims (neither applies here: evidence photos are kept as-is)."""
from rest_framework import serializers

_MAX_EVIDENCE_BYTES = 20 * 1024 * 1024  # 20 MB, same cap as design uploads

_SIGNATURES = {
    b'\xff\xd8\xff': 'image/jpeg',
    b'\x89PNG\r\n\x1a\n': 'image/png',
    b'%PDF-': 'application/pdf',
}
_MAX_SIG_LEN = max(len(sig) for sig in _SIGNATURES)


def validate_evidence_file(file_obj):
    """Validates size + magic bytes. Raises ValidationError; returns nothing
    (unlike the PNG validator, there's no trimming — the file is used as-is)."""
    file_obj.seek(0, 2)
    size = file_obj.tell()
    file_obj.seek(0)
    if size > _MAX_EVIDENCE_BYTES:
        raise serializers.ValidationError(
            f'File too large. Maximum is 20 MB (uploaded: {size / 1024 / 1024:.1f} MB).'
        )

    header = file_obj.read(_MAX_SIG_LEN)
    file_obj.seek(0)
    if not any(header.startswith(sig) for sig in _SIGNATURES):
        raise serializers.ValidationError('Only JPG, PNG, or PDF files are accepted.')
