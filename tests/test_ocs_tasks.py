"""Nested core:text2text scheduling/polling (OCS) against a fake app client."""

from __future__ import annotations

import pytest

from marp_slides.ocs_tasks import (
    SCHEDULE_ENDPOINT,
    TASK_ENDPOINT,
    NestedTaskError,
    schedule_and_wait,
)


class FakeOcs:
    def __init__(self, schedule_response, poll_statuses, schedule_errors=0):
        self.schedule_response = schedule_response
        self.poll_statuses = list(poll_statuses)
        self.schedule_errors = schedule_errors
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, endpoint, json=None, **kwargs):
        self.calls.append((method, endpoint, json))
        if endpoint == SCHEDULE_ENDPOINT:
            from niquests import ConnectionError as RequestsConnectionError

            if self.schedule_errors > 0:
                self.schedule_errors -= 1
                raise RequestsConnectionError("no route to host")
            return self.schedule_response
        task = dict(self.poll_statuses.pop(0))
        return {"task": task}


def ok_poll(extra=None):
    task = {
        "id": 7,
        "status": "STATUS_SUCCESSFUL",
        "output": {"output": "the deck text"},
    }
    task.update(extra or {})
    return task


def test_schedule_and_wait_success() -> None:
    ocs = FakeOcs(
        schedule_response={"task": {"id": 7, "status": "STATUS_SCHEDULED"}},
        poll_statuses=[ok_poll()],
    )
    nc = type("FakeNc", (), {"ocs": staticmethod(ocs)})()
    output = schedule_and_wait(
        nc,
        "core:text2text",
        {"input": "hello"},
        "marp_slides",
        timeout_s=60,
        sleep=lambda s: None,
    )
    assert output == {"output": "the deck text"}
    method, endpoint, payload = ocs.calls[0]
    assert (method, endpoint) == (
        "POST",
        "/ocs/v1.php/taskprocessing/tasks_consumer/schedule",
    )
    assert payload == {
        "type": "core:text2text",
        "appId": "marp_slides",
        "input": {"input": "hello"},
    }
    assert ocs.calls[1][1] == TASK_ENDPOINT.format(task_id=7)


def test_polls_until_successful() -> None:
    ocs = FakeOcs(
        schedule_response={"task": {"id": 7, "status": "STATUS_SCHEDULED"}},
        poll_statuses=[
            {"id": 7, "status": "STATUS_RUNNING"},
            {"id": 7, "status": "STATUS_RUNNING"},
            ok_poll(),
        ],
    )
    nc = type("FakeNc", (), {"ocs": staticmethod(ocs)})()
    slept = []

    output = schedule_and_wait(
        nc,
        "core:text2text",
        {"input": "x"},
        "marp_slides",
        timeout_s=60,
        sleep=slept.append,
    )
    assert output == {"output": "the deck text"}
    assert len(slept) == 2


def test_failure_surfaces_user_facing_error() -> None:
    ocs = FakeOcs(
        schedule_response={"task": {"id": 7}},
        poll_statuses=[
            {
                "id": 7,
                "status": "STATUS_FAILED",
                "userFacingErrorMessage": "provider is offline",
            }
        ],
    )
    nc = type("FakeNc", (), {"ocs": staticmethod(ocs)})()
    with pytest.raises(NestedTaskError, match="provider is offline"):
        schedule_and_wait(
            nc, "core:text2text", {"input": "x"}, "marp_slides", sleep=lambda s: None
        )


def test_cancelled_is_an_error() -> None:
    ocs = FakeOcs(
        schedule_response={"task": {"id": 7}},
        poll_statuses=[{"id": 7, "status": "STATUS_CANCELLED"}],
    )
    nc = type("FakeNc", (), {"ocs": staticmethod(ocs)})()
    with pytest.raises(NestedTaskError, match="core:text2text"):
        schedule_and_wait(
            nc, "core:text2text", {"input": "x"}, "marp_slides", sleep=lambda s: None
        )


