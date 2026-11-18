import unittest
from datetime import datetime
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from domain.appointment_change import AppointmentChange
from repositories.appointment_actions import PostgresAppointmentActionRepository
from services.appointment_actions import (
    AdminAppointmentActions, ClinicalAppointmentActions, InvalidAppointmentAction,
    ChangeStatusCommand, FlagOverbookingCommand, RecordArrivalCommand,
)
from routers import admin_routes, appointment_routes, doctor_routes


class CommandTests(unittest.TestCase):
    def test_admin_factory_products_and_effects(self):
        repository = Mock()
        service = AdminAppointmentActions(repository)
        for action, command_type, change in [
            ("confirm", ChangeStatusCommand, AppointmentChange("confirmed", False)),
            ("double_book", FlagOverbookingCommand, AppointmentChange(is_overbooked=True)),
            ("dismiss", ChangeStatusCommand, AppointmentChange("reschedule_requested")),
        ]:
            with self.subTest(action=action):
                self.assertIsInstance(service.create_command(action), command_type)
                service.execute(42, action)
                repository.apply.assert_called_with(42, change)

    def test_clinical_factory_effects_preserve_endpoint_differences(self):
        repository = Mock()
        now = datetime(2026, 11, 18, 10, 30)
        service = ClinicalAppointmentActions(repository, lambda: now)
        self.assertIsInstance(service.create_command("arrive"), RecordArrivalCommand)
        for action, change in [
            ("arrive", AppointmentChange("entered_hospital", entry_time=now)),
            ("cancel", AppointmentChange("cancelled")),
            ("consult", AppointmentChange("in_consultation")),
            ("complete", AppointmentChange("completed")),
            ("doctor_complete", AppointmentChange("completed", False)),
        ]:
            with self.subTest(action=action):
                service.execute(42, action)
                repository.apply.assert_called_with(42, change)

    def test_generic_status_preserves_existing_unrestricted_contract(self):
        repository = Mock()
        ClinicalAppointmentActions(repository).execute(42, "set_status", status="No Show")
        repository.apply.assert_called_once_with(42, AppointmentChange("No Show"))

    def test_invalid_action_does_not_touch_persistence(self):
        for creator in (AdminAppointmentActions, ClinicalAppointmentActions):
            repository = Mock()
            with self.assertRaises(InvalidAppointmentAction):
                creator(repository).execute(42, "not_an_action")
            repository.apply.assert_not_called()


class ActionRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.cursor = self.connection.cursor.return_value
        self.repository = PostgresAppointmentActionRepository(lambda: self.connection)

    def test_partial_update_does_not_overwrite_unmentioned_columns(self):
        self.repository.apply(42, AppointmentChange(is_overbooked=True))
        sql, parameters = self.cursor.execute.call_args.args
        self.assertEqual(sql, "UPDATE appointments SET is_overbooked = %s WHERE appointment_id = %s")
        self.assertEqual(parameters, (True, 42))
        self.connection.commit.assert_called_once()
        self.connection.close.assert_called_once()

    def test_false_is_not_omitted_and_status_is_bound_as_data(self):
        status = "completed'; DROP TABLE appointments; --"
        self.repository.apply(42, AppointmentChange(status, False))
        sql, parameters = self.cursor.execute.call_args.args
        self.assertNotIn(status, sql)
        self.assertEqual(parameters, (status, False, 42))

    def test_database_failure_rolls_back_and_closes(self):
        self.cursor.execute.side_effect = RuntimeError("write failed")
        with self.assertRaises(RuntimeError):
            self.repository.apply(42, AppointmentChange("cancelled"))
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_cursor_creation_failure_closes_connection(self):
        self.connection.cursor.side_effect = RuntimeError("cursor failed")
        with self.assertRaises(RuntimeError):
            self.repository.apply(42, AppointmentChange("cancelled"))
        self.connection.rollback.assert_called_once()
        self.connection.close.assert_called_once()

    def test_empty_change_rejected_without_connection(self):
        factory = Mock()
        with self.assertRaises(ValueError):
            PostgresAppointmentActionRepository(factory).apply(42, AppointmentChange())
        factory.assert_not_called()


class ActionAPITests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.now = datetime(2026, 11, 18, 10)
        app = FastAPI()
        for module in (admin_routes, appointment_routes, doctor_routes):
            app.include_router(module.router)
        app.dependency_overrides[admin_routes.get_action_service] = lambda: AdminAppointmentActions(self.repository)
        for module in (appointment_routes, doctor_routes):
            app.dependency_overrides[module.get_action_service] = lambda: ClinicalAppointmentActions(self.repository, lambda: self.now)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_all_clinical_routes_and_messages(self):
        for path, message in [
            ("/api/appointments/42/arrive", "Patient marked as arrived."),
            ("/api/appointments/42/cancel", "Appointment cancelled successfully"),
            ("/api/appointments/42/consult", "Patient moved to consultation!"),
            ("/api/appointments/42/complete", "Appointment marked as completed!"),
            ("/api/doctors/appointments/42/complete", "Appointment marked as completed!"),
        ]:
            with self.subTest(path=path):
                response = self.client.put(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "success", "message": message})

    def test_admin_action_messages(self):
        for action, message in [
            ("confirm", "Appointment locked and confirmed."),
            ("double_book", "Slot marked for double booking. Patient remains scheduled."),
            ("dismiss", "Patient dismissed. Warning sent to their dashboard."),
        ]:
            response = self.client.put("/api/admin/appointments/42/action", json={"action": action})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "success", "message": message})

    def test_invalid_admin_action_returns_intended_400(self):
        response = self.client.put("/api/admin/appointments/42/action", json={"action": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Invalid action."})
        self.repository.apply.assert_not_called()

    def test_generic_status_response(self):
        response = self.client.put("/api/appointments/42/status", json={"status": "confirmed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success", "message": "Status updated to confirmed"})

    def test_write_error_contracts(self):
        self.repository.apply.side_effect = RuntimeError("write failed")
        for path, payload, detail in [
            ("/api/admin/appointments/42/action", {"action": "confirm"}, "Failed to update appointment"),
            ("/api/appointments/42/status", {"status": "confirmed"}, "Failed to update status"),
            ("/api/appointments/42/cancel", None, "Failed to cancel appointment"),
        ]:
            response = self.client.put(path, json=payload)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json(), {"detail": detail})


if __name__ == "__main__":
    unittest.main()
