import io

from django.core.files.uploadedfile import InMemoryUploadedFile
from rest_framework import serializers

from apps.orders.services import _normalize_personalization

from .models import OrderItemDesign, SkuDesign

# ── PNG validation + auto-trim ────────────────────────────────────────────────

_PNG_SIGNATURE   = b'\x89PNG\r\n\x1a\n'
_MAX_DESIGN_BYTES = 20 * 1024 * 1024  # 20 MB


def _validate_and_trim_png(file_obj):
    """Validate PNG format/size and silently trim transparent borders.

    Returns the (possibly cropped) file object. Raises ValidationError on
    wrong format or oversized files.
    """
    # ── size ──────────────────────────────────────────────────────────────────
    file_obj.seek(0, 2)
    size = file_obj.tell()
    file_obj.seek(0)
    if size > _MAX_DESIGN_BYTES:
        raise serializers.ValidationError(
            f'File too large. Maximum is 20 MB (uploaded: {size / 1024 / 1024:.1f} MB).'
        )

    # ── PNG magic bytes ───────────────────────────────────────────────────────
    if file_obj.read(8) != _PNG_SIGNATURE:
        raise serializers.ValidationError('Only PNG files are accepted.')
    file_obj.seek(0)

    # ── open + trim ───────────────────────────────────────────────────────────
    try:
        from PIL import Image
        img = Image.open(file_obj)
        img.load()
    except Exception:
        raise serializers.ValidationError('Invalid or corrupted PNG file.')

    if img.mode in ('RGBA', 'LA') or 'transparency' in img.info:
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        _, _, _, alpha = img.split()
        bbox = alpha.getbbox()  # None if fully transparent; else (x0,y0,x1,y1)
        if bbox and bbox != (0, 0, img.width, img.height):
            img = img.crop(bbox)
            buf = io.BytesIO()
            img.save(buf, format='PNG', optimize=True)
            buf_size = buf.tell()
            buf.seek(0)
            return InMemoryUploadedFile(
                buf, 'file',
                getattr(file_obj, 'name', 'design.png'),
                'image/png', buf_size, None,
            )

    file_obj.seek(0)
    return file_obj


class SkuDesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = SkuDesign
        fields = (
            'id', 'sku', 'print_area_name', 'color_scope', 'specific_color', 'personalization_match',
            'file', 'file_name', 'width_px', 'height_px', 'created_at',
        )
        read_only_fields = ('id', 'file_name', 'width_px', 'height_px', 'created_at')

    def validate_file(self, value):
        return _validate_and_trim_png(value)

    def validate(self, attrs):
        if attrs.get('color_scope') == SkuDesign.ColorScope.SPECIFIC and not attrs.get('specific_color'):
            raise serializers.ValidationError({'specific_color': 'Required when color_scope is specific'})
        if 'personalization_match' in attrs:
            attrs['personalization_match'] = _normalize_personalization(attrs['personalization_match'])
        return attrs

    def create(self, validated_data):
        file = validated_data.get('file')
        if file:
            validated_data['file_name'] = file.name
            w, h = _image_dimensions(file)
            validated_data['width_px'] = w
            validated_data['height_px'] = h
        return SkuDesign.objects.create(sub_user=self.context['sub_user'], **validated_data)

    def update(self, instance, validated_data):
        file = validated_data.get('file')
        if file:
            validated_data['file_name'] = file.name
            w, h = _image_dimensions(file)
            validated_data['width_px'] = w
            validated_data['height_px'] = h
        return super().update(instance, validated_data)


