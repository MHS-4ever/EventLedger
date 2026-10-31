# EventLedger

EventLedger is a reliable webhook and event-processing backend built with Python and FastAPI. This initial phase provides a minimal API with a health-check endpoint.

## Local development

Python 3.11 or newer is required. In PowerShell, run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Start the development server:

```powershell
python -m uvicorn app.main:app --reload
```

The health endpoint is available at `http://127.0.0.1:8000/health`.

Run the tests:

```powershell
python -m pytest
```
