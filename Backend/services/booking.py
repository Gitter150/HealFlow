from datetime import datetime
from typing import Callable, ContextManager
import logging

from domain.appointment import Appointment
from repositories.appointments import AppointmentSession
from services.prediction import NoShowPredictor, PredictionFeatures
from services.triage import TriageEngine

logger = logging.getLogger(__name__)


class BookingService:
    """Facade for booking: triage, prediction and persistence coordination."""

    def __init__(self, transaction: Callable[[], ContextManager[AppointmentSession]],
                 predictor: NoShowPredictor, triage: TriageEngine,
                 clock: Callable[[], datetime] = datetime.now):
        self._transaction = transaction
        self._predictor = predictor
        self._triage = triage
        self._clock = clock

    def book(self, appointment: Appointment) -> Appointment:
        with self._transaction() as session:
            identifier = session.save(appointment)
        appointment.appointment_id = identifier
        return appointment

    def book_with_triage(self, patient_id: str, doctor_id: str, appointment_time: str,
                         symptom: str, content: bytes | None = None,
                         filename: str = "") -> Appointment:
        text = self._triage.extract_medical_text(content, filename) if content is not None else ""
        priority = self._triage.assess_severity(symptom, text)
        appointment = Appointment(patient_id, doctor_id, appointment_time,
                                  priority_score=priority)
        with self._transaction() as session:
            # Missing models previously bypassed profile/date work entirely.
            if self._predictor.requires_features:
                try:
                    date = datetime.strptime(appointment_time[:16], "%Y-%m-%d %H:%M")
                    lead = max(0, (date - self._clock()).days)
                    metrics = session.patient_metrics(patient_id)
                    if metrics and metrics["age"] is not None and metrics["distance_miles"] is not None:
                        appointment.no_show_risk = self._predictor.predict_probability(
                            PredictionFeatures(metrics["age"], metrics["distance_miles"],
                                               lead, metrics["history"]))
                except Exception:
                    # Preserve booking's existing best-effort prediction behavior.
                    # A SQL failure still aborts the PostgreSQL transaction and save fails.
                    logger.warning("Prediction skipped; preserving booking fallback", exc_info=True)
            identifier = session.save(appointment)
        appointment.appointment_id = identifier
        return appointment
