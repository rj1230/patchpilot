"""Tamper-evident audit log: each event hashes its predecessor."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import AuditEvent


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _event_hash(ev_seq: int, etype: str, payload_hash: str, prev: str, actor: str) -> str:
    return _h({"seq": ev_seq, "type": etype, "ph": payload_hash, "prev": prev, "actor": actor})


class AuditLog:
    def __init__(self, task_id: str, correlation_id: str, sink: Path | None = None):
        self.task_id, self.correlation_id, self.sink = task_id, correlation_id, sink
        self.events: list[AuditEvent] = []
        if sink:
            sink.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, payload: dict | None = None, actor: str = "orchestrator") -> AuditEvent:
        payload = payload or {}
        prev = self.events[-1].event_hash if self.events else "GENESIS"
        ph = _h(payload)
        ev = AuditEvent(
            seq=len(self.events), task_id=self.task_id, correlation_id=self.correlation_id,
            event_type=event_type, payload=payload, payload_hash=ph, prev_hash=prev, actor=actor,
            event_hash=_event_hash(len(self.events), event_type, ph, prev, actor),
        )
        self.events.append(ev)
        if self.sink:
            with self.sink.open("a") as f:
                f.write(ev.model_dump_json() + "\n")
        return ev

    def verify(self) -> bool:
        return verify_chain(self.events)


def verify_chain(events: list[AuditEvent]) -> bool:
    prev = "GENESIS"
    for ev in events:
        if ev.prev_hash != prev or ev.payload_hash != _h(ev.payload):
            return False
        if ev.event_hash != _event_hash(ev.seq, ev.event_type, ev.payload_hash, prev, ev.actor):
            return False
        prev = ev.event_hash
    return True
