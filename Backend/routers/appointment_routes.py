from repositories.appointment_actions import PostgresAppointmentActionRepository
from services.appointment_actions import ClinicalAppointmentActions, InvalidAppointmentAction
from fastapi import APIRouter, HTTPException, File, Form, UploadFile, Depends
from pydantic import BaseModel
from typing import Optional
import psycopg2
import psycopg2.extras
import os
from dotenv import load_dotenv
from datetime import datetime
from optimizer import ScheduleOptimizer

# Import Phase 2 Triage Logic
from domain.appointment import Appointment
from repositories.appointments import PostgresAppointmentRepository
from services.booking import BookingService
from services.prediction import load_booking_predictor
from services.triage import TriageEngine
from functools import lru_cache

# Import Phase 5 Simulator
from monte_carlo import MonteCarloSimulator

load_dotenv()
DB_URL = os.getenv("DATABASE_URL")

router = APIRouter(prefix="/api/appointments", tags=["Appointment Scheduling"])

@lru_cache(maxsize=1)
def get_booking_predictor():
    return load_booking_predictor()


def get_booking_service() -> BookingService:
    repository = PostgresAppointmentRepository(get_db_connection)
    return BookingService(repository.transaction, get_booking_predictor(), TriageEngine())


def get_db_connection():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)




def get_action_service():
    return ClinicalAppointmentActions(PostgresAppointmentActionRepository(get_db_connection))

# ==========================================
# PHASE 4: LIVE PRIORITY QUEUE & RECEPTIONIST (GLOBAL/ADMIN)
# ==========================================
@router.put("/{appointment_id}/arrive")
def mark_patient_arrived(appointment_id: int, service: ClinicalAppointmentActions = Depends(get_action_service)):
    try:
        message = service.execute(appointment_id, "arrive")
        return {"status": "success", "message": message}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/live-queue")
