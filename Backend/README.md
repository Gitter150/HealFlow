# Intelligent-health-hub----backend

## Incremental OOAD refactoring

### Run the application

Use Python 3.11 or newer. From the repository root, create the environment and
install dependencies with the commands below. For live database operations, set
`DATABASE_URL` in the process environment or in a local `Backend/.env` file using
the project's existing PostgreSQL database and schema. The submission archive
omits local environment files. Run the backend from `Backend`:

```powershell
../.venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

From the repository root, run `npm ci` followed by `npm run dev` in a separate
terminal for the React dashboard. Configure its Firebase project for authenticated
workflows. For a credential-free check, use the offline tests below.

Bed management now uses `domain/bed.py`, `services/beds.py`, and
`repositories/beds.py`. The existing `/api/beds/*` routes retain their successful
response contracts and PostgreSQL NOWAIT allocation locking. An unavailable bed
now returns the intended 404 rather than being accidentally converted to 500.

From the project root, create an isolated environment and install test dependencies:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r Backend/requirements-dev.txt
cd Backend
../.venv/Scripts/python -B -m unittest discover -s tests -v
```

These are offline tests with substituted persistence; no database credentials are
needed. They cover bed management and booking, including comparison of the
prediction adapter with the checked-in model's original inference calculation.
The model artifacts were saved with scikit-learn 1.6.1, which is pinned in the
runtime requirements. OCR of images additionally requires a Tesseract executable
on PATH; pip installs only its Python wrapper.

Booking now uses `domain/appointment.py`, `services/booking.py`,
`services/triage.py`, `services/prediction.py`, and
`repositories/appointments.py`. The original algorithm functions remain intact.
The booking model loads lazily from Backend and is cached. Its existing zero-risk
fallback on unavailable/failed inference is preserved, not a calibrated estimate.

Appointment status/admin actions now use
`services/appointment_actions.py` and `repositories/appointment_actions.py`.
The integrated patterns are Factory Method (admin/clinical command creators),
Command (status, overbooking and arrival operations), and Adapter
(`services/prediction.py`). Invalid admin actions now return the intended 400.
The current status API remains permissive; this is not an enforced state machine.
