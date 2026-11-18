import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import Mock
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np

from domain.appointment import Appointment
from repositories.appointments import PostgresAppointmentRepository
from services.booking import BookingService
from services.prediction import SklearnNoShowAdapter, UnavailableModelPredictor, PredictionFeatures
from services.triage import TriageEngine
from routers.appointment_routes import router, get_booking_service


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.save.return_value = 42
        self.session.patient_metrics.return_value = {"age": 30, "distance_miles": 5, "history": 2}
        self.predictor = Mock(requires_features=True)
        self.predictor.predict_probability.return_value = 23.45
        self.triage = Mock()
        self.triage.assess_severity.return_value = 7
        self.triage.extract_medical_text.return_value = "cardiac history"
        self.events = []

        @contextmanager
        def transaction():
            self.events.append("begin")
            try:
                yield self.session
                self.events.append("commit")
            except Exception:
                self.events.append("rollback")
                raise

        self.service = BookingService(transaction, self.predictor, self.triage,
                                      lambda: datetime(2026, 11, 18, 9))

    def book(self, date="2026-11-20 09:00", **kwargs):
        return self.service.book_with_triage("patient-uid", "5", date, "Mild Pain", **kwargs)

    def test_standard_booking_preserves_caller_values_without_inference(self):
        appointment = Appointment("p", "5", "2026-11-20 09:00", "confirmed", 4, 12.5)
        self.assertIs(self.service.book(appointment), appointment)
        self.assertEqual(appointment.appointment_id, 42)
        self.predictor.predict_probability.assert_not_called()
        self.assertEqual(self.events, ["begin", "commit"])

    def test_triage_prediction_then_persist_scheduled_appointment(self):
        appointment = self.book(content=b"report", filename="report.pdf")
        self.triage.extract_medical_text.assert_called_once_with(b"report", "report.pdf")
        self.triage.assess_severity.assert_called_once_with("Mild Pain", "cardiac history")
        self.predictor.predict_probability.assert_called_once_with(PredictionFeatures(30, 5, 2, 2))
        self.assertEqual((appointment.status, appointment.priority_score, appointment.no_show_risk),
                         ("scheduled", 7, 23.45))
        self.session.save.assert_called_once_with(appointment)
        self.assertEqual(self.events, ["begin", "commit"])

    def test_incomplete_profile_retains_zero_fallback(self):
        for metrics in (None, {"age": None, "distance_miles": 5, "history": 0}):
            with self.subTest(metrics=metrics):
                self.session.patient_metrics.return_value = metrics
                self.assertEqual(self.book().no_show_risk, 0.0)
        self.predictor.predict_probability.assert_not_called()

    def test_prediction_failure_still_books(self):
        self.predictor.predict_probability.side_effect = ValueError("bad model")
        with self.assertLogs("services.booking", level="WARNING"):
            appointment = self.book()
        self.assertEqual(appointment.no_show_risk, 0.0)
        self.assertEqual(appointment.appointment_id, 42)

    def test_unavailable_model_skips_metrics_and_date_parsing(self):
        self.service._predictor = UnavailableModelPredictor()
        self.assertEqual(self.book().no_show_risk, 0.0)
        self.session.patient_metrics.assert_not_called()

    def test_legacy_date_parse_failure_skips_prediction(self):
        with self.assertLogs("services.booking", level="WARNING"):
            self.assertEqual(self.book("2026-11-20T09:00").no_show_risk, 0.0)
        self.session.patient_metrics.assert_not_called()

    def test_past_date_clamps_lead_time_to_zero(self):
        self.book("2026-11-17 09:00")
        self.assertEqual(self.predictor.predict_probability.call_args.args[0].lead_time_days, 0)

    def test_save_failure_propagates_and_rolls_back(self):
        self.session.save.side_effect = RuntimeError("insert failed")
        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            self.book()
        self.assertEqual(self.events, ["begin", "rollback"])


