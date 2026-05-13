"""Agent Broker: privileged command broker with Docker socket access.

Accepts commands from agent-core via Redis Streams, validates against a
strict allowlist, executes via Docker SDK, and responds on a separate stream.
"""

import asyncio
import json
import logging
import signal
from datetime import datetime, timezone

import docker
import redis.asyncio as aioredis
import structlog

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

import os

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        LOG_LEVELS.get(os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    ),
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)

logger = structlog.get_logger()

STREAM_COMMANDS = "broker:commands"
STREAM_RESPONSES = "broker:responses"
CONSUMER_GROUP = "agent-broker-group"
CONSUMER_NAME = "broker-1"

# Strict allowlist of permitted operations and targets
ALLOWED_OPS = {
    "restart_container": {
        "targets": {"agent-core", "agent-telegram", "agent-redis"},
    },
    "container_status": {
        "targets": {"agent-core", "agent-telegram", "agent-redis", "agent-postgres", "agent-ollama"},
    },
    "container_logs": {
        "targets": {"agent-core", "agent-telegram", "agent-redis", "agent-postgres", "agent-ollama"},
    },
}


def validate_command(operation: str, target: str) -> str | None:
    """Validate a command. Returns error message or None if valid."""
    if operation not in ALLOWED_OPS:
        return f"Operation '{operation}' not allowed. Allowed: {', '.join(ALLOWED_OPS)}"
    allowed_targets = ALLOWED_OPS[operation]["targets"]
    if target not in allowed_targets:
        return f"Target '{target}' not allowed for '{operation}'. Allowed: {', '.join(sorted(allowed_targets))}"
    return None


def execute_command(docker_client: docker.DockerClient, operation: str, target: str) -> dict:
    """Execute a validated command via Docker SDK. Returns result dict."""
    try:
        container = docker_client.containers.get(target)
    except docker.errors.NotFound:
        return {"success": False, "error": f"Container '{target}' not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    if operation == "restart_container":
        try:
            container.restart(timeout=30)
            return {"success": True, "result": f"Container '{target}' restarted"}
        except Exception as e:
            return {"success": False, "error": f"Restart failed: {e}"}

    elif operation == "container_status":
        try:
            container.reload()
            state = container.attrs.get("State", {})
            return {
                "success": True,
                "result": json.dumps({
                    "status": container.status,
                    "running": state.get("Running", False),
                    "started_at": state.get("StartedAt", ""),
                    "health": state.get("Health", {}).get("Status", "n/a"),
                }),
            }
        except Exception as e:
            return {"success": False, "error": f"Status check failed: {e}"}

    elif operation == "container_logs":
        try:
            logs = container.logs(tail=50, timestamps=True).decode("utf-8", errors="replace")
            return {"success": True, "result": logs[-2000:]}  # Cap at 2000 chars
        except Exception as e:
            return {"success": False, "error": f"Logs retrieval failed: {e}"}

    return {"success": False, "error": f"Unknown operation: {operation}"}


async def main():
    redis_url = os.environ.get("REDIS_URL", "redis://agent-redis:6379/0")

    logger.info("broker.starting", redis_url=redis_url)

    # Connect to Redis
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.ping()
    logger.info("broker.redis_connected")

    # Ensure consumer group
    try:
        await client.xgroup_create(STREAM_COMMANDS, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("broker.group_created", stream=STREAM_COMMANDS)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    # Connect to Docker
    docker_client = docker.from_env()
    docker_client.ping()
    logger.info("broker.docker_connected")

    # Shutdown signal
    shutdown = asyncio.Event()

    def signal_handler():
        logger.info("broker.shutdown_signal")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    logger.info("broker.ready")

    while not shutdown.is_set():
        try:
            results = await client.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={STREAM_COMMANDS: ">"},
                count=1,
                block=5000,
            )

            if not results:
                continue

            for _stream, messages in results:
                for msg_id, data in messages:
                    correlation_id = data.get("correlation_id", "")
                    operation = data.get("operation", "")
                    target = data.get("target", "")
                    requested_by = data.get("requested_by", "unknown")

                    logger.info(
                        "broker.command_received",
                        operation=operation,
                        target=target,
                        requested_by=requested_by,
                        correlation_id=correlation_id,
                    )

                    # Validate
                    error = validate_command(operation, target)
                    if error:
                        response = {"success": False, "error": error}
                        logger.warning("broker.command_rejected", reason=error)
                    else:
                        # Execute in thread pool (Docker SDK is sync)
                        response = await loop.run_in_executor(
                            None, execute_command, docker_client, operation, target
                        )

                    # Publish response
                    await client.xadd(STREAM_RESPONSES, {
                        "correlation_id": correlation_id,
                        "operation": operation,
                        "target": target,
                        "success": str(response.get("success", False)).lower(),
                        "result": str(response.get("result", response.get("error", ""))),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                    # Trim response stream (keep last 100)
                    await client.xtrim(STREAM_RESPONSES, maxlen=100, approximate=True)

                    # ACK
                    await client.xack(STREAM_COMMANDS, CONSUMER_GROUP, msg_id)

                    logger.info(
                        "broker.command_executed",
                        operation=operation,
                        target=target,
                        success=response.get("success"),
                    )

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("broker.consume_error")
            await asyncio.sleep(2)

    await client.aclose()
    docker_client.close()
    logger.info("broker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
