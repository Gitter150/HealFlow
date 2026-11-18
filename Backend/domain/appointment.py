from dataclasses import dataclass


@dataclass
class Appointment:
    """Booking data in the existing database vocabulary (risk is a percentage)."""

    patient_id: str
    doctor_id: str
    appointment_time: str
    status: str = "scheduled"
    priority_score: int = 1
    no_show_risk: float = 0.0
    appointment_id: int | None = None