def get_live_priority_queue():
    """Calculates effective priority dynamically based on wait time for the Admin Dashboard."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Fetch only patients who are physically in the waiting room
        query = """
            SELECT a.appointment_id, a.patient_id, COALESCE(p.full_name, 'Unknown') as patient_name, 
                   a.doctor_id, a.priority_score as base_priority, a.entry_time
            FROM appointments a
            LEFT JOIN patient_profiles p ON a.patient_id = p.uid
            WHERE a.status = 'entered_hospital'
        """
        cursor.execute(query)
        waiting_patients = cursor.fetchall()

        now = datetime.now()
        queue_response = []

        for patient in waiting_patients:
            entry_time = patient['entry_time']
            wait_time_minutes = 0
            effective_priority = patient['base_priority'] or 1

            if entry_time:
                # Strip hidden timezone data to prevent crashes/offsets
                safe_entry_time = entry_time.replace(tzinfo=None)
                
                # Calculate true minutes passed
                wait_time_delta = now - safe_entry_time
                wait_time_minutes = int(wait_time_delta.total_seconds() / 60)
                
                # Prevent negative wait times if clocks drift by a millisecond
                wait_time_minutes = max(0, wait_time_minutes)
                
                # Add 0.1 priority points for every minute waiting
                effective_priority = effective_priority + (wait_time_minutes * 0.1)

            # Package it for the frontend
            patient_data = dict(patient)
            patient_data['wait_time_minutes'] = wait_time_minutes
            patient_data['effective_priority'] = round(effective_priority, 2)
            
            # Serialize datetime for JSON
            if patient_data['entry_time']:
                patient_data['entry_time'] = safe_entry_time.isoformat()
            
            queue_response.append(patient_data)

        # THE SORT: Sort primarily by Effective Priority (Highest first), then by longest wait time
        queue_response.sort(key=lambda x: (x['effective_priority'], x['wait_time_minutes']), reverse=True)

        return {"status": "success", "data": queue_response}

    except Exception as e:
        print(f"🔥 Error fetching live queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


# ==========================================
# ACTUAL DATABASE BOOKING ROUTE (Standard)
# ==========================================
class AppointmentCreate(BaseModel):
    patient_id: str          
    doctor_id: str = "1"     
    appointment_time: str    
    status: str = "scheduled"
    priority_score: int = 1  
    no_show_risk: float = 0.0

@router.post("/book")
def book_appointment(data: AppointmentCreate,
                     service: BookingService = Depends(get_booking_service)):
    try:
        appointment = service.book(Appointment(**data.model_dump()))
        return {"status": "success", "appointment_id": appointment.appointment_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to book") from exc

# ==========================================
# PHASE 2 & 3: INTELLIGENT TRIAGE BOOKING ROUTE
# ==========================================
@router.post("/book-with-triage")
async def book_appointment_triage(
    patient_id: str = Form(...),
    doctor_id: str = Form(...),
    appointment_time: str = Form(...),
    current_symptom: str = Form(...),
    file: Optional[UploadFile] = File(None),
    service: BookingService = Depends(get_booking_service),
):
    try:
        content = await file.read() if file else None
        appointment = service.book_with_triage(
            patient_id, doctor_id, appointment_time, current_symptom,
            content, (file.filename or "") if file else "",
        )
        return {
            "status": "success",
            "appointment_id": appointment.appointment_id,
            "assigned_priority": appointment.priority_score,
            "message": "Appointment booked successfully.",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to process triage booking.") from exc


@router.put("/{appointment_id}/cancel")
def cancel_appointment(appointment_id: int, service: ClinicalAppointmentActions = Depends(get_action_service)):
    try:
        message = service.execute(appointment_id, "cancel")
        return {"status": "success", "message": message}
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to cancel appointment") from exc

# ==========================================
# PHASE 7: CALL PATIENT ROUTE (Doctor Dashboard)
# ==========================================
@router.put("/{appointment_id}/consult")
def consult_appointment(appointment_id: int, service: ClinicalAppointmentActions = Depends(get_action_service)):
    try:
        message = service.execute(appointment_id, "consult")
        return {"status": "success", "message": message}
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to call patient") from exc

# ==========================================
# COMPLETE APPOINTMENT ROUTE (Doctor Dashboard)
# ==========================================
@router.put("/{appointment_id}/complete")
def complete_appointment(appointment_id: int, service: ClinicalAppointmentActions = Depends(get_action_service)):
    try:
        message = service.execute(appointment_id, "complete")
        return {"status": "success", "message": message}
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to complete appointment") from exc

# ==========================================
# UPDATE APPOINTMENT STATUS (Admin Confirm/Reschedule)
# ==========================================
class UpdateStatusRequest(BaseModel):
    status: str

@router.put("/{appointment_id}/status")
def update_appointment_status(appointment_id: int, request: UpdateStatusRequest,
                              service: ClinicalAppointmentActions = Depends(get_action_service)):
    try:
        message = service.execute(appointment_id, "set_status", status=request.status)
        return {"status": "success", "message": message}
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to update status") from exc

# ==========================================
# GET APPOINTMENTS FOR PATIENT HISTORY TAB
# ==========================================
@router.get("/patient/{patient_id}")
def get_patient_appointments(patient_id: str):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        query = """
            SELECT 
                appointment_id as id, 
                CAST(appointment_time AS TEXT) as date, 
                status, 
                CASE 
                    WHEN doctor_id = '1' THEN 'Dr. Sarah Chen'
                    WHEN doctor_id = '2' THEN 'Dr. James Wilson'
                    WHEN doctor_id = '3' THEN 'Dr. Emily Park'
                    WHEN doctor_id = '4' THEN 'Dr. Michael Ross'
                    WHEN doctor_id = '5' THEN 'Dr. Robert King'
                    WHEN doctor_id = '6' THEN 'Dr. Lisa Cuddy'
                    ELSE 'Dr. ' || doctor_id 
                END as doctor, 
                CASE 
                    WHEN doctor_id = '1' THEN 'Cardiology'
                    WHEN doctor_id = '2' THEN 'General Medicine'
                    WHEN doctor_id = '3' THEN 'Orthopedics'
                    WHEN doctor_id = '4' THEN 'Dermatology'
                    WHEN doctor_id = '5' THEN 'Neurology'
                    WHEN doctor_id = '6' THEN 'Pediatrics'
                    ELSE 'General'
                END as department, 
                'Consultation' as diagnosis
            FROM appointments 
            WHERE patient_id = %s 
            ORDER BY appointment_time DESC;
        """
        cursor.execute(query, (patient_id,))
        records = cursor.fetchall()
        
        return {"status": "success", "data": records}
        
    except Exception as e:
        print(f"🔥 Error fetching history: {e}")
        return {"status": "error", "data": []}
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# ==========================================
# GET SPECIFIC DOCTOR'S LIVE WAITING ROOM
# ==========================================
@router.get("/doctor/{uid}/live-queue")
def get_doctor_live_queue(uid: str):
    """Fetches the live priority queue ONLY for a specific doctor based on their Firebase UID."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # JOIN the doctors and users tables to filter by the Firebase UID
        query = """
            SELECT a.appointment_id, a.patient_id, COALESCE(p.full_name, 'Unknown') as patient_name, 
                   a.doctor_id, a.priority_score as base_priority, a.entry_time
            FROM appointments a
            JOIN doctors d ON a.doctor_id::VARCHAR = d.doctor_id::VARCHAR
            JOIN users u ON u.email = d.email
            LEFT JOIN patient_profiles p ON a.patient_id = p.uid
            WHERE a.status = 'entered_hospital' AND u.uid = %s
        """
        cursor.execute(query, (uid,))
        waiting_patients = cursor.fetchall()

        now = datetime.now()
        queue_response = []

        for patient in waiting_patients:
            entry_time = patient['entry_time']
            wait_time_minutes = 0
            effective_priority = patient['base_priority'] or 1

            if entry_time:
                safe_entry_time = entry_time.replace(tzinfo=None)
                wait_time_delta = now - safe_entry_time
                wait_time_minutes = int(wait_time_delta.total_seconds() / 60)
                wait_time_minutes = max(0, wait_time_minutes)
                effective_priority = effective_priority + (wait_time_minutes * 0.1)

            patient_data = dict(patient)
            patient_data['wait_time_minutes'] = wait_time_minutes
            patient_data['effective_priority'] = round(effective_priority, 2)
            
            if patient_data['entry_time']:
                patient_data['entry_time'] = safe_entry_time.isoformat()
            
            queue_response.append(patient_data)

        # Sort by highest effective priority first
        queue_response.sort(key=lambda x: (x['effective_priority'], x['wait_time_minutes']), reverse=True)

        return {"status": "success", "data": queue_response}

    except Exception as e:
        print(f"🔥 Error fetching doctor live queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# ==========================================
# FETCH SPECIFIC DOCTOR'S APPOINTMENTS
# ==========================================
@router.get("/doctor/{uid}/appointments")
def get_doctor_appointments(uid: str):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        query = """
            SELECT 
                a.appointment_id, 
                COALESCE(p.full_name, 'Unknown Patient') as patient_name, 
                p.phone,
                p.blood_group,
                a.appointment_time, 
                a.status
            FROM appointments a
            JOIN doctors d ON a.doctor_id::VARCHAR = d.doctor_id::VARCHAR
            JOIN users u ON u.email = d.email
            LEFT JOIN patient_profiles p ON a.patient_id = p.uid
            WHERE u.uid = %s
            ORDER BY a.appointment_time ASC;
        """
        cursor.execute(query, (uid,))
        appointments = cursor.fetchall()

        for apt in appointments:
            if hasattr(apt['appointment_time'], 'strftime'):
                apt['appointment_time'] = apt['appointment_time'].strftime("%b %d, %Y - %I:%M %p")
                
        return {"status": "success", "data": appointments}
        
    except Exception as e:
        print(f"🔥 Error fetching appointments: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch appointments")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# ==========================================
# GET DOCTOR PROFILE
# ==========================================
@router.get("/doctor/{uid}/profile")
def get_doctor_profile(uid: str):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        query = """
            SELECT full_name, phone, department 
            FROM doctor_profiles 
            WHERE uid = %s;
        """
        cursor.execute(query, (uid,))
        profile = cursor.fetchone()
        
        return {"status": "success", "data": profile or {}}
    except Exception as e:
        print(f"🔥 Error fetching profile: {e}")
        return {"status": "error", "data": {}}
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# ==========================================
# UPDATE DOCTOR PROFILE
# ==========================================
class DoctorProfileUpdate(BaseModel):
    full_name: str
    phone: str
    department: str

@router.put("/doctor/{uid}/profile")
def update_doctor_profile(uid: str, data: DoctorProfileUpdate):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        query = """
            UPDATE doctor_profiles 
            SET full_name = %s, phone = %s, department = %s
            WHERE uid = %s;
        """
        cursor.execute(query, (data.full_name, data.phone, data.department, uid))
        
        cursor.execute("UPDATE users SET full_name = %s, phone = %s WHERE uid = %s;", 
                      (data.full_name, data.phone, uid))
        
        conn.commit()
        return {"status": "success", "message": "Profile updated successfully"}
    except Exception as e:
        print(f"🔥 Error updating profile: {e}")
        if conn: conn.rollback()
        raise HTTPException(status_code=500, detail="Failed to update profile")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# ==========================================
# PHASE 5: MONTE CARLO SIMULATION ENDPOINT
# ==========================================
@router.get("/simulate/{doctor_id}/{date_string}")
def run_monte_carlo_simulation(doctor_id: str, date_string: str):
    """
    Runs the simulation for a specific doctor on a specific date (e.g., '2026-05-04')
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Fetch tomorrow's scheduled/confirmed appointments for this doctor
        query = """
            SELECT appointment_id, appointment_time, priority_score, no_show_risk
            FROM appointments
            WHERE doctor_id = %s 
            AND DATE(appointment_time) = %s
            AND status IN ('scheduled', 'confirmed')
        """
        cursor.execute(query, (doctor_id, date_string))
        appointments = cursor.fetchall()
        
        if not appointments:
            return {"status": "success", "message": "No appointments found.", "data": None}

        # 2. Format the data for the Simulator (Convert time to minutes-since-midnight)
        schedule_data = []
        for apt in appointments:
            dt = apt['appointment_time']
            # e.g., 09:30 AM = (9 * 60) + 30 = 570 minutes
            scheduled_minute = (dt.hour * 60) + dt.minute 
            
            schedule_data.append({
                'id': apt['appointment_id'],
                'scheduled_minute': scheduled_minute,
                'base_priority': apt['priority_score'] or 1,
                'no_show_risk': float(apt['no_show_risk'] or 0.0)
            })

        # 3. Feed it to the Engine and run 1,000 times!
        simulator = MonteCarloSimulator(schedule_data)
        results = simulator.run_simulation(iterations=1000)

        return {
            "status": "success",
            "doctor_id": doctor_id,
            "date": date_string,
            "total_appointments": len(schedule_data),
            "simulation_results": results
        }

    except Exception as e:
        print(f"🔥 Error running simulation: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# ==========================================
# PHASE 6: SIMULATED ANNEALING OPTIMIZER
# ==========================================
@router.post("/optimize/{doctor_id}/{date_string}")
def optimize_doctor_schedule(doctor_id: str, date_string: str):
    """
    Runs Simulated Annealing to find the mathematically perfect order 
    for tomorrow's appointments.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Fetch tomorrow's appointments
        query = """
            SELECT appointment_id, appointment_time, priority_score, no_show_risk
            FROM appointments
            WHERE doctor_id = %s 
            AND DATE(appointment_time) = %s
            AND status IN ('scheduled', 'confirmed')
        """
        cursor.execute(query, (doctor_id, date_string))
        appointments = cursor.fetchall()
        
        if len(appointments) < 2:
            return {"status": "success", "message": "Not enough appointments to optimize.", "data": None}

        # 2. Format the data for the Optimizer
        schedule_data = []
        for apt in appointments:
            dt = apt['appointment_time']
            scheduled_minute = (dt.hour * 60) + dt.minute 
            
            schedule_data.append({
                'id': apt['appointment_id'],
                'scheduled_minute': scheduled_minute,
                'base_priority': apt['priority_score'] or 1,
                'no_show_risk': float(apt['no_show_risk'] or 0.0),
                'original_dt': dt # Keep this so we can update the DB later
            })

        # 3. Run the AI Optimizer!
        optimizer = ScheduleOptimizer(schedule_data, max_iterations=100)
        result = optimizer.run_optimization()
        
        best_schedule = result['optimized_schedule']

        # 4. Save the optimized times back to the Database
        update_query = """
            UPDATE appointments 
            SET appointment_time = %s 
            WHERE appointment_id = %s
        """
        
        for patient in best_schedule:
            # Reconstruct the datetime object from the new scheduled_minute
            old_dt = patient['original_dt']
            new_hour = patient['scheduled_minute'] // 60
            new_minute = patient['scheduled_minute'] % 60
            new_dt = old_dt.replace(hour=new_hour, minute=new_minute)
            
            cursor.execute(update_query, (new_dt, patient['id']))
            
        conn.commit()

        return {
            "status": "success",
            "message": "Schedule optimized perfectly!",
            "final_cost_score": result['final_cost']
        }

    except Exception as e:
        print(f"🔥 Error optimizing schedule: {e}")
        if conn: conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cursor: cursor.close()
        if conn: conn.close()