"""Command-level coverage of wait summaries using mocked cloud responses."""

import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
from typer.testing import CliRunner

from comfy_cli.command import jobs
from comfy_cli.output import OutputMode, Renderer, set_renderer


@pytest.fixture
def cloud_wait(monkeypatch):
    def invoke(payloads):
        client = SimpleNamespace(
            target=SimpleNamespace(base_url="https://cloud.example"),
            get_job_status=lambda pid: payloads[pid],
            get_history=lambda pid: {"outputs": {}},
            extract_outputs=lambda record: [{"node_id": "9", "url": "https://cloud.example/output.png"}],
        )
        monkeypatch.setattr(jobs, "_cloud_client", lambda: client)
        monkeypatch.setattr(jobs, "cloud_preflight_or_exit", lambda: None)
        set_renderer(Renderer(mode=OutputMode.JSON, command="jobs wait"))
        result = CliRunner().invoke(jobs.app, ["wait", *payloads, "--where", "cloud", "--timeout", "0"])
        envelope = json.loads(result.stdout)
        summary = envelope["data"] if envelope["ok"] else envelope["error"]["details"]
        schema_path = Path(jobs.__file__).resolve().parents[1] / "schemas" / "jobs_wait.json"
        jsonschema.Draft202012Validator(json.loads(schema_path.read_text())).validate(summary)
        assert "current_inputs" not in result.stdout
        assert "private-widget-value" not in result.stdout
        return result, envelope, summary

    return invoke


@pytest.mark.parametrize(
    "record",
    [
        {
            "node_id": "5",
            "node_type": "KSampler",
            "exception_type": "RuntimeError",
            "exception_message": "Node failed",
            "traceback": ["frame one", "frame two", "frame three"],
            "current_inputs": {"api_key": "private-widget-value"},
        },
        {"node_id": "5"},
        {},
    ],
    ids=["structured", "partial", "empty"],
)
def test_wait_preserves_structured_failure(cloud_wait, record):
    result, envelope, summary = cloud_wait({"failed": {"status": "failed", "execution_error": record}})

    assert result.exit_code == 1, result.output
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "execution_error"
    assert (summary["total"], summary["completed"], summary["failed"]) == (1, 0, 1)
    row = summary["jobs"][0]
    assert (row["prompt_id"], row["status"], row["ok"]) == ("failed", "error", False)
    assert row["execution_error"] == {k: v for k, v in record.items() if k != "current_inputs"}
    if "exception_message" in record:
        assert row["error_message"] == "RuntimeError: Node failed (node 5 KSampler)"


@pytest.mark.parametrize("extra", [{}, {"execution_error": None}])
def test_wait_failure_without_structured_error(cloud_wait, extra):
    result, envelope, summary = cloud_wait({"failed": {"status": "failed", "error_message": "boom", **extra}})

    assert result.exit_code == 1, result.output
    assert envelope["error"]["code"] == "execution_error"
    assert summary["jobs"] == [{"prompt_id": "failed", "status": "error", "ok": False, "error_message": "boom"}]


@pytest.mark.parametrize("extra", [{}, {"execution_error": {"node_id": "5", "exception_message": "stale"}}])
def test_wait_success_has_no_execution_error(cloud_wait, extra):
    result, envelope, summary = cloud_wait({"success": {"status": "success", **extra}})

    assert result.exit_code == 0, result.output
    assert envelope["ok"] is True
    assert (summary["total"], summary["completed"], summary["failed"]) == (1, 1, 0)
    assert summary["jobs"] == [
        {"prompt_id": "success", "status": "completed", "ok": True, "outputs": ["https://cloud.example/output.png"]}
    ]


@pytest.mark.parametrize(
    ("statuses", "counts", "exit_code", "error_code"),
    [
        (["success", "failed", "cancelled", "running"], (1, 1, 1, 1), 1, "execution_error"),
        (["cancelled"], (0, 0, 1, 0), 130, "cancelled"),
        (["running"], (0, 0, 0, 1), 1, "wait_timeout"),
    ],
    ids=["mixed", "cancelled", "timeout"],
)
def test_wait_preserves_counts_and_exit_codes(cloud_wait, statuses, counts, exit_code, error_code):
    record = {"node_id": "5", "exception_type": "RuntimeError", "exception_message": "boom"}
    payloads = {
        status: {"status": status, **({"execution_error": record} if status == "failed" else {})} for status in statuses
    }
    result, envelope, summary = cloud_wait(payloads)

    assert result.exit_code == exit_code, result.output
    assert envelope["error"]["code"] == error_code
    assert summary["total"] == len(statuses)
    assert tuple(summary[key] for key in ("completed", "failed", "cancelled", "timed_out")) == counts
    assert [row["prompt_id"] for row in summary["jobs"]] == statuses
    for row in summary["jobs"]:
        assert row["ok"] is (row["status"] == "completed")
        if row["status"] == "error":
            assert row["execution_error"] == record
        else:
            assert "execution_error" not in row
