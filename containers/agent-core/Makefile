# WASP Agent Core — Test Runner
# Usage:
#   make test-unit        Run only unit tests (fast, no Docker)
#   make test-integration Run integration tests
#   make test-chaos       Run chaos/stress tests
#   make test-security    Run security scans
#   make test-e2e         Run E2E dashboard tests
#   make test-all         Run the complete test harness
#   make test-existing    Run pre-existing test suite
#   make test-fast        Run unit + security (CI-friendly, <30s)

.PHONY: test-unit test-integration test-chaos test-security test-e2e \
        test-all test-existing test-fast lint

# Path to test harness
HARNESS := tests/e2e_harness
PYTEST   := python -m pytest

# Common flags
QUIET   := -q
VERBOSE := -v
ASYNCIO := --asyncio-mode=auto

# Colors (if terminal supports it)
GREEN  := \033[0;32m
YELLOW := \033[0;33m
RESET  := \033[0m

## ── Fast unit tests (no external deps) ─────────────────────────────────────
test-unit:
	@echo "$(GREEN)Running unit tests...$(RESET)"
	$(PYTEST) $(HARNESS)/unit/ $(VERBOSE) $(ASYNCIO) \
		--tb=short \
		-x \
		2>&1

## ── Integration tests (mocked HTTP, no real services) ───────────────────────
test-integration:
	@echo "$(GREEN)Running integration tests...$(RESET)"
	$(PYTEST) $(HARNESS)/integration/ $(VERBOSE) $(ASYNCIO) \
		--tb=short \
		2>&1

## ── Security scans (static analysis + policy invariants) ────────────────────
test-security:
	@echo "$(GREEN)Running security tests...$(RESET)"
	$(PYTEST) $(HARNESS)/security/ $(VERBOSE) $(ASYNCIO) \
		--tb=short \
		2>&1

## ── E2E dashboard tests ──────────────────────────────────────────────────────
test-e2e:
	@echo "$(GREEN)Running E2E tests...$(RESET)"
	$(PYTEST) $(HARNESS)/e2e/ $(VERBOSE) $(ASYNCIO) \
		--tb=short \
		2>&1

## ── Chaos / stress tests ────────────────────────────────────────────────────
test-chaos:
	@echo "$(YELLOW)Running chaos tests (may be slow)...$(RESET)"
	$(PYTEST) $(HARNESS)/chaos/ $(VERBOSE) $(ASYNCIO) \
		--tb=short \
		--timeout=60 \
		2>&1

## ── Pre-existing test suite ─────────────────────────────────────────────────
test-existing:
	@echo "$(GREEN)Running pre-existing tests...$(RESET)"
	$(PYTEST) tests/test_integrations_platform.py \
		       tests/test_advanced_goal_layer.py \
		       tests/test_agent_manager.py \
	$(VERBOSE) $(ASYNCIO) \
		--tb=short \
		-e DATABASE_URL="postgresql+asyncpg://x:x@localhost/test" \
		2>&1

## ── Fast subset (CI-friendly, no chaos) ─────────────────────────────────────
test-fast:
	@echo "$(GREEN)Running fast tests (unit + security)...$(RESET)"
	$(PYTEST) \
		$(HARNESS)/unit/ \
		$(HARNESS)/security/ \
	$(VERBOSE) $(ASYNCIO) \
		--tb=short \
		-x \
		2>&1

## ── Full test harness ───────────────────────────────────────────────────────
test-all:
	@echo "$(GREEN)════════════════════════════════════════════════$(RESET)"
	@echo "$(GREEN)  WASP Full Test Harness$(RESET)"
	@echo "$(GREEN)════════════════════════════════════════════════$(RESET)"
	@echo ""
	@echo "$(GREEN)[1/5] Unit tests$(RESET)"
	$(PYTEST) $(HARNESS)/unit/ $(ASYNCIO) --tb=short -q 2>&1 || true
	@echo ""
	@echo "$(GREEN)[2/5] Security tests$(RESET)"
	$(PYTEST) $(HARNESS)/security/ $(ASYNCIO) --tb=short -q 2>&1 || true
	@echo ""
	@echo "$(GREEN)[3/5] Integration tests$(RESET)"
	$(PYTEST) $(HARNESS)/integration/ $(ASYNCIO) --tb=short -q 2>&1 || true
	@echo ""
	@echo "$(GREEN)[4/5] E2E tests$(RESET)"
	$(PYTEST) $(HARNESS)/e2e/ $(ASYNCIO) --tb=short -q 2>&1 || true
	@echo ""
	@echo "$(YELLOW)[5/5] Chaos tests$(RESET)"
	$(PYTEST) $(HARNESS)/chaos/ $(ASYNCIO) --tb=short -q --timeout=60 2>&1 || true
	@echo ""
	@echo "$(GREEN)════════════════════════════════════════════════$(RESET)"
	@echo "$(GREEN)  Test harness complete$(RESET)"
	@echo "$(GREEN)════════════════════════════════════════════════$(RESET)"

## ── Lint ─────────────────────────────────────────────────────────────────────
lint:
	python -m flake8 src/ --max-line-length=120 --ignore=E501,W503 2>&1 || true
	python -m mypy src/ --ignore-missing-imports --no-error-summary 2>&1 || true
