"""Capability guards against a fake nc client (no network).

The frozen contract under test: each guard probes the instance through
the impersonated direct-editing route (ok / missing / unknown), the
warn-once latch warns exactly once per guard and re-arms on recovery,
unknown probes (errors) never warn, and the sweep is thread-safe and
never raises. The healthy/missing shapes mirror a live instance's answers
(see marp_slides/capability_guards.py's module docstring for the
mechanism evidence).
"""

from __future__ import annotations

import logging
import threading

import pytest
from nc_py_api import NextcloudException, NextcloudExceptionNotFound

from marp_slides import capability_guards
from marp_slides.capability_guards import GUARDS, sweep_capability_guards
from marp_slides.settings import MARKDOWN_MIME_TYPE

#: A live instance's healthy shape: the text app's editor covers markdown (and
#: more), richdocuments the office formats.
HEALTHY_EDITORS = {
    "text": [MARKDOWN_MIME_TYPE, "text/plain", "application/yaml"],
    "richdocuments": ["application/vnd.oasis.opendocument.text"],
}

#: An instance with an office suite but no markdown/text editor: the
#: motivating incident's shape (the delivered .md cannot be opened).
OFFICE_ONLY_EDITORS = {"richdocuments": ["application/vnd.oasis.opendocument.text"]}

DIRECT_EDITING_PATH = "/ocs/v2.php/apps/files/api/v1/directEditing"


class FakeNc:
    """The client surface the probes touch: one OCS route. ``editors``
    configures the direct-editing answer (editor id -> mimetypes),
    ``error`` raises instead of answering, ``payload`` overrides the
    whole response body (malformed-shape tests)."""

    def __init__(
        self,
        editors: dict[str, list[str]] | None = None,
        error: Exception | None = None,
        payload: object = None,
    ) -> None:
        self._editors = HEALTHY_EDITORS if editors is None else editors
        self._error = error
        self._payload = payload
        self.ocs_calls: list[tuple[str, str]] = []

    def ocs(self, method: str, path: str, **kwargs):
        self.ocs_calls.append((method, path))
        if self._error is not None:
            raise self._error
        if self._payload is not None:
            return self._payload
        return {
            "editors": {
                editor: {"mimetypes": mimetypes}
                for editor, mimetypes in self._editors.items()
            }
        }


@pytest.fixture(autouse=True)
def _fresh_latch():
    """The warn-once latch is process state; every test starts clean and
    leaves it clean (leakage would silence other tests' warnings)."""
    capability_guards._WARNED.clear()
    yield
    capability_guards._WARNED.clear()


def guard_records(caplog, levelname: str, guard_name: str) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelname == levelname
        and r.name == "marp_slides.capability_guards"
        and guard_name in r.getMessage()
    ]


def test_registry_pins_the_two_initial_guards():
    assert [g.name for g in GUARDS] == ["files_app", "markdown_editor"]


def test_probes_hit_the_direct_editing_route():
    nc = FakeNc()
    sweep_capability_guards(nc)
    assert nc.ocs_calls == [("GET", DIRECT_EDITING_PATH)] * len(GUARDS)


def test_healthy_instance_stays_silent(caplog):
    caplog.set_level(logging.INFO, logger="marp_slides.capability_guards")
    sweep_capability_guards(FakeNc())
    sweep_capability_guards(FakeNc())
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "INFO")], (
        "a healthy instance must not log a single guard line"
    )


def test_missing_markdown_editor_warns_exactly_once(caplog):
    caplog.set_level(logging.INFO, logger="marp_slides.capability_guards")
    sweep_capability_guards(FakeNc(editors=OFFICE_ONLY_EDITORS))
    sweep_capability_guards(FakeNc(editors=OFFICE_ONLY_EDITORS))
    warnings = guard_records(caplog, "WARNING", "markdown_editor")
    assert len(warnings) == 1, "warn-once: the second failing sweep stays silent"
    message = warnings[0].getMessage()
    assert "delivered deck sources (.md) cannot be opened or edited" in message
    assert "enable an app that edits text/markdown in Files" in message
    # the route ANSWERED (files app healthy), so files_app must not warn
    assert guard_records(caplog, "WARNING", "files_app") == []


