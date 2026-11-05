"""Blimp Crew v2 — Wake rules engine (spec §8.6): events -> prioritized, debounced TurnRequests.

Zero LLM. Rules are data (policies/wake_rules.yaml semantics compiled to code here for
the default set; the file stays the source of truth for ops changes, R4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Priority classes (spec §8.6)
DEBOUNCE_S = {"P0": 0, "P1": 60, "P2": 600}
OFFICE_HOURS_P3 = ["09:00", "13:00", "17:00", "21:00"]  # MYT
RATE_LIMITS_TURNS_PER_HOUR = {"duke": 12, "hermes": 10, "frank": 12}

ACT_TO_PRIORITY = {
    "REQUEST": "from_message", "QUERY": "from_message", "PROPOSE": "from_message",
    "HANDOFF": "from_message", "PUSHBACK": "from_message", "ESCALATE": "from_message",
    "ANSWER": "P2", "ACCEPT": "P2", "COUNTER": "P1", "REJECT": "P2",
    "STATUS": None,   # never wakes
    "DONE": None,     # wakes the verifier, not the sender
    "ALERT": "from_message", "INFORM": None,
}


@dataclass
class WakeEvent:
    event_id: int
    type: str
    actor: str
    payload: dict = field(default_factory=dict)


@dataclass
class TurnRequest:
    agent: str
    priority: str
    event_ids: list[int] = field(default_factory=list)
    task_id: str | None = None


def _message_priority(msg: dict, sender: str) -> str:
    sa = (msg.get("metadata") or {}).get("https://blimp.my/a2a/ext/speech-act/v1") or {}
    if sa.get("act") == "ESCALATE":
        return "P0"  # decision queue: the Boss-bound
    p = sa.get("priority")
    return p if p in ("P0", "P1", "P2", "P3") else "P2"


def evaluate_wakes(events: list[WakeEvent], *, logan_id: str = "logan") -> list[TurnRequest]:
    """Apply the default wake rules to a batch of events; return TurnRequests to enqueue.

    Rules (spec §8.6 wake_rules.yaml):
      - message from the Boss to an agent                          -> P0
      - speech acts REQUEST/QUERY/PROPOSE/HANDOFF/PUSHBACK      -> priority from the message
      - ALERT sev1/sev2                                         -> P0 to frank
      - verification.failed                                     -> P1 to task owner
      - approval.granted                                        -> P1 to requester
      - schedule.fired                                          -> schedule's priority
      - task.unblocked                                          -> P2 to owner
      - STATUS/DONE/INFORM                                      -> never wake an agent
    """
    requests: list[TurnRequest] = []
    per_agent: dict[tuple[str, str], TurnRequest] = {}

    def add(agent: str | None, priority: str, event_id: int, task_id: str | None):
        if not agent or priority is None:
            return
        key = (agent, priority)
        tr = per_agent.get(key)
        if tr is None:
            tr = TurnRequest(agent=agent, priority=priority, task_id=task_id)
            per_agent[key] = tr
            requests.append(tr)
        tr.event_ids.append(event_id)

    for ev in events:
        p = ev.payload or {}
        if ev.type == "message.sent":
            msg = p.get("message", {})
            sa = (msg.get("metadata") or {}).get("https://blimp.my/a2a/ext/speech-act/v1") or {}
            act = sa.get("act", "")
            sender = sa.get("from", ev.actor)
            recipients = sa.get("to", [])
            if ev.actor == logan_id:
                for r in recipients:
                    add(r, "P0", ev.event_id, msg.get("taskId"))
                continue
            act_priority = ACT_TO_PRIORITY.get(act, "P2")
            if act_priority is None:
                continue  # STATUS / DONE / INFORM never wake
            # "from_message" means the sender's declared priority (spec 8.6 wake_rules.yaml);
            # it used to collapse every such message to P1, so a P3 PROPOSE woke its
            # recipient like a blocked teammate would.
            pr = _message_priority(msg, sender) if act_priority == "from_message" else act_priority
            for r in recipients:
                add(r, pr, ev.event_id, msg.get("taskId"))
        elif ev.type == "alert.raised" and p.get("severity") in ("sev1", "sev2"):
            add("frank", "P0", ev.event_id, p.get("task_id"))
        elif ev.type == "verification.failed":
            add(p.get("owner"), "P1", ev.event_id, p.get("task_id"))
        elif ev.type == "approval.granted":
            add(p.get("requested_by"), "P1", ev.event_id, p.get("task_id"))
        elif ev.type == "task.unblocked":
            add(p.get("owner"), "P2", ev.event_id, p.get("task_id"))
        elif ev.type == "schedule.fired":
            add(p.get("owner"), p.get("priority", "P3"), ev.event_id, p.get("task_id"))
        # everything else: no wake

    return requests
