"""
P7 Task Hook — notifies the Hermes bridge of cron task lifecycle events.

This module runs inside the hermes-agent cron scheduler process.
Cron jobs don't go through the bridge's HTTP API (they use AIAgent directly),
so we POST to the bridge's task endpoints to create task records.

Lightweight: fire-and-forget HTTP calls with a 2-second timeout.
Failures are logged but never block or fail the cron job.
"""
import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger("hermes.cron.p7")

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = int(os.getenv("HERMES_BRIDGE_PORT", 9002))  # hermes-bridge default
TIMEOUT = 2  # seconds


def _post(endpoint: str, payload: dict) -> bool:
    """Fire-and-forget POST to the bridge. Returns True on success."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://{BRIDGE_HOST}:{BRIDGE_PORT}{endpoint}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
            return body.get("success", False)
    except Exception as e:
        logger.debug("P7 task hook: %s %s — %s", endpoint.split("/")[-1], 
                      payload.get("task_id", "?"), e)
        return False


def task_start(task_id: str, job_id: str, job_name: str) -> bool:
    """
    Create a task record for a cron job execution.

    Args:
        task_id: Unique per-execution ID (e.g., cron_session_id)
        job_id: Persistent cron job ID (stable across runs)
        job_name: Human-readable job name
    """
    return _post("/api/p7/task-start", {
        "task_id": task_id,
        "cron_job_id": job_id,
        "agent_id": "thoth",
        "session_id": task_id,
        "metadata": {
            "job_name": job_name,
            "source": "cron_scheduler",
        },
    })


def task_end(task_id: str, status: str, error_message: str = None) -> bool:
    """
    Mark a cron task as completed or failed.

    Args:
        task_id: Same task_id used in task_start
        status: "completed" or "failed"
        error_message: Error detail if failed
    """
    return _post("/api/p7/task-end", {
        "task_id": task_id,
        "status": status,
        "error_message": error_message,
    })
