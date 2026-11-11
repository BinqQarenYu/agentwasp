# Implementation Plan: Ollama Timeout and Latency Optimization

**Branch**: `001-ollama-optimization` | **Date**: 2026-05-22 | **Spec**: [spec.md](file:///F:/012A_Github/agentwasp/specs/001-ollama-optimization/spec.md)

## Summary
To prevent timeouts and resolve user-facing latency issues for local and remote cloud Ollama setups, we will parameterize model loading and client generation timeouts, enable custom keep-alive parameters, and implement connection optimizations for cloud instances.

## Technical Context
- **Language/Version**: Python 3.12 (inside Docker Container)
- **Primary Dependencies**: FastAPI, Pydantic, HTTPX, pytest
- **Testing**: `pytest` executed inside the Docker container using `docker compose exec`.
- **Target Platform**: Docker-based agent-core container environment.

## Constitution Check
- ✅ No new projects (conforms to Simplicity Article VII).
- ✅ Clean direct framework configuration (conforms to Anti-Abstraction Article VIII).
- ✅ Tests will be written before implementation (conforms to Test-First Imperative Article III).

## Proposed Changes

### 1. `config.py`
Add settings:
- `ollama_load_timeout`: float, default `300.0`
- `ollama_client_timeout`: float, default `300.0`
- `ollama_keep_alive`: str, default `"10m"`
- `ollama_is_remote`: bool, default `False`

### 2. `ollama_provider.py`
Refactor `OllamaProvider` methods:
- `load_model`: Use custom client timeout and keep-alive.
- `generate`: Use custom client timeout, set optimized httpx limits/timeout if `ollama_is_remote` is `True`.

### 3. `tests/test_ollama_optimization.py`
Write TDD contract/unit tests to mock/verify custom timeout behaviors and configurations.

## Verification Plan

### Automated Tests
```bash
docker compose exec agent-core python -m pytest tests/test_ollama_optimization.py
```