class OrderItemDesignSerializer(serializers.ModelSerializer):
    color_scope = serializers.ChoiceField(
        choices=SkuDesign.ColorScope.choices, required=False, default=SkuDesign.ColorScope.ALL, write_only=True,
    )
    specific_color = serializers.CharField(required=False, allow_blank=True, default='', write_only=True)
    specific_colors = serializers.CharField(
        required=False, allow_blank=True, default='', write_only=True,
        help_text='Comma-separated color names. Used instead of specific_color to save one design '
                  'to the library under several colors at once (each gets its own SkuDesign row).',
    )
    personalization_match = serializers.CharField(required=False, allow_blank=True, default='', write_only=True)
    save_to_library = serializers.BooleanField(required=False, default=False, write_only=True)

    class Meta:
        model = OrderItemDesign
        fields = (
            'id', 'print_area_name', 'file', 'file_name',
            'width_px', 'height_px', 'sku_design', 'created_at',
            'color_scope', 'specific_color', 'specific_colors', 'personalization_match', 'save_to_library',
        )
        read_only_fields = ('id', 'file_name', 'width_px', 'height_px', 'sku_design', 'created_at')

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # write_only fields are excluded by default; add read values from linked SkuDesign
        if instance.sku_design_id:
            data['color_scope']    = instance.sku_design.color_scope
            data['specific_color'] = instance.sku_design.specific_color
        else:
            data['color_scope']    = None
            data['specific_color'] = ''
        return data

    def validate_file(self, value):
        return _validate_and_trim_png(value)

    def validate(self, attrs):
        if attrs.get('color_scope') == SkuDesign.ColorScope.SPECIFIC \
                and not attrs.get('specific_color') and not attrs.get('specific_colors'):
            raise serializers.ValidationError({'specific_color': 'Required when color_scope is specific'})
        attrs['personalization_match'] = _normalize_personalization(attrs.get('personalization_match', ''))
        return attrs

    def create(self, validated_data):
        color_scope = validated_data.pop('color_scope', SkuDesign.ColorScope.ALL)
        specific_color = validated_data.pop('specific_color', '')
        specific_colors_raw = validated_data.pop('specific_colors', '')
        personalization_match = validated_data.pop('personalization_match', '')
        save_to_library = validated_data.pop('save_to_library', False)

        file = validated_data.get('file')
        if file:
            validated_data['file_name'] = file.name
            w, h = _image_dimensions(file)
            validated_data['width_px'] = w
            validated_data['height_px'] = h

        if save_to_library:
            order_item = self.context['order_item']
            sub_user = self.context['sub_user']

            if color_scope == SkuDesign.ColorScope.SPECIFIC:
                colors = [c.strip() for c in specific_colors_raw.split(',') if c.strip()] \
                    or ([specific_color] if specific_color else [])
                current_color = order_item.pc_variant.color if order_item.pc_variant_id else None
                created = []
                for color_name in colors:
                    if file:
                        file.seek(0)
                    sd, _ = SkuDesign.objects.update_or_create(
                        sub_user=sub_user,
                        sku=order_item.marketplace_sku,
                        print_area_name=validated_data['print_area_name'],
                        color_scope=SkuDesign.ColorScope.SPECIFIC,
                        specific_color=color_name,
                        personalization_match=personalization_match,
                        defaults={
                            'file': validated_data['file'],
                            'file_name': validated_data['file_name'],
                            'width_px': validated_data['width_px'],
                            'height_px': validated_data['height_px'],
                        },
                    )
                    created.append(sd)
                sku_design = next((sd for sd in created if sd.specific_color == current_color), created[0] if created else None)
            else:
                if file:
                    file.seek(0)
                sku_design, _ = SkuDesign.objects.update_or_create(
                    sub_user=sub_user,
                    sku=order_item.marketplace_sku,
                    print_area_name=validated_data['print_area_name'],
                    color_scope=color_scope,
                    specific_color='',
                    personalization_match=personalization_match,
                    defaults={
                        'file': validated_data['file'],
                        'file_name': validated_data['file_name'],
                        'width_px': validated_data['width_px'],
                        'height_px': validated_data['height_px'],
                    },
                )

            validated_data['sku_design'] = sku_design
            # Expose the saved SkuDesign so the view can return correct data even when the
            # OrderItemDesign isn't updated (non-matching scope — e.g., dark on a white shirt).
            self.context['saved_sku_design'] = sku_design
            if file:
                file.seek(0)  # SkuDesign.file already consumed the stream — rewind for OrderItemDesign.file

        order_item = self.context['order_item']

        # Only create/update OrderItemDesign when the design scope matches this item's color.
        # Dark scope shouldn't overwrite an existing (or future) light design on a light-colored item,
        # and vice versa. 'all' and 'specific' (current color) always apply.
        update_order_design = _scope_matches_item(color_scope, order_item, specific_colors_raw or specific_color)

        if update_order_design:
            if file:
                file.seek(0)
            obj, _ = OrderItemDesign.objects.update_or_create(
                order_item=order_item,
                print_area_name=validated_data['print_area_name'],
                defaults=validated_data,
            )
        else:
            # Design saved to library only; return existing OrderItemDesign or a transient stub
            obj = OrderItemDesign.objects.filter(
                order_item=order_item,
                print_area_name=validated_data['print_area_name'],
            ).first()
            if obj is None:
                # No matching design exists yet — create it anyway so the caller gets a valid response
                if file:
                    file.seek(0)
                obj, _ = OrderItemDesign.objects.update_or_create(
                    order_item=order_item,
                    print_area_name=validated_data['print_area_name'],
                    defaults=validated_data,
                )
        return obj


def _scope_matches_item(color_scope, order_item, specific_colors_raw=''):
    """Return True if this design scope is applicable to the order item's actual color."""
    if color_scope == SkuDesign.ColorScope.ALL:
        return True
    variant = getattr(order_item, 'pc_variant', None) if order_item.pc_variant_id else None
    if variant is None:
        return True  # no variant info → allow
    item_is_dark = variant.is_dark
    if color_scope == SkuDesign.ColorScope.DARK:
        return bool(item_is_dark)
    if color_scope == SkuDesign.ColorScope.LIGHT:
        return not item_is_dark
    if color_scope == SkuDesign.ColorScope.SPECIFIC:
        item_color = (variant.color or '').lower()
        colors = [c.strip().lower() for c in specific_colors_raw.split(',') if c.strip()]
        return item_color in colors if colors else True
    return True


def _image_dimensions(file_obj):
    """Return (width_px, height_px) using Pillow. Gracefully returns (0, 0) on failure."""
    try:
        from PIL import Image
        file_obj.seek(0)
        with Image.open(file_obj) as img:
            return img.width, img.height
    except Exception:
        return 0, 0
