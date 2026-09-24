from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from stablehand.config import get_settings
from stablehand.models import LIVE_STATES, RunState
from stablehand.runs.service import counts_of, log_text
from stablehand.security import csrf_token
from stablehand.ui.timeline import format_timestamp, isoformat_utc, timeline

STATE_LABELS = {
    RunState.check_queued.value: "Check queued",
    RunState.check_running.value: "Checking",
    RunState.needs_approval.value: "Needs approval",
    RunState.unchanged.value: "No changes",
    RunState.check_failed.value: "Check failed",
    RunState.apply_queued.value: "Apply queued",
    RunState.apply_running.value: "Applying",
    RunState.succeeded.value: "Applied",
    RunState.apply_failed.value: "Apply failed",
    RunState.rejected.value: "Rejected",
}

templates = Jinja2Templates(directory=str(get_settings().package_dir / "templates"))
templates.env.globals["state_label"] = lambda state: STATE_LABELS.get(state, state)
templates.env.globals["counts_of"] = counts_of
templates.env.globals["log_text"] = log_text
templates.env.globals["timeline"] = timeline
templates.env.globals["format_timestamp"] = format_timestamp
templates.env.globals["isoformat_utc"] = isoformat_utc
templates.env.globals["live_states"] = {state.value for state in LIVE_STATES}
templates.env.globals["public_url"] = lambda: get_settings().public_url.rstrip("/")


def render(request: Request, name: str, *, status_code: int = 200, **context):
    session_id = request.cookies.get("stablehand_session", "")
    context.setdefault("csrf_token", csrf_token(session_id) if session_id else "")
    context.setdefault("notice", request.query_params.get("notice", ""))
    context.setdefault("error", context.get("error") or request.query_params.get("error", ""))
    context.setdefault("user", None)
    return templates.TemplateResponse(request, name, context, status_code=status_code)
