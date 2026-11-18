from dataclasses import dataclass


class BedUnavailableError(Exception):
    """The requested bed does not exist or is already occupied."""


@dataclass
class Bed:
    """A bed snapshot; concurrent allocation is enforced by the repository lock."""

    bed_id: int
    ward_type: str
    _is_occupied: bool = False
    _patient_id: int | None = None

    @property
    def is_occupied(self) -> bool:
        return self._is_occupied

    @property
    def patient_id(self) -> int | None:
        return self._patient_id

    def check_availability(self) -> bool:
        return not self._is_occupied

    def assign_patient(self, patient_id: int) -> None:
        if not self.check_availability():
            raise BedUnavailableError(f"Bed {self.bed_id} is already occupied")
        self._patient_id = patient_id
        self._is_occupied = True

    def discharge_patient(self) -> None:
        self._patient_id = None
        self._is_occupied = False
