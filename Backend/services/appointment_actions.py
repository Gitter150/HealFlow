"""Commands and Factory Method creators for existing appointment actions."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from domain.appointment_change import AppointmentChange
from repositories.appointment_actions import AppointmentActionRepository


class InvalidAppointmentAction(ValueError):
    pass


class AppointmentCommand(ABC):
    @abstractmethod
    def execute(self, repository: AppointmentActionRepository, appointment_id: int) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class ChangeStatusCommand(AppointmentCommand):
    status: str
    message: str
    clear_overbooking: bool = False

    def execute(self, repository: AppointmentActionRepository, appointment_id: int) -> str:
        repository.apply(appointment_id, AppointmentChange(
            status=self.status, is_overbooked=False if self.clear_overbooking else None))
        return self.message


@dataclass(frozen=True)
class FlagOverbookingCommand(AppointmentCommand):
    def execute(self, repository: AppointmentActionRepository, appointment_id: int) -> str:
        repository.apply(appointment_id, AppointmentChange(is_overbooked=True))
        return "Slot marked for double booking. Patient remains scheduled."


@dataclass(frozen=True)
class RecordArrivalCommand(AppointmentCommand):
    clock: Callable[[], datetime] = datetime.now

    def execute(self, repository: AppointmentActionRepository, appointment_id: int) -> str:
        repository.apply(appointment_id, AppointmentChange(
            status="entered_hospital", entry_time=self.clock()))
        return "Patient marked as arrived."


class AppointmentActionService(ABC):
    """Creator: shared execution delegates product creation to a subclass."""

    def __init__(self, repository: AppointmentActionRepository):
        self._repository = repository

    @abstractmethod
    def create_command(self, action: str, *, status: str | None = None) -> AppointmentCommand:
        raise NotImplementedError

    def execute(self, appointment_id: int, action: str, *, status: str | None = None) -> str:
        command = self.create_command(action, status=status)
        return command.execute(self._repository, appointment_id)


class AdminAppointmentActions(AppointmentActionService):
    def create_command(self, action: str, *, status: str | None = None) -> AppointmentCommand:
        if action == "confirm":
            return ChangeStatusCommand("confirmed", "Appointment locked and confirmed.", True)
        if action == "double_book":
            return FlagOverbookingCommand()
        if action == "dismiss":
            return ChangeStatusCommand("reschedule_requested", "Patient dismissed. Warning sent to their dashboard.")
        raise InvalidAppointmentAction("Invalid action.")


class ClinicalAppointmentActions(AppointmentActionService):
    def __init__(self, repository: AppointmentActionRepository,
                 clock: Callable[[], datetime] = datetime.now):
        super().__init__(repository)
        self._clock = clock

    def create_command(self, action: str, *, status: str | None = None) -> AppointmentCommand:
        if action == "arrive":
            return RecordArrivalCommand(self._clock)
        if action == "cancel":
            return ChangeStatusCommand("cancelled", "Appointment cancelled successfully")
        if action == "consult":
            return ChangeStatusCommand("in_consultation", "Patient moved to consultation!")
        if action in ("complete", "doctor_complete"):
            return ChangeStatusCommand("completed", "Appointment marked as completed!",
                                       clear_overbooking=action == "doctor_complete")
        if action == "set_status" and status is not None:
            return ChangeStatusCommand(status, f"Status updated to {status}")
        raise InvalidAppointmentAction("Invalid action.")
