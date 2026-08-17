"""Hang-resilience telemetry — the four kirocrew.* series added after the
silent child-permission hang incidents (issue #3785, PRs #3786/#3889).

Each test drives the REAL production emit site (never a reimplementation)
with the recorder mocked, so a renamed metric, changed attr enum, or removed
emit fails here.
"""

from __future__ import annotations

import asyncio
import json  # noqa: F401  (parity with sibling runtime tests)
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.metrics import events as metric_events


@pytest.fixture()
def recorded(monkeypatch):
    """Capture every emit_counter call routed through metrics.events."""
    calls: list[tuple[str, dict]] = []

    def _fake(name: str, attrs: dict) -> None:
        calls.append((name, dict(attrs)))

    # Patch at the SOURCE module; the emitting modules import the function by
    # name, so patch their bound references too.
    for mod in (
        "kiro_crew.metrics.events",
        "kiro_crew.acp.runtime",
        "kiro_crew.acp.session_handle",
        "kiro_crew.subagent",
        "kiro_crew.session",
        "kiro_crew.dashboard.chat_runner",
    ):
        monkeypatch.setattr(f"{mod}.emit_counter", _fake, raising=False)
    return calls


def test_emit_counter_never_raises():
    """Telemetry must never break the instrumented path."""
    with patch(
        "kiro_crew.metrics.provider.get_recorder",
        side_effect=RuntimeError("recorder down"),
    ):
        metric_events.emit_counter("kirocrew.test", {"a": 1})  # no raise


@pytest.mark.asyncio
async def test_runtime_denial_emits_child_permission_denied(recorded):
    from test.test_acp_runtime import JsonRpcMessage, _make_runtime

    rt, _, _ = _make_runtime()
    msg = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "session/request_permission",
            "params": {
                "sessionId": "child-a",
                "toolCall": {"toolCallId": "tc-1", "title": "Running: x"},
                "options": [
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                ],
            },
        }
    )
    await rt._answer_unroutable_permission(msg, "child-a")
    await asyncio.sleep(0)
    hits = [a for n, a in recorded if n == metric_events.CHILD_PERMISSION_DENIED]
    assert {"surface": "runtime", "reason": "unregistered_session_auto_reject"} in hits


def test_dropped_frame_emits_method_class(recorded):
    from test.test_acp_runtime import _make_runtime

    rt, _, _ = _make_runtime()
    rt._note_dropped_frame("s-x", "session/request_permission")
    rt._note_dropped_frame("s-x", "session/update")
    rt._note_dropped_frame("s-x", "weird/method")
    classes = [
        a["method_class"] for n, a in recorded if n == metric_events.DROPPED_FRAMES
    ]
    assert classes == ["permission", "update", "other"]


def test_handle_reject_emits_child_permission_denied(recorded):
    from test.test_acp_runtime import AcpSessionHandle, _make_runtime, _register

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._audit_handle_reject(7, "Running: x", "child_low_fidelity_unaware_consumer")
    hits = [a for n, a in recorded if n == metric_events.CHILD_PERMISSION_DENIED]
    assert {
        "surface": "session_handle",
        "reason": "child_low_fidelity_unaware_consumer",
    } in hits


@pytest.mark.asyncio
async def test_subagent_child_reject_emits_denied(recorded):
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
    from kiro_crew.subagent import SubagentManager

    client = MagicMock()

    async def _noop(_rid):
        return None

    client.reject_tool = _noop
    ev = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=9,
        title="t",
        sub_session_id="child-a",
    )
    with patch("kiro_crew.subagent.sel"):
        await SubagentManager._reject_and_log(
            client, 9, "k", ev, error="child_escalation_limit"
        )
    hits = [a for n, a in recorded if n == metric_events.CHILD_PERMISSION_DENIED]
    assert {"surface": "subagent", "reason": "child_escalation_limit"} in hits
    # Parent-origin rejections do NOT emit (child series only).
    ev2 = LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id=10, title="t")
    with patch("kiro_crew.subagent.sel"):
        await SubagentManager._reject_and_log(client, 10, "k", ev2, error="hook_deny")
    assert len([a for n, a in recorded if n == metric_events.CHILD_PERMISSION_DENIED]) == 1
