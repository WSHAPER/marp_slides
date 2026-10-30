"""Defensive capability guards: notice instance gaps, warn once, never block.

The app DELIVERS artifacts and flows that rely on instance capabilities
it does not control - the canonical incident (observed live): the deck
source ``.md`` was delivered into the user's Files, but the instance had
NO markdown/text editor app enabled, so the owner could not open the
source and the entire edit-and-re-render flow was silently dead. This
module notices such gaps and WARNs loudly - one WARNING line naming the
guard, what is degraded and the admin remedy - so the gap surfaces in
docker logs (the admin-visible surface; deliberately no new API
dependency) instead of dying silently. Guards never raise, never block
and never change any task's outcome.

Probe mechanism (each guard's mechanism is live-verified against the
nc35 dev instance, 2026-10-30, read-only from the ExApp container):

- ``nc.apps.get_list()`` (OCS ``GET /ocs/v1.php/cloud/apps``) answers
  ``401 Unauthorized`` from app context: the apps-list endpoint is
  admin-only and the ExApp cannot use it. A known-app-ids probe on the
  enabled-apps list is therefore NOT viable from the ExApp.
- ``nc.capabilities`` (app context) carries sections only from apps
  that register one; neither ``files`` nor ``text`` registers any (the
  live response lists app_api/assistant/bruteforce/calendar/
  integration_openai/theming), so capabilities cannot see either app.
- The direct-editing INFO route (a files-app route, impersonated
  ``GET /ocs/v2.php/apps/files/api/v1/directEditing`` via the same
  ``nc.set_user`` seam the Files delivery rides) answers 200: its
  ``editors`` mapping lists every enabled app that can edit files in
  Files together with the mimetypes each covers. Live: the ``text``
  app's editor lists ``text/markdown`` first (plus text/plain, yaml,
  ...); richdocuments lists the office formats. THIS is the probe
  surface for both guards.

Guard semantics on that surface:

- ``files_app``: ok iff the direct-editing route answers at all. The
  route belongs to the files app, so its absence (404, whether as OCS
  ``NextcloudExceptionNotFound`` or as an HTTP-level 404 raised as a
  plain ``NextcloudException(status_code=404)`` - nc_py_api's session
  maps only the OCS-meta 404 to NotFound) means the delivery target
  itself is broken.
- ``markdown_editor``: ok iff SOME editor covers ``text/markdown`` -
  the mimetype contract, not an app id, so the probe survives app
  renames and accepts any future editor that registers for markdown
  (extend the known coverage by simply enabling such an app; nothing
  to change here). The endpoint answering with no markdown editor
  (404 included: no editing surface at all) fails the guard.

Anything else a probe hits - 401 (the worker-startup sweep runs without
an impersonated user; live-verified: the same call 401s then), a
timeout, a malformed payload - counts as UNKNOWN: one DEBUG line, latch
untouched, never a warning and never a failure.

Latch: WARN-ONCE per process per guard. The first failing probe warns;
further failing probes stay silent until a probe turns ok, which logs
one INFO line ("recovered") and re-arms the warning. Thread-safe (the
worker and request threads sweep concurrently): probes run outside the
lock, latch transitions serialize under one module-level lock.

Call sites (keep this list true): the task-worker loop's startup path
(best-effort - unimpersonated, so the probes read unknown until a user
context exists) and every successful Files delivery in
:mod:`marp_slides.draft_delivery` (impersonated, the authoritative
re-check right when a delivered artifact depends on the capability).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nc_py_api import NextcloudApp, NextcloudException, NextcloudExceptionNotFound

from .settings import MARKDOWN_MIME_TYPE

LOGGER = logging.getLogger("marp_slides.capability_guards")

#: The files app's direct-editing info route: answers with the editors
#: mapping (editor id -> covered mimetypes) for an impersonated user.
_DIRECT_EDITING_PATH = "/ocs/v2.php/apps/files/api/v1/directEditing"

#: HTTP-level 404, raised by nc_py_api as a plain NextcloudException
#: (only the OCS-meta 404 becomes NextcloudExceptionNotFound).
_HTTP_NOT_FOUND = 404

_LATCH_LOCK = threading.Lock()
_WARNED: set[str] = set()


@dataclass(frozen=True)
class CapabilityGuard:
    """One instance capability the delivered artifacts rely on.

    ``probe`` gets the app-context client as-is (impersonation state
    included) and answers ``True`` (capability present) or ``False``
    (capability missing); any exception it raises reads as unknown.
    ``degraded`` and ``remedy`` are the warning's payload: what usage
    breaks and the administrator action that fixes it.
    """

    name: str
    degraded: str
    remedy: str
    probe: Callable[[NextcloudApp], bool]


def _direct_editing_editors(nc: NextcloudApp) -> dict[str, list[str]] | None:
    """The instance's direct-editing editors as ``{editor id: mimetypes}``,
    or None when the direct-editing route itself is gone (the files app
    that serves it is not enabled). Any other problem raises, which the
    sweep reads as unknown."""
    try:
        payload: Any = nc.ocs("GET", _DIRECT_EDITING_PATH)
    except NextcloudExceptionNotFound:
        return None
    except NextcloudException as exc:
        if getattr(exc, "status_code", None) != _HTTP_NOT_FOUND:
            raise
        return None
    if not isinstance(payload, dict):
        raise TypeError(f"unexpected direct editing payload: {payload!r}")
    editors = payload.get("editors")
    if not isinstance(editors, dict):
        return {}
    result: dict[str, list[str]] = {}
    for editor_id, entry in editors.items():
        if not isinstance(entry, dict):
            continue
        mimetypes = entry.get("mimetypes")
        result[str(editor_id)] = (
            [str(m) for m in mimetypes if isinstance(m, str)]
            if isinstance(mimetypes, list)
            else []
        )
    return result


def _probe_files_app(nc: NextcloudApp) -> bool:
    """The files app (the delivery target) is enabled: its direct-editing
    route answers. See the module docstring for the 404 semantics."""
    return _direct_editing_editors(nc) is not None


def _probe_markdown_editor(nc: NextcloudApp) -> bool:
    """Some enabled app lets users edit text/markdown in Files: an editor
    registered for the markdown mimetype (the ``text`` app today; the
    mimetype contract accepts any future one). No route at all also
    fails: with no editing surface nothing edits markdown either."""
    editors = _direct_editing_editors(nc)
    return editors is not None and any(
        MARKDOWN_MIME_TYPE in mimetypes for mimetypes in editors.values()
    )


#: The registry. To guard another instance capability: add a probe
#: (cheap, read-only, app-context reachable) and an entry here.
GUARDS: tuple[CapabilityGuard, ...] = (
    CapabilityGuard(
        name="files_app",
        degraded=(
            "deck files are delivered into the Files app, which users then "
            "cannot browse or open"
        ),
        remedy="enable the files app (the delivery target of the generated decks)",
        probe=_probe_files_app,
    ),
    CapabilityGuard(
        name="markdown_editor",
        degraded=(
            "delivered deck sources (.md) cannot be opened or edited by "
            "users, so the edit-and-re-render flow is dead"
        ),
        remedy=("enable an app that edits text/markdown in Files, e.g. the text app"),
        probe=_probe_markdown_editor,
    ),
)


def sweep_capability_guards(nc: NextcloudApp) -> None:
    """Probe every guard against ``nc`` and drive the warn-once latch.
    Never raises and never blocks anything: a probe that errors is
    unknown (one DEBUG line, latch untouched); a failing probe warns
    exactly once until a probe turns ok, which logs one INFO line and
    re-arms the warning. Thread-safe: probes run outside the lock,
    latch transitions serialize under :data:`_LATCH_LOCK`."""
    for guard in GUARDS:
        try:
            ok = guard.probe(nc)
        except Exception as exc:  # noqa: BLE001 - unknown by contract: a broken probe must never warn, let alone block
            LOGGER.debug(
                "Instance capability guard %r could not be probed (%s); "
                "treating this check as unknown.",
                guard.name,
                exc,
            )
            continue
        if ok:
            with _LATCH_LOCK:
                recovered = guard.name in _WARNED
                _WARNED.discard(guard.name)
            if recovered:
                LOGGER.info(
                    "Instance capability guard %r recovered: the capability "
                    "is available again.",
                    guard.name,
                )
            continue
        with _LATCH_LOCK:
            first_failure = guard.name not in _WARNED
            _WARNED.add(guard.name)
        if first_failure:
            LOGGER.warning(
                "Instance capability guard %r failed: %s. Related usage is "
                "degraded until an administrator fixes it: %s.",
                guard.name,
                guard.degraded,
                guard.remedy,
            )
