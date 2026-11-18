from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AppointmentChange:
    """A partial update: None means leave that column unchanged."""

    status: str | None = None
    is_overbooked: bool | None = None
    entry_time: datetime | None = None
