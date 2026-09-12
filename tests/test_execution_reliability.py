import json
from types import SimpleNamespace

from app import execution
from app import execution_app


def test_recover_interrupted_is_null_safe_for_orphaned_references(monkeypatch):
    job = SimpleNamespace(
        status="SENDING", error=None, finished_at=None, delivery_id=991, attempt_id=992,
    )

    class Query:
        def filter_by(self, **_filters):
            return self

        def all(self):
            return [job]

    class FakeSession:
        committed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def query(self, _model):
            return Query()

        def get(self, _model, _identifier):
            return None

        def commit(self):
            self.committed = True

    session = FakeSession()
    monkeypatch.setattr(execution, "SessionLocal", lambda: session)

    execution.recover_interrupted()

    assert job.status == "UNCERTAIN"
    assert job.error == "WORKER_INTERRUPTED"
    assert job.finished_at is not None
    assert session.committed is True


def test_worker_heartbeat_has_grace_failure_recovery_and_staleness(monkeypatch):
    monkeypatch.setenv("MAAK_EXECUTION_STARTUP_GRACE_SECONDS", "5")
    monkeypatch.setenv("MAAK_EXECUTION_STALE_AFTER_SECONDS", "3")
    execution.worker_started(monotonic_now=100)

    assert execution.worker_health(monotonic_now=104)["state"] == "STARTING"
    execution.worker_failed(RuntimeError("private detail"), monotonic_now=106)
    failed = execution.worker_health(monotonic_now=106)
    assert failed["healthy"] is False
    assert failed["state"] == "FAILING"
    assert failed["last_error"] == "RuntimeError"
    assert "private detail" not in str(failed)

    execution.worker_succeeded(monotonic_now=107)
    assert execution.worker_health(monotonic_now=109)["state"] == "HEALTHY"
    assert execution.worker_health(monotonic_now=111)["state"] == "STALE"


def test_execution_health_returns_503_when_worker_is_not_live(monkeypatch):
    class EmptyQuery:
        def filter_by(self, **_filters):
            return self

        def count(self):
            return 0

    class EmptyDatabase:
        def query(self, _model):
            return EmptyQuery()

    monkeypatch.setattr(execution, "worker_health", lambda: {
        "healthy": False, "state": "STALE", "last_success_at": None,
        "last_error_at": None, "last_error": None,
    })
    response = execution_app.execution_health(db=EmptyDatabase())
    assert response.status_code == 503
    assert json.loads(response.body)["worker"]["state"] == "STALE"
