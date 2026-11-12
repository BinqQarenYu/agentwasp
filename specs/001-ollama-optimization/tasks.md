# Tasks: Ollama Timeout and Latency Optimization

**Input**: Design documents from `/specs/001-ollama-optimization/`

## Phase 1: Setup

- [x] T001 Verify existing branch configuration and tests can run successfully inside the container environment.

## Phase 2: Foundational

- [x] T002 Add new configurable Ollama settings inside `containers/agent-core/src/config.py`.

## Phase 3: User Story 1 & 3 - Configurable Timeouts & Keep-Alives (Priority: P1 & P3)

🎯 **MVP Milestone**

**Goal**: Model loading and generation use custom timeouts and keep-alives configured in `config.py` instead of hardcoded defaults.

### TDD Test Phase
- [x] T003 Write initial tests in `containers/agent-core/tests/test_ollama_optimization.py` and verify that they fail inside the container (Red phase).

### Implementation Phase
- [x] T004 Modify `load_model` in `containers/agent-core/src/models/ollama_provider.py` to use `ollama_load_timeout` and `ollama_keep_alive`.
- [x] T005 Modify `generate` in `containers/agent-core/src/models/ollama_provider.py` to use `ollama_client_timeout`.

---

## Phase 4: User Story 2 - Local vs Remote/Cloud Ollama Profiles (Priority: P2)

**Goal**: Support optimized connection and keep-alive configuration tailored for remote cloud instances when `ollama_is_remote` is `True`.

### TDD Test Phase
- [x] T006 Write tests in `test_ollama_optimization.py` to verify remote/cloud connection profile handling.

### Implementation Phase
- [x] T007 Modify `generate` in `containers/agent-core/src/models/ollama_provider.py` to utilize custom client connection limits and network settings when `ollama_is_remote` is `True`.

---

## Phase 5: Polish & Verification

- [x] T008 Run the full test suite inside the docker container (`pytest tests/test_ollama_optimization.py`) and verify all tests pass.
- [x] T009 Create the walkthrough artifact to document all completed work and results.
