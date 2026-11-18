"""Offline domain, transaction, and HTTP regression checks (no live database)."""
import unittest
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from domain.bed import Bed, BedUnavailableError
from repositories.beds import BedRepository, PostgresBedRepository
from services.beds import BedService
from routers.bed_routes import router, get_bed_service


class BedDomainTests(unittest.TestCase):
    def test_allocate_reject_duplicate_then_release(self):
        bed = Bed(7, "ICU")
        bed.assign_patient(101)
        self.assertEqual((bed.is_occupied, bed.patient_id), (True, 101))
        with self.assertRaises(BedUnavailableError):
            bed.assign_patient(102)
        self.assertEqual(bed.patient_id, 101)
        bed.discharge_patient()
        self.assertTrue(bed.check_availability())
        self.assertIsNone(bed.patient_id)
        bed.discharge_patient()  # Idempotent.


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.cursor = self.connection.cursor.return_value
        self.repo = PostgresBedRepository(lambda: self.connection)

    def assert_closed(self):
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_allocate_locks_before_update_and_commits_once(self):
        self.cursor.fetchone.return_value = {"bed_id": 7, "ward_type": "ICU"}
        self.repo.allocate(7, 101)
        calls = self.cursor.execute.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("FOR UPDATE NOWAIT", calls[0].args[0])
        self.assertIn("is_occupied = FALSE", calls[0].args[0])
        self.assertEqual(calls[0].args[1], (7,))
        self.assertEqual(calls[1].args[1], (101, 7))
        self.connection.commit.assert_called_once()
        self.connection.rollback.assert_not_called()
        self.assert_closed()

    def test_unavailable_never_updates_and_rolls_back(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(BedUnavailableError):
            self.repo.allocate(7, 101)
        self.cursor.execute.assert_called_once()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.assert_closed()

    def test_update_failure_rolls_back_and_closes(self):
        self.cursor.fetchone.return_value = {"bed_id": 7, "ward_type": "ICU"}
        self.cursor.execute.side_effect = [None, RuntimeError("write failed")]
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            self.repo.allocate(7, 101)
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.assert_closed()

    def test_cursor_creation_failure_still_closes_connection(self):
        self.connection.cursor.side_effect = RuntimeError("cursor failed")
        with self.assertRaises(RuntimeError):
            self.repo.list_beds()
        self.connection.rollback.assert_called_once()
        self.connection.close.assert_called_once()

    def test_release_preserves_idempotent_update(self):
        self.repo.release(7)
        self.assertIn("current_patient_id = NULL", self.cursor.execute.call_args.args[0])
        self.assertEqual(self.cursor.execute.call_args.args[1], (7,))
        self.connection.commit.assert_called_once()
        self.assert_closed()

    def test_list_maps_database_columns_to_domain(self):
        self.cursor.fetchall.return_value = [
            {"bed_id": 7, "ward_type": "ICU", "is_occupied": True,
             "current_patient_id": 101}
        ]
        bed = self.repo.list_beds()[0]
        self.assertEqual((bed.bed_id, bed.ward_type, bed.patient_id), (7, "ICU", 101))
        self.assertTrue(bed.is_occupied)
        self.connection.commit.assert_not_called()
        self.assert_closed()


class BedAPITests(unittest.TestCase):
    def setUp(self):
        # Exercise real HTTP serialization + service, replacing only persistence.
        self.repository = Mock(spec=BedRepository)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_bed_service] = lambda: BedService(self.repository)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_existing_status_response_shape(self):
        self.repository.list_beds.return_value = [Bed(7, "ICU", True, 101)]
        response = self.client.get("/api/beds/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [
            {"id": 7, "ward": "ICU", "occupied": True, "patient": 101}
        ])

    def test_allocate_success_contract(self):
        response = self.client.post("/api/beds/allocate", json={"bed_id": 7, "patient_id": 101})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Bed allocated successfully", "bed_id": 7})
        self.repository.allocate.assert_called_once_with(7, 101)

    def test_unavailable_returns_intended_404_instead_of_500(self):
        self.repository.allocate.side_effect = BedUnavailableError()
        response = self.client.post("/api/beds/allocate", json={"bed_id": 7, "patient_id": 101})
        self.assertEqual(response.status_code, 404)

    def test_lock_collision_returns_409(self):
        class LockCollision(Exception):
            pgcode = "55P03"
        self.repository.allocate.side_effect = LockCollision()
        response = self.client.post("/api/beds/allocate", json={"bed_id": 7, "patient_id": 101})
        self.assertEqual(response.status_code, 409)

    def test_other_database_errors_return_500(self):
        self.repository.allocate.side_effect = RuntimeError("database unavailable")
        response = self.client.post("/api/beds/allocate", json={"bed_id": 7, "patient_id": 101})
        self.assertEqual(response.status_code, 500)

    def test_release_success_contract(self):
        response = self.client.post("/api/beds/release/7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Bed released successfully."})
        self.repository.release.assert_called_once_with(7)

    def test_invalid_request_does_not_reach_repository(self):
        response = self.client.post("/api/beds/allocate", json={"bed_id": "bad"})
        self.assertEqual(response.status_code, 422)
        self.repository.allocate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
