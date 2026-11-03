"""V2 contracts are tested only against MockTransport, never a robot."""

import json
import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from omni_tools.backends.http import HTTPBackend
from omni_tools.config import Settings
from omni_tools.errors import OmniError


ROBOT = "robot-demo"
TOKEN = "secret-test-token"


def envelope(data=None):
    value = {"code": 0, "message": "success"}
    if data is not None:
        value["data"] = data
    return httpx.Response(200, json=value)


def task(status="pending", **changes):
    return {"task_id": "task-1", "robot_id": ROBOT, "route_id": "route-1", "status": status, **changes}


def goal(status="pending", **changes):
    return {"goal_id": "goal-1", "plan_id": "plan-1", "robot_id": ROBOT, "map_id": "map-1", "map_version": 1, "status": status, **changes}


def plan(body, **changes):
    return {
        **body, "plan_id": "plan-1", "robot_id": ROBOT,
        "status": "ready", "executable": True,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
        **changes,
    }


@pytest.fixture(autouse=True)
def prohibit_real_transport(monkeypatch):
    def blocked(*_, **__):
        pytest.fail("Live HTTP transport is forbidden in control tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.fixture
def make_backend():
    clients = []

    def make(handler, **overrides):
        config = {
            "backend": "http", "robot_id": ROBOT, "base_url": "https://cloud.example.test",
            "token": TOKEN, "allow_writes": True, "allowed_robots": (ROBOT,),
        }
        config.update(overrides)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        return HTTPBackend(Settings(**config), client)

    yield make
    for client in clients:
        client.close()


OPERATIONS = [
    lambda backend: backend.start_inspection("route-1", ROBOT),
    lambda backend: backend.navigate_to("map-1", 1, 2, 3, 0, ROBOT),
    lambda backend: backend.cancel_task("task-1", ROBOT),
    lambda backend: backend.cancel_task("goal-1", ROBOT, "navigation"),
]


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("config,code", [
    ({"allow_writes": False}, "writes_disabled"),
    ({"allowed_robots": ("some-other-robot",)}, "robot_not_allowed"),
    ({"backend": "ssh"}, "unsupported_backend"),
])
def test_policy_rejection_never_even_queries_backend(make_backend, operation, config, code):
    def handler(_):
        pytest.fail("No request is allowed after a denied guard")

    with pytest.raises(OmniError) as caught:
        operation(make_backend(handler, **config))
    assert caught.value.code == code


def test_inspection_create_then_start_uses_cloud_ids_and_manual_pending_contract(make_backend):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        if len(requests) == 1:
            assert request.method == "POST" and request.url.path == "/api/v1/patrol/tasks"
            assert json.loads(request.content) == {
                "robot_id": ROBOT, "route_id": "route-1", "task_type": "manual", "priority": 0,
            }
            return envelope(task())
        assert request.method == "POST" and request.url.path == "/api/v1/patrol/tasks/task-1/start"
        assert request.content == b""
        return envelope(task("dispatching"))

    result = make_backend(handler).start_inspection("route-1", ROBOT)
    assert result["task_id"] == "task-1" and result["status"] == "dispatching"
    assert len(requests) == 2


@pytest.mark.parametrize("changes", [
    {"robot_id": "other-robot"}, {"task_id": ".."}, {"task_id": ""},
    {"route_id": "other-route"}, {"status": "running"},
])
def test_invalid_created_task_is_never_started(make_backend, changes):
    requests = []

    def handler(request):
        requests.append(request)
        return envelope(task(**changes))

    with pytest.raises(OmniError) as caught:
        make_backend(handler).start_inspection("route-1", ROBOT)
    assert len(requests) == 1
    assert caught.value.details["phase"] == "create_inspection_task"
    assert caught.value.details["outcome_unknown"] is True


@pytest.mark.parametrize("failure_phase", [1, 2])
def test_inspection_timeout_never_retries_and_preserves_known_task(make_backend, failure_phase):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == failure_phase:
            raise httpx.ReadTimeout(TOKEN, request=request)
        return envelope(task())

    with pytest.raises(OmniError) as caught:
        make_backend(handler).start_inspection("route-1", ROBOT)
    error = caught.value
    assert error.code == "upstream_timeout"
    assert error.details["outcome_unknown"] is True
    assert error.details["retry_writes"] is False
    assert error.details["recover_by_get"]["method"] == "GET"
    assert len(requests) == failure_phase
    assert TOKEN not in json.dumps(error.as_dict())
    if failure_phase == 2:
        assert error.details["task_id"] == "task-1"
        assert error.details["recover_by_get"]["path"].endswith("/patrol/tasks/task-1")
    else:
        assert "task_id" not in error.details
        assert error.details["recover_by_get"]["params"] == {"robot_id": ROBOT}


def test_explicit_start_failure_keeps_created_task_for_recovery(make_backend):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return envelope(task())
        return httpx.Response(400, json={"code": 400000, "message": TOKEN})

    with pytest.raises(OmniError) as caught:
        make_backend(handler).start_inspection("route-1", ROBOT)
    assert caught.value.details["task_id"] == "task-1"
    assert caught.value.details["upstream_code"] == 400000
    assert len(requests) == 2
    assert TOKEN not in json.dumps(caught.value.as_dict())


def test_start_response_identity_mismatch_is_not_success(make_backend):
    requests = []

    def handler(request):
        requests.append(request)
        return envelope(task() if len(requests) == 1 else task("running", task_id="other-task"))

    with pytest.raises(OmniError) as caught:
        make_backend(handler).start_inspection("route-1", ROBOT)
    assert caught.value.details["task_id"] == "task-1"
    assert caught.value.details["outcome_unknown"] is True
    assert len(requests) == 2


def test_navigation_uses_pose_for_plan_then_plan_id_only_for_goal(make_backend):
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        if len(requests) == 1:
            assert request.method == "POST" and request.url.path.endswith("/navigation/plans")
            assert body["map_id"] == "map-1" and body["map_version"] == 1
            assert body["goal_pose"]["position"] == {"x": 2, "y": 3, "z": 0}
            orientation = body["goal_pose"]["orientation"]
            assert orientation["z"] == pytest.approx(math.sin(math.pi / 4))
            assert orientation["w"] == pytest.approx(math.cos(math.pi / 4))
            return envelope(plan(body))
        assert request.method == "POST" and request.url.path.endswith("/navigation/goals")
        assert body == {"plan_id": "plan-1"}
        return envelope(goal())

    result = make_backend(handler).navigate_to("map-1", 1, 2, 3, math.pi / 2, ROBOT)
    assert result["goal_id"] == "goal-1" and result["status"] == "pending"
    assert len(requests) == 2


@pytest.mark.parametrize("changes,code", [
    ({"robot_id": "other-robot"}, "robot_mismatch"),
    ({"map_id": "other-map"}, "map_mismatch"),
    ({"map_version": 2}, "map_mismatch"),
    ({"map_version": True}, "map_mismatch"),
    ({"plan_id": ".."}, "upstream_invalid_response"),
    ({"status": "failed"}, "plan_not_executable"),
    ({"executable": False}, "plan_not_executable"),
    ({"executable": 1}, "plan_not_executable"),
    ({"expires_at": "2000-01-01T00:00:00Z"}, "plan_expired"),
    ({"expires_at": "2100-01-01T00:00:00"}, "upstream_invalid_response"),
    ({"expires_at": "bad-date"}, "upstream_invalid_response"),
    ({"goal_pose": {"position": {"x": 999, "y": 3, "z": 0}}}, "goal_mismatch"),
    ({"goal_pose": {"position": {"x": 10 ** 500, "y": 3, "z": 0}}}, "goal_mismatch"),
])
def test_invalid_or_expired_plan_never_submits_goal(make_backend, changes, code):
    requests = []

    def handler(request):
        requests.append(request)
        return envelope(plan(json.loads(request.content), **changes))

    with pytest.raises(OmniError) as caught:
        make_backend(handler).navigate_to("map-1", 1, 2, 3, 0, ROBOT)
    assert caught.value.code == code
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/plans")
    assert caught.value.details["phase"] == "create_navigation_plan"


@pytest.mark.parametrize("failure_phase", [1, 2])
def test_navigation_timeout_preserves_plan_and_never_retries(make_backend, failure_phase):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == failure_phase:
            raise httpx.ReadTimeout(TOKEN, request=request)
        return envelope(plan(json.loads(request.content)))

    with pytest.raises(OmniError) as caught:
        make_backend(handler).navigate_to("map-1", 1, 2, 3, 0, ROBOT)
    assert caught.value.code == "upstream_timeout"
    assert caught.value.details["outcome_unknown"] is True
    assert len(requests) == failure_phase
    if failure_phase == 2:
        assert caught.value.details["plan_id"] == "plan-1"
        assert caught.value.details["recover_by_get"]["path"].endswith("/plans/plan-1")
        assert "does not confirm execution" in caught.value.details["recover_by_get"]["note"]
        assert caught.value.details["recover_by_get"]["also"]["path"].endswith("/status")
    else:
        assert "plan_id" not in caught.value.details


@pytest.mark.parametrize("changes", [
    {"goal_id": ""}, {"plan_id": "other-plan"}, {"robot_id": "other-robot"},
    {"map_id": "other-map"}, {"map_version": 2}, {"status": ""},
])
def test_invalid_goal_reply_returns_uncertainty_with_known_plan(make_backend, changes):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return envelope(plan(json.loads(request.content)))
        return envelope(goal(**changes))

    with pytest.raises(OmniError) as caught:
        make_backend(handler).navigate_to("map-1", 1, 2, 3, 0, ROBOT)
    assert len(requests) == 2
    assert caught.value.details["plan_id"] == "plan-1"
    assert caught.value.details["outcome_unknown"] is True


@pytest.mark.parametrize("values", [
    ("map-1", 0, 2, 3, 0), ("map-1", True, 2, 3, 0),
    ("map-1", 1.5, 2, 3, 0), ("..", 1, 2, 3, 0),
    ("map-1", 4294967296, 2, 3, 0),
    ("map-1", 1, float("nan"), 3, 0), ("map-1", 1, 2, float("inf"), 0),
    ("map-1", 1, 2, 3, float("-inf")), ("map-1", 1, True, 3, 0),
    ("map-1", 1, "2", 3, 0), ("map-1", 1, 10 ** 500, 3, 0),
])
def test_invalid_navigation_arguments_make_no_network_requests(make_backend, values):
    def handler(_):
        pytest.fail("Invalid navigation input must not reach transport")

    with pytest.raises(OmniError) as caught:
        make_backend(handler).navigate_to(*values, ROBOT)
    assert caught.value.code == "invalid_argument"


def test_cancel_inspection_verifies_ownership_then_posts_cancel_and_reads_status(make_backend):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return envelope(task("running"))
        if len(requests) == 2:
            assert request.method == "POST" and request.url.path.endswith("/task-1/cancel")
            return envelope()  # Actual backend omits data for this operation.
        assert request.method == "GET"
        return envelope(task("cancelling"))

    result = make_backend(handler).cancel_task("task-1", ROBOT)
    assert result["status"] == "cancelling" and result["cancel_requested"] is True
    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.parametrize("kind", ["inspection", "navigation"])
def test_cancel_wrong_robot_never_sends_mutation(make_backend, kind):
    requests = []

    def handler(request):
        requests.append(request)
        return envelope(task("running", robot_id="other") if kind == "inspection" else goal("executing", robot_id="other"))

    with pytest.raises(OmniError) as caught:
        make_backend(handler).cancel_task("task-1" if kind == "inspection" else "goal-1", ROBOT, kind)
    assert caught.value.code == "robot_mismatch"
    assert len(requests) == 1 and requests[0].method == "GET"


def test_cancel_navigation_uses_delete_goal_and_preserves_pending_cancel_status(make_backend):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/v1/robots/robot-demo/navigation/goals/goal-1"
        return envelope(goal("executing"))

    result = make_backend(handler).cancel_task("goal-1", ROBOT, "navigation")
    assert [request.method for request in requests] == ["GET", "DELETE"]
    assert result["status"] == "executing" and result["cancel_requested"] is True


@pytest.mark.parametrize("kind,status", [("inspection", "completed"), ("navigation", "succeeded")])
def test_cancel_terminal_task_is_read_only(make_backend, kind, status):
    requests = []

    def handler(request):
        requests.append(request)
        return envelope(task(status) if kind == "inspection" else goal(status))

    result = make_backend(handler).cancel_task("task-1" if kind == "inspection" else "goal-1", ROBOT, kind)
    assert result["already_terminal"] is True and result["cancel_requested"] is False
    assert len(requests) == 1 and requests[0].method == "GET"


@pytest.mark.parametrize("kind", ["inspection", "navigation"])
def test_cancel_timeout_is_not_retried_and_returns_recovery_id(make_backend, kind):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return envelope(task("running") if kind == "inspection" else goal("executing"))
        raise httpx.ReadTimeout(TOKEN, request=request)

    with pytest.raises(OmniError) as caught:
        make_backend(handler).cancel_task("task-1" if kind == "inspection" else "goal-1", ROBOT, kind)
    assert len(requests) == 2
    assert caught.value.code == "upstream_timeout"
    assert caught.value.details["outcome_unknown"] is True
    assert caught.value.details["task_id" if kind == "inspection" else "goal_id"] == ("task-1" if kind == "inspection" else "goal-1")


def test_inspection_cancel_followup_read_failure_does_not_claim_cancelled(make_backend):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return envelope(task("running"))
        if len(requests) == 2:
            return envelope()
        return httpx.Response(503, json={"code": 503001, "message": TOKEN})

    with pytest.raises(OmniError) as caught:
        make_backend(handler).cancel_task("task-1", ROBOT)
    assert len(requests) == 3
    assert caught.value.details["outcome_unknown"] is True
    assert caught.value.details["task_id"] == "task-1"


def test_navigation_read_does_not_require_write_permission(make_backend):
    seen = []

    def handler(request):
        seen.append(request)
        return envelope(goal("executing"))

    result = make_backend(handler, allow_writes=False).get_navigation_task("goal-1", ROBOT)
    assert result["goal_id"] == "goal-1"
    assert len(seen) == 1 and seen[0].method == "GET"


def test_mutation_redirect_is_not_followed(make_backend):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example.test/" + TOKEN})

    with pytest.raises(OmniError) as caught:
        make_backend(handler).start_inspection("route-1", ROBOT)
    assert len(seen) == 1
    assert caught.value.details["outcome_unknown"] is True
    assert TOKEN not in json.dumps(caught.value.as_dict())
