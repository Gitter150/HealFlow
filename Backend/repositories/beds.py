from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Callable, Iterator

from domain.bed import Bed, BedUnavailableError


class BedRepository(ABC):
    """Allocation must be atomic, including the availability check."""

    @abstractmethod
    def list_beds(self) -> list[Bed]:
        raise NotImplementedError

    @abstractmethod
    def allocate(self, bed_id: int, patient_id: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def release(self, bed_id: int) -> None:
        raise NotImplementedError


class PostgresBedRepository(BedRepository):
    """Owns connection lifetime and keeps check/update in one transaction."""

    def __init__(self, connection_factory: Callable):
        self._connection_factory = connection_factory

    @contextmanager
    def _cursor(self, *, write: bool = False) -> Iterator:
        connection = self._connection_factory()
        cursor = None
        try:
            cursor = connection.cursor()
            yield cursor
            if write:
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

    def list_beds(self) -> list[Bed]:
        with self._cursor() as cursor:
            cursor.execute("""
                SELECT bed_id, ward_type, is_occupied, current_patient_id
                FROM beds ORDER BY bed_id;
            """)
            return [Bed(row["bed_id"], row["ward_type"], row["is_occupied"],
                        row["current_patient_id"]) for row in cursor.fetchall()]

    def allocate(self, bed_id: int, patient_id: int) -> None:
        with self._cursor(write=True) as cursor:
            # Preserve NOWAIT: another allocator must fail instead of double-assigning.
            cursor.execute("""
                SELECT bed_id, ward_type FROM beds
                WHERE bed_id = %s AND is_occupied = FALSE
                FOR UPDATE NOWAIT;
            """, (bed_id,))
            row = cursor.fetchone()
            if row is None:
                raise BedUnavailableError("Bed is already occupied or unavailable")
            bed = Bed(row["bed_id"], row["ward_type"])
            bed.assign_patient(patient_id)
            cursor.execute("""
                UPDATE beds SET is_occupied = TRUE, current_patient_id = %s
                WHERE bed_id = %s;
            """, (bed.patient_id, bed.bed_id))

    def release(self, bed_id: int) -> None:
        # Preserve the existing idempotent release contract, including missing IDs.
        with self._cursor(write=True) as cursor:
            cursor.execute("""
                UPDATE beds SET is_occupied = FALSE, current_patient_id = NULL
                WHERE bed_id = %s;
            """, (bed_id,))
