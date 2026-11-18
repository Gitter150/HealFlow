from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db_connection
from domain.bed import BedUnavailableError
from repositories.beds import PostgresBedRepository
from services.beds import BedService

router = APIRouter()


class BedAllocationRequest(BaseModel):
    bed_id: int
    patient_id: int


def get_bed_service() -> BedService:
    return BedService(PostgresBedRepository(get_db_connection))


@router.get("/api/beds/status")
def get_bed_status(service: BedService = Depends(get_bed_service)):
    try:
        return [
            {"id": bed.bed_id, "ward": bed.ward_type,
             "occupied": bed.is_occupied, "patient": bed.patient_id}
            for bed in service.list_beds()
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/beds/allocate")
def allocate_bed(request: BedAllocationRequest,
                 service: BedService = Depends(get_bed_service)):
    try:
        service.assign_patient(request.bed_id, request.patient_id)
        return {"message": "Bed allocated successfully", "bed_id": request.bed_id}
    except BedUnavailableError as exc:
        raise HTTPException(
            status_code=404,
            detail="Bed is already occupied or locked by another admin!",
        ) from exc
    except Exception as exc:
        if getattr(exc, "pgcode", None) == "55P03":
            raise HTTPException(
                status_code=409,
                detail="Database Lock: Another nurse is booking this exact bed right now!",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/beds/release/{bed_id}")
def release_bed(bed_id: int, service: BedService = Depends(get_bed_service)):
    try:
        service.discharge_patient(bed_id)
        return {"message": "Bed released successfully."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