def test_timeout_cap_enforced() -> None:
    ocs = FakeOcs(
        schedule_response={"task": {"id": 7}},
        poll_statuses=[{"id": 7, "status": "STATUS_RUNNING"}] * 100,
    )
    nc = type("FakeNc", (), {"ocs": staticmethod(ocs)})()
    clock = iter(range(0, 10_000, 10)).__next__
    with pytest.raises(NestedTaskError, match="did not finish within 300"):
        schedule_and_wait(
            nc,
            "core:text2text",
            {"input": "x"},
            "marp_slides",
            timeout_s=300,
            sleep=lambda s: None,
            clock=clock,
        )


def test_schedule_retries_transient_errors() -> None:
    ocs = FakeOcs(
        schedule_response={"task": {"id": 7}},
        poll_statuses=[ok_poll()],
        schedule_errors=2,
    )
    nc = type("FakeNc", (), {"ocs": staticmethod(ocs)})()
    output = schedule_and_wait(
        nc, "core:text2text", {"input": "x"}, "marp_slides", sleep=lambda s: None
    )
    assert output == {"output": "the deck text"}


def test_schedule_permanent_failure_is_actionable() -> None:
    from niquests import ConnectionError as RequestsConnectionError

    class AlwaysDown:
        def ocs(self, method, endpoint, json=None, **kw):
            if endpoint == SCHEDULE_ENDPOINT:
                raise RequestsConnectionError("no route to host")
            raise AssertionError("should not poll")

    nc = AlwaysDown()
    with pytest.raises(NestedTaskError, match="unreachable"):
        schedule_and_wait(
            nc, "core:text2text", {"input": "x"}, "marp_slides", sleep=lambda s: None
        )


def test_schedule_429_is_retried() -> None:
    from nc_py_api import NextcloudException

    ocs = FakeOcs(
        schedule_response={"task": {"id": 7}},
        poll_statuses=[ok_poll()],
    )
    real_call = ocs.__call__
    state = {"n": 0}

    def rate_limited_then_ok(method, endpoint, json=None, **kw):
        state["n"] += 1
        if endpoint == SCHEDULE_ENDPOINT and state["n"] == 1:
            raise NextcloudException(429, "Too many requests")
        return real_call(method, endpoint, json=json, **kw)

    nc = type("FakeNc", (), {"ocs": staticmethod(rate_limited_then_ok)})()
    output = schedule_and_wait(
        nc, "core:text2text", {"input": "x"}, "marp_slides", sleep=lambda s: None
    )
    assert output == {"output": "the deck text"}


def test_poll_429_is_retried() -> None:
    from nc_py_api import NextcloudException

    ocs = FakeOcs(
        schedule_response={"task": {"id": 7}},
        poll_statuses=[ok_poll()],
    )
    real_call = ocs.__call__
    state = {"n": 0}

    def rate_limited_once(method, endpoint, json=None, **kw):
        state["n"] += 1
        if endpoint != SCHEDULE_ENDPOINT and state["n"] == 2:  # first poll
            raise NextcloudException(429, "Too many requests")
        return real_call(method, endpoint, json=json, **kw)

    nc = type("FakeNc", (), {"ocs": staticmethod(rate_limited_once)})()
    output = schedule_and_wait(
        nc, "core:text2text", {"input": "x"}, "marp_slides", sleep=lambda s: None
    )
    assert output == {"output": "the deck text"}


def test_poll_other_nextcloud_error_surfaces() -> None:
    from nc_py_api import NextcloudException

    class Gone:
        def ocs(self, method, endpoint, json=None, **kw):
            if endpoint == SCHEDULE_ENDPOINT:
                return {"task": {"id": 7}}
            raise NextcloudException(500, "internal server error")

    nc = Gone()
    with pytest.raises(NestedTaskError, match="polling"):
        schedule_and_wait(
            nc, "core:text2text", {"input": "x"}, "marp_slides", sleep=lambda s: None
        )


def test_missing_output_is_an_error() -> None:
    ocs = FakeOcs(
        schedule_response={"task": {"id": 7}},
        poll_statuses=[{"id": 7, "status": "STATUS_SUCCESSFUL", "output": None}],
    )
    nc = type("FakeNc", (), {"ocs": staticmethod(ocs)})()
    with pytest.raises(NestedTaskError, match="without an output"):
        schedule_and_wait(
            nc, "core:text2text", {"input": "x"}, "marp_slides", sleep=lambda s: None
        )
