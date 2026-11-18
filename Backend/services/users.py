"""Workflow actors following the original UML user hierarchy."""
from dataclasses import dataclass, field

from domain.appointment import Appointment
from services.appointment_actions import AdminAppointmentActions, ClinicalAppointmentActions
from services.booking import BookingService


@dataclass(frozen=True)
class User:
    """A workflow actor. The caller supplies an identifier when available."""

    user_id: str | None = None


@dataclass(frozen=True)
class Patient(User):
    user_id: str = field()

    def book_appointment(self, service: BookingService, doctor_id: str,
                         appointment_time: str, symptom: str,
                         content: bytes | None = None, filename: str = "") -> Appointment:
        return service.book_with_triage(
            self.user_id, doctor_id, appointment_time, symptom, content, filename,
        )


class Doctor(User):
    def complete_appointment(self, service: ClinicalAppointmentActions, appointment_id: int) -> str:
        return service.execute(appointment_id, "complete")


class Admin(User):
    def review_appointment(self, service: AdminAppointmentActions,
                           appointment_id: int, action: str) -> str:
        return service.execute(appointment_id, action)