def test_recovery_logs_info_once_and_rearms_the_warning(caplog):
    caplog.set_level(logging.INFO, logger="marp_slides.capability_guards")
    nc_gap = FakeNc(editors=OFFICE_ONLY_EDITORS)
    sweep_capability_guards(nc_gap)
    sweep_capability_guards(FakeNc())  # admin enabled the text app
    recovered = guard_records(caplog, "INFO", "markdown_editor")
    assert len(recovered) == 1
    assert "recovered" in recovered[0].getMessage()
    # a later regression warns again: recovery re-armed the latch
    sweep_capability_guards(nc_gap)
    sweep_capability_guards(nc_gap)
    assert len(guard_records(caplog, "WARNING", "markdown_editor")) == 2


def test_route_gone_fails_both_guards_once_each(caplog):
    caplog.set_level(logging.INFO, logger="marp_slides.capability_guards")
    sweep_capability_guards(FakeNc(error=NextcloudExceptionNotFound("no route")))
    sweep_capability_guards(FakeNc(error=NextcloudExceptionNotFound("no route")))
    assert len(guard_records(caplog, "WARNING", "files_app")) == 1
    assert len(guard_records(caplog, "WARNING", "markdown_editor")) == 1


def test_http_level_404_also_means_route_gone(caplog):
    """An HTTP-level 404 arrives as a plain NextcloudException (only the
    OCS-meta 404 becomes NextcloudExceptionNotFound); the probe must
    read it as the missing files-app route, not as unknown."""
    caplog.set_level(logging.INFO, logger="marp_slides.capability_guards")
    sweep_capability_guards(FakeNc(error=NextcloudException(404, reason="Not Found")))
    assert len(guard_records(caplog, "WARNING", "files_app")) == 1
    assert len(guard_records(caplog, "WARNING", "markdown_editor")) == 1


def test_probe_error_is_unknown_never_warns_and_keeps_the_latch(caplog):
    caplog.set_level(logging.DEBUG, logger="marp_slides.capability_guards")
    # 401 is exactly the worker-startup state: no impersonated user
    sweep_capability_guards(
        FakeNc(error=NextcloudException(401, reason="Unauthorized"))
    )
    assert guard_records(caplog, "WARNING", "markdown_editor") == []
    unknown = guard_records(caplog, "DEBUG", "markdown_editor")
    assert len(unknown) == 1
    assert "unknown" in unknown[0].getMessage()
    # unknown left the latch untouched: the first definitive failure
    # (not the earlier unknown) is what warns
    sweep_capability_guards(FakeNc(editors=OFFICE_ONLY_EDITORS))
    assert len(guard_records(caplog, "WARNING", "markdown_editor")) == 1


def test_unexpected_probe_exception_is_unknown_and_sweep_never_raises(caplog):
    caplog.set_level(logging.DEBUG, logger="marp_slides.capability_guards")
    sweep_capability_guards(FakeNc(error=RuntimeError("probe bug")))
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    assert len(guard_records(caplog, "DEBUG", "files_app")) == 1


def test_malformed_payload_is_unknown_not_a_verdict(caplog):
    caplog.set_level(logging.DEBUG, logger="marp_slides.capability_guards")
    sweep_capability_guards(FakeNc(payload=["not", "a", "dict"]))
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    assert len(guard_records(caplog, "DEBUG", "files_app")) == 1


def test_concurrent_sweeps_warn_exactly_once(caplog):
    """Thread-safety smoke: the worker and delivery threads sweep
    concurrently; the latch must still warn exactly once."""
    caplog.set_level(logging.INFO, logger="marp_slides.capability_guards")
    barrier = threading.Barrier(8, timeout=10)

    def sweep_concurrently() -> None:
        barrier.wait()
        sweep_capability_guards(FakeNc(editors=OFFICE_ONLY_EDITORS))

    threads = [threading.Thread(target=sweep_concurrently) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(guard_records(caplog, "WARNING", "markdown_editor")) == 1
