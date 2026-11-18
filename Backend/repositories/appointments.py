from contextlib import contextmanager
from typing import Protocol

from domain.appointment import Appointment


class AppointmentSession(Protocol):
    def patient_metrics(self, patient_id: str) -> dict | None: ...
    def save(self, appointment: Appointment) -> int | None: ...


class PostgresAppointmentSession:
    """SQL operations belonging to one booking transaction."""

    def __init__(self, cursor):
        self._cursor = cursor

    def patient_metrics(self, patient_id: str) -> dict | None:
        self._cursor.execute("""
            SELECT age, distance_miles,
            (SELECT COUNT(*) FROM appointments WHERE patient_id = %s
             AND status IN ('No-Show', 'no_show', 'No Show')) as history
            FROM patient_profiles WHERE uid = %s;
        """, (patient_id, patient_id))
        return self._cursor.fetchone()

    def save(self, appointment: Appointment) -> int | None:
        self._cursor.execute("""
            INSERT INTO appointments
            (patient_id, doctor_id, appointment_time, status, priority_score, no_show_risk)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING appointment_id;
        """, (appointment.patient_id, appointment.doctor_id, appointment.appointment_time,
              appointment.status, appointment.priority_score, appointment.no_show_risk))
        result = self._cursor.fetchone()
        return result["appointment_id"] if result else None


class PostgresAppointmentRepository:
    def __init__(self, connection_factory):
        self._connection_factory = connection_factory

    @contextmanager
    def transaction(self):
        connection = self._connection_factory()
        cursor = None
        try:
            cursor = connection.cursor()
            yield PostgresAppointmentSession(cursor)
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
