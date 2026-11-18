from domain.bed import Bed
from repositories.beds import BedRepository


class BedService:
    """Application entry point for the admin bed-management use case."""

    def __init__(self, repository: BedRepository):
        self._repository = repository

    def list_beds(self) -> list[Bed]:
        return self._repository.list_beds()

    def assign_patient(self, bed_id: int, patient_id: int) -> None:
        self._repository.allocate(bed_id, patient_id)

    def discharge_patient(self, bed_id: int) -> None:
        self._repository.release(bed_id)
