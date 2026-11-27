"""Background update scheduling (systemd --user) and desktop notifications."""

from appima.service.notify import notify_updates
from appima.service.systemd import get_timer_status, sync_update_timer

__all__ = ["notify_updates", "sync_update_timer", "get_timer_status"]