class AlgorithmBoundaryTests(unittest.TestCase):
    def test_checked_in_model_matches_original_inference(self):
        import joblib
        directory = Path(__file__).resolve().parents[1]
        model = joblib.load(directory / "noshow_model.pkl")
        scaler = joblib.load(directory / "scaler.pkl")
        adapter = SklearnNoShowAdapter(model, scaler)
        for row in ([30, 5, 2, 0], [25, 45, 60, 3], [70, 10, 0, 1]):
            with self.subTest(features=row):
                expected = float(round(model.predict_proba(scaler.transform(np.array([row])))[0][0] * 100, 2))
                self.assertEqual(adapter.predict_probability(PredictionFeatures(*row)), expected)

    def test_adapter_preserves_feature_order_scaling_and_percentage(self):
        scaler, model = Mock(), Mock()
        scaler.transform.return_value = np.array([[1, 2, 3, 4]])
        model.predict_proba.return_value = np.array([[0.123456, 0.876544]])
        adapter = SklearnNoShowAdapter(model, scaler)
        self.assertEqual(adapter.predict_probability(PredictionFeatures(30, 5.5, 2, 1)), 12.35)
        np.testing.assert_array_equal(scaler.transform.call_args.args[0], [[30, 5.5, 2, 1]])
        np.testing.assert_array_equal(model.predict_proba.call_args.args[0], [[1, 2, 3, 4]])

    def test_triage_known_scores_unchanged(self):
        engine = TriageEngine()
        for symptom, text, expected in [
            ("Routine Checkup", "", 1), ("Mild Pain", "diabetic", 5),
            ("Routine Checkup", "cardiac", 7), ("Chest Pain", "asthma", 10),
            ("unknown", "", 2),
        ]:
            with self.subTest(symptom=symptom, text=text):
                self.assertEqual(engine.assess_severity(symptom, text), expected)


class AppointmentRepositoryTests(unittest.TestCase):
    def test_metrics_and_save_share_connection_and_commit(self):
        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.fetchone.side_effect = [{"age": 30}, {"appointment_id": 42}]
        repo = PostgresAppointmentRepository(lambda: connection)
        with repo.transaction() as session:
            self.assertEqual(session.patient_metrics("p"), {"age": 30})
            self.assertEqual(session.save(Appointment("p", "5", "date")), 42)
        self.assertEqual(cursor.execute.call_args.args[1], ("p", "5", "date", "scheduled", 1, 0.0))
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    def test_failed_commit_rolls_back_and_closes(self):
        connection = Mock()
        connection.cursor.return_value.fetchone.return_value = {"appointment_id": 42}
        connection.commit.side_effect = RuntimeError("commit failed")
        repo = PostgresAppointmentRepository(lambda: connection)
        with self.assertRaises(RuntimeError):
            with repo.transaction() as session:
                session.save(Appointment("p", "5", "date"))
        connection.rollback.assert_called_once()
        connection.cursor.return_value.close.assert_called_once()
        connection.close.assert_called_once()


class BookingAPITests(unittest.TestCase):
    def setUp(self):
        self.service = Mock(spec=BookingService)
        self.service.book.return_value = Appointment("p", "5", "date", appointment_id=42)
        self.service.book_with_triage.return_value = Appointment("p", "5", "date", priority_score=7,
                                                                appointment_id=42)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_booking_service] = lambda: self.service
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_standard_json_contract(self):
        response = self.client.post("/api/appointments/book", json={"patient_id": "p", "appointment_time": "date"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success", "appointment_id": 42})
        appointment = self.service.book.call_args.args[0]
        self.assertEqual((appointment.doctor_id, appointment.status), ("1", "scheduled"))

    def test_multipart_contract_and_uploaded_bytes(self):
        response = self.client.post("/api/appointments/book-with-triage",
                                    data={"patient_id": "p", "doctor_id": "5", "appointment_time": "date",
                                          "current_symptom": "Mild Pain"},
                                    files={"file": ("history.txt", b"history", "text/plain")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success", "appointment_id": 42,
                                          "assigned_priority": 7, "message": "Appointment booked successfully."})
        self.service.book_with_triage.assert_called_once_with("p", "5", "date", "Mild Pain", b"history", "history.txt")

    def test_persistence_error_retains_api_error_contract(self):
        self.service.book.side_effect = RuntimeError("database failed")
        response = self.client.post("/api/appointments/book", json={"patient_id": "p", "appointment_time": "date"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Failed to book"})


if __name__ == "__main__":
    unittest.main()
