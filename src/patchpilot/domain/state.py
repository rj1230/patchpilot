"""Durable workflow state machine with explicit legal transitions."""
from __future__ import annotations

from .models import TaskState as S

TERMINAL = {S.AWAIT_REVIEW, S.AWAIT_APPROVAL, S.MERGED, S.REJECTED, S.BLOCK_AND_REPORT, S.FAILED}

_FAIL = {S.BLOCK_AND_REPORT, S.FAILED}

TRANSITIONS: dict[S, set[S]] = {
    S.RECEIVED: {S.VALIDATE_REQUEST} | _FAIL,
    S.VALIDATE_REQUEST: {S.CLONE_REPOSITORY} | _FAIL,
    S.CLONE_REPOSITORY: {S.ANALYZE_REPOSITORY} | _FAIL,
    S.ANALYZE_REPOSITORY: {S.RETRIEVE_CONTEXT} | _FAIL,
    S.RETRIEVE_CONTEXT: {S.CREATE_PLAN} | _FAIL,
    S.CREATE_PLAN: {S.POLICY_CHECK} | _FAIL,
    S.POLICY_CHECK: {S.GENERATE_PATCH} | _FAIL,
    S.GENERATE_PATCH: {S.STATIC_ANALYSIS, S.ANALYZE_FAILURE} | _FAIL,
    S.STATIC_ANALYSIS: {S.RUN_TESTS_IN_SANDBOX, S.ANALYZE_FAILURE} | _FAIL,
    S.RUN_TESTS_IN_SANDBOX: {S.CREATE_PULL_REQUEST, S.AWAIT_APPROVAL, S.ANALYZE_FAILURE} | _FAIL,
    S.ANALYZE_FAILURE: {S.REPAIR_PATCH} | _FAIL,
    S.REPAIR_PATCH: {S.GENERATE_PATCH} | _FAIL,
    S.AWAIT_APPROVAL: {S.CREATE_PULL_REQUEST, S.REJECTED},
    S.CREATE_PULL_REQUEST: {S.AWAIT_REVIEW} | _FAIL,
    S.AWAIT_REVIEW: {S.MERGED, S.REJECTED},
    S.MERGED: set(),
    S.REJECTED: set(),
    S.BLOCK_AND_REPORT: set(),
    S.FAILED: set(),
}


class IllegalTransition(Exception):
    pass


class StateMachine:
    def __init__(self, initial: S = S.RECEIVED):
        self.state = initial
        self.history: list[S] = [initial]

    def to(self, new: S) -> S:
        if new not in TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {new.value} is not allowed")
        self.state = new
        self.history.append(new)
        return new

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL
