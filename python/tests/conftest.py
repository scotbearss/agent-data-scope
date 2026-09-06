"""Every test runs offline: no LangSmith, no network, no paid model."""

from __future__ import annotations

import os
import socket

import pytest

from agent_data_scope.lesson import LESSON_POLICY_PATH
from agent_data_scope.policy import load_scope_policy


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("Offline only")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def policy_path() -> str:
    assert os.path.exists(LESSON_POLICY_PATH), LESSON_POLICY_PATH
    return LESSON_POLICY_PATH


@pytest.fixture
def policy(policy_path: str):
    return load_scope_policy(policy_path)
