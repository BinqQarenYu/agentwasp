# Feature Specification: Ollama Timeout and Latency Optimization

**Feature Branch**: `001-ollama-optimization`

**Created**: 2026-05-22

**Status**: Draft

**Input**: User description: "lets forcus on ollama model (both clound and local) ; regarding te chat (wasp) it lag and slow and most of the time time out. let make a good management of this."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Configurable Ollama Client Timeouts (Priority: P1)

As an AgentWasp user, I want the chat model loading and response generation timeouts to be fully configurable, so that large models (like Gemma 4 or Llama 3.3) don't trigger frequent network timeouts during cold starts or long generation phases.

**Why this priority**: Highly critical. Currently, cold-starting a large model takes longer than the hardcoded 120s timeout, causing immediate generation failure and chat timeout.

**Independent Test**: Can be tested by invoking the Ollama provider with a simulated slow load time (e.g. 150 seconds) and verifying that generation succeeds if the configured timeout is set high enough (e.g. 300 seconds), and fails early if set low (e.g. 5 seconds).

**Acceptance Scenarios**:

1. **Given** Ollama settings have `ollama_load_timeout` set to `300.0`, **When** a cold-start model requires `150.0` seconds to load, **Then** the model load succeeds and the chat generation completes without a timeout.
2. **Given** Ollama settings have `ollama_client_timeout` set to `10.0`, **When** the model takes `15.0` seconds to generate a response, **Then** a timeout is correctly raised after `10.0` seconds.

---

### User Story 2 - Local vs Remote/Cloud Ollama Profiles (Priority: P2)

As a user running Ollama in a cloud/remote server (high-network-latency, high-performance host) or on my local machine (low-network-latency, constrained resources), I want different timeout and connection settings to be applied depending on the deployment profile.

**Why this priority**: Crucial for cloud Ollama setups where remote network latency is higher, but server resources are more abundant (cold load is faster).

**Independent Test**: Can be tested by specifying a remote Ollama flag in settings and verifying that the client applies appropriate connection/network keep-alive properties.

**Acceptance Scenarios**:

1. **Given** `ollama_is_remote` is `True`, **When** connection is established, **Then** the client uses optimized connection pools and separate connect timeouts for network-sensitive cloud endpoints.

---

### User Story 3 - Configurable Keep-Alive Management (Priority: P3)

As a resource-conscious user, I want to control how long Ollama keeps a model loaded in VRAM/RAM after generation completes.

**Why this priority**: Keeps my machine responsive. Keeping a heavy model in VRAM can lag the entire OS, so allowing a short keep-alive or immediate unloading (`0s`) when idle is highly desirable.

**Independent Test**: Can be tested by verifying that the `keep_alive` parameter passed to the Ollama `api/generate` or `api/chat` endpoints matches the value configured in `config.py`.

**Acceptance Scenarios**:

1. **Given** `ollama_keep_alive` is configured as `"2m"`, **When** a model load or generation request is sent, **Then** the JSON payload contains `"keep_alive": "2m"`.

---

## Edge Cases

- **Ollama Offline / Server Crash**: If Ollama goes offline during a long generation, the client must raise a clear connection error instead of hanging or retrying indefinitely.
- **VRAM Exhaustion**: If loading a model fails due to out-of-memory on local systems, it should return a clear, user-friendly error message indicating insufficient RAM/VRAM.
- **Invalid Keep-Alive format**: If the keep-alive is set to an invalid duration string (e.g. `"foo"`), it should fall back safely to a default value (like `"10m"`).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST define `ollama_load_timeout` (float, default `300.0` seconds) in `config.py`.
- **FR-002**: The system MUST define `ollama_client_timeout` (float, default `300.0` seconds) in `config.py`.
- **FR-003**: The system MUST define `ollama_keep_alive` (str, default `"10m"`) in `config.py`.
- **FR-004**: The system MUST define `ollama_is_remote` (bool, default `False`) in `config.py`.
- **FR-005**: `OllamaProvider` MUST use `ollama_load_timeout` for HTTP POST requests to `api/generate` during model loading.
- **FR-006**: `OllamaProvider` MUST use `ollama_client_timeout` for HTTP POST requests to `api/chat` during response generation.
- **FR-007**: `OllamaProvider` MUST pass the configured `ollama_keep_alive` duration as `keep_alive` in all load/generation requests.

### Key Entities

- **Settings**: Configuration object storing all LLM and system parameters.
- **OllamaProvider**: The provider class managing local and remote Ollama API requests.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of large model cold starts under `300` seconds load successfully without timing out.
- **SC-002**: Users can adjust keep-alive to `"0"` and verify that the model is immediately unloaded after generation, reclaiming 100% VRAM instantly.
- **SC-003**: The system supports both local loops and remote Ollama URLs with custom profiles.
