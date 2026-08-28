from django.db.models import Case, IntegerField, Value, When

SIZE_ORDER = ['XS', 'S', 'SM', 'M', 'ML', 'L', 'XL', '1XL', '2XL', 'XXL', '3XL', 'XXXL', '4XL', '5XL', '6XL']


def size_order_annotation(field='size'):
    """
    Returns a Django Case/When annotation that maps standard apparel sizes
    to sort integers. Unknown sizes sort last (value 999).

    Usage:
        qs.annotate(size_ord=size_order_annotation()).order_by('size_ord', 'color')
    """
    whens = [When(**{f'{field}__iexact': s, 'then': Value(i)}) for i, s in enumerate(SIZE_ORDER)]
    return Case(*whens, default=Value(999), output_field=IntegerField())
