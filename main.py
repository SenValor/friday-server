"""
main.py
-------
Railway entry-point.
Sadece server.py'deki FastAPI app'ini re-export eder.
Uvicorn: `uvicorn main:app --host 0.0.0.0 --port $PORT`
"""

from server import app  # noqa: F401  — Railway uvicorn main:app kullanır

__all__ = ["app"]
