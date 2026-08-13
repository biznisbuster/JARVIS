"""Tests for jarvis.log — logging setup honoring JARVIS_LOG_LEVEL."""

from __future__ import annotations

import logging

import jarvis.log as jlog


def test_setup_logging_sets_level_from_env(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(jlog, "_CONFIGURED", False)
    logger = logging.getLogger("jarvis")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    jlog.setup_logging()
    assert logger.level == logging.DEBUG
    assert logger.handlers, "jarvis logger must have a handler"
    assert logger.propagate is False


def test_setup_logging_defaults_to_info(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_LOG_LEVEL", raising=False)
    monkeypatch.setattr(jlog, "_CONFIGURED", False)
    logger = logging.getLogger("jarvis")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    jlog.setup_logging()
    assert logger.level == logging.INFO


def test_setup_logging_invalid_level_falls_back_to_info(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_LOG_LEVEL", "NOT_A_LEVEL")
    monkeypatch.setattr(jlog, "_CONFIGURED", False)
    logger = logging.getLogger("jarvis")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    jlog.setup_logging()
    assert logger.level == logging.INFO


def test_setup_logging_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(jlog, "_CONFIGURED", False)
    logger = logging.getLogger("jarvis")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    jlog.setup_logging()
    n = len(logger.handlers)
    jlog.setup_logging()
    assert len(logger.handlers) == n
