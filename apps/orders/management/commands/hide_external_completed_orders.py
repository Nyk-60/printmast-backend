"""
Hide orders that were completed in ShipStation but never processed through this platform.
These are historical orders imported by accident during full sync.
"""
from django.core.management.base import BaseCommand

from apps.orders.models import Order


class Command(BaseCommand):
    help = 'Hide orders that arrived already shipped/cancelled from ShipStation'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would be hidden without making changes',
        )
        parser.add_argument(
            '--store', type=int,
            help='Limit to a specific store ID',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        store_id = options.get('store')

        # Orders that are in a terminal status but were never actually labeled
        # through this platform — regardless of whether a carrier was selected
        # or a design was uploaded — never went through our production pipeline
        # and must not be tracked as ours.
        qs = Order.objects.filter(
            status__in=('shipped', 'delivered', 'cancelled', 'refunded'),
            is_hidden=False,
            labeled_at__isnull=True,
        )
        if store_id:
            qs = qs.filter(store_id=store_id)

        count = qs.count()
        self.stdout.write(
            f'{"[DRY RUN] " if dry_run else ""}Found {count} externally-completed orders'
        )

        if not dry_run and count:
            qs.update(is_hidden=True)
            self.stdout.write(self.style.SUCCESS(f'Hidden {count} orders.'))
        elif dry_run:
            for o in qs[:50]:
                self.stdout.write(f'  #{o.external_order_number}  status={o.status}  store={o.store_id}')
