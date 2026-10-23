"""Nested TaskProcessing helper: schedule a task via OCS on the NextcloudApp
client and poll until it finishes.

Used for the inner ``core:text2text`` LLM step: whatever text provider the
instance has configured does the inference; this ExApp never calls a model API
directly. Uses the ``tasks_consumer`` endpoints, which are the app-context
(no user) variants intended for ExApps.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from nc_py_api import NextcloudApp, NextcloudException
from niquests import ConnectionError as RequestsConnectionError
from niquests import Timeout as RequestsTimeout

SCHEDULE_ENDPOINT = "/ocs/v1.php/taskprocessing/tasks_consumer/schedule"
TASK_ENDPOINT = "/ocs/v1.php/taskprocessing/tasks_consumer/task/{task_id}"

STATUS_SUCCESSFUL = "STATUS_SUCCESSFUL"
STATUS_FAILED = "STATUS_FAILED"
STATUS_CANCELLED = "STATUS_CANCELLED"

_SCHEDULE_RETRIES = 20
_RETRY_DELAY_S = 1.0
#: HTTP status Nextcloud answers with while the provider is rate-limiting
#: the schedule/poll calls: back off instead of failing the task.
_RATE_LIMIT_STATUS = 429

Sleep = Callable[[float], None]
Clock = Callable[[], float]


class NestedTaskError(RuntimeError):
    """The nested task failed, timed out or could not be scheduled. The
    message is user-facing-safe and includes the server's error text."""


def schedule_and_wait(
    nc: NextcloudApp,
    task_type_id: str,
    task_input: Mapping[str, Any],
    app_id: str,
    timeout_s: int = 300,
    poll_interval_s: float = 2.0,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    """Schedule a TaskProcessing task as the ExApp and wait for completion.

    Returns the task's ``output`` dict. Raises :class:`NestedTaskError` on
    failure, cancellation or timeout, surfacing the server-side error text.
    """
    task_id = _schedule(nc, task_type_id, task_input, app_id, sleep)
    return _wait(nc, task_type_id, task_id, timeout_s, poll_interval_s, sleep, clock)


def _schedule(
    nc: NextcloudApp,
    task_type_id: str,
    task_input: Mapping[str, Any],
    app_id: str,
    sleep: Sleep,
) -> int:
    last_exc: Exception | None = None
    for _ in range(_SCHEDULE_RETRIES):
        try:
            response = nc.ocs(
                "POST",
                SCHEDULE_ENDPOINT,
                json={"type": task_type_id, "appId": app_id, "input": dict(task_input)},
            )
        except (RequestsConnectionError, RequestsTimeout) as exc:
            last_exc = exc
            sleep(_RETRY_DELAY_S)
            continue
        except NextcloudException as exc:
            if exc.status_code == _RATE_LIMIT_STATUS:
                last_exc = exc
                sleep(_RETRY_DELAY_S * 2)
                continue
            raise NestedTaskError(
                f"Could not schedule the internal {task_type_id} task: {exc}"
            ) from exc
        try:
            return int(response["task"]["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NestedTaskError(
                f"Could not schedule the internal {task_type_id} task: unexpected server response."
            ) from exc
    raise NestedTaskError(
        f"Could not schedule the internal {task_type_id} task: the Nextcloud server was unreachable "
        f"after {_SCHEDULE_RETRIES} attempts ({last_exc})."
    )


def _wait(
    nc: NextcloudApp,
    task_type_id: str,
    task_id: int,
    timeout_s: int,
    poll_interval_s: float,
    sleep: Sleep,
    clock: Clock,
) -> dict[str, Any]:
    deadline = clock() + timeout_s
    status = ""
    task: dict[str, Any] = {}
    while True:
        if clock() >= deadline:
            raise NestedTaskError(
                f"The internal {task_type_id} task did not finish within {timeout_s} seconds."
            )
        try:
            response = nc.ocs("GET", TASK_ENDPOINT.format(task_id=task_id))
            task = response["task"]
            status = task.get("status", "")
        except (RequestsConnectionError, RequestsTimeout):
            sleep(poll_interval_s)
            continue
        except NextcloudException as exc:
            if exc.status_code == _RATE_LIMIT_STATUS:
                sleep(poll_interval_s * 2)
                continue
            raise NestedTaskError(
                f"Nextcloud error while polling the internal {task_type_id} task: {exc}"
            ) from exc
        except (KeyError, TypeError) as exc:
            raise NestedTaskError(
                f"Lost track of the internal {task_type_id} task: unexpected server response."
            ) from exc

        if status == STATUS_SUCCESSFUL:
            output = task.get("output")
            if not isinstance(output, dict):
                raise NestedTaskError(
                    f"The internal {task_type_id} task finished without an output."
                )
            return output
        if status in (STATUS_FAILED, STATUS_CANCELLED):
            error_text = (
                task.get("userFacingErrorMessage") or task.get("errorMessage") or status
            )
            raise NestedTaskError(
                f"The internal {task_type_id} task failed: {error_text}"
            )
        sleep(poll_interval_s)
