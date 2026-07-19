from django.core.management.base import BaseCommand
import logging

from agent_system.models import ProfileEvent
from agent_system.services.profile_events import apply_profile_event

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process pending ProfileEvent entries and apply their profile deltas to StudentProfile.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=200, help='Maximum number of events to process')

    def handle(self, *args, **options):
        limit = int(options.get('limit') or 200)
        qs = ProfileEvent.objects.filter(processed_at__isnull=True).order_by('created_at')[:limit]
        total = qs.count() if hasattr(qs, '__len__') else len(list(qs))
        processed = 0
        for ev in qs:
            try:
                apply_profile_event(ev)
                processed += 1
                self.stdout.write(self.style.SUCCESS(f"Processed event {ev.id} ({ev.event_type}) for user {ev.user_id}"))
            except Exception:
                logger.exception('Failed to process ProfileEvent %s', getattr(ev, 'id', None))
                self.stdout.write(self.style.ERROR(f"Failed event {ev.id} ({ev.event_type})"))

        self.stdout.write(self.style.SUCCESS(f'Processed {processed} profile events (queried {total})'))
