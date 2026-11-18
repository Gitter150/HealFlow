from typing import Protocol
from domain.appointment_change import AppointmentChange


class AppointmentActionRepository(Protocol):
    def apply(self, appointment_id: int, change: AppointmentChange) -> None: ...


class PostgresAppointmentActionRepository:
    def __init__(self, connection_factory):
        self._connection_factory = connection_factory

    def apply(self, appointment_id: int, change: AppointmentChange) -> None:
        # Column names come only from this allowlist, never from request text.
        fields = [(name, getattr(change, name)) for name in
                  ("status", "is_overbooked", "entry_time")
                  if getattr(change, name) is not None]
        if not fields:
            raise ValueError("An appointment change must update at least one field")
        connection = self._connection_factory()
        cursor = None
        try:
            cursor = connection.cursor()
            assignments = ", ".join(f"{name} = %s" for name, _ in fields)
            cursor.execute(f"UPDATE appointments SET {assignments} WHERE appointment_id = %s",
                           tuple(value for _, value in fields) + (appointment_id,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            try:
                if cursor is not None:
                    cursor.close()
            finally:
                connection.close()
