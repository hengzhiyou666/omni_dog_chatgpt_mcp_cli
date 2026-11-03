"""Offline HTTP contract tests; no real cloud or robot endpoints are used."""

import json

import httpx
import pytest

from omni_tools.backends.http import HTTPBackend, _api_base
from omni_tools.config import Settings
from omni_tools.errors import OmniError


TOKEN = "private-test-token-do-not-print"


def settings(base_url="https://cloud.example.test"):
    return Settings(backend="http", base_url=base_url, token=TOKEN, robot_id="robot-demo", timeout=2)


def ok(data):
    return httpx.Response(200, json={"code": 0, "message": "success", "data": data})


@pytest.fixture
def make_backend():
    clients = []

    def make(handler, base_url="https://cloud.example.test", **client_options):
        client = httpx.Client(transport=httpx.MockTransport(handler), **client_options)
        clients.append(client)
        return HTTPBackend(settings(base_url), client=client)

    yield make
    for client in clients:
        client.close()


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://cloud.example.test", "https://cloud.example.test/api/v1"),
        ("https://cloud.example.test/", "https://cloud.example.test/api/v1"),
        ("https://cloud.example.test/proxy", "https://cloud.example.test/proxy/api/v1"),
        ("https://cloud.example.test/proxy/api/v1/", "https://cloud.example.test/proxy/api/v1"),
        ("http://localhost:8080", "http://localhost:8080/api/v1"),
    ],
)
def test_base_url_normalization(base, expected):
    assert _api_base(base) == expected


@pytest.mark.parametrize(
    "base",
    [
        "https://user:secret@cloud.example.test", "https://cloud.example.test?token=secret",
        "https://cloud.example.test#secret", "file:///etc/passwd", "https://cloud.example.test/../proxy",
        "https://cloud.example.test/%2e%2e/proxy", "https://cloud.example.test/api/v1/api/v1",
        "https://cloud.example.test/api/v1/tasks", "https://cloud.example.test:invalid",
        "https://cloud.example.test/\nsecret", "https://cloud.example.test/proxy\\other",
    ],
)
def test_unsafe_base_url_does_not_echo_secrets(base):
    with pytest.raises(OmniError) as caught:
        _api_base(base)
    assert caught.value.code == "configuration_error"
    assert "secret" not in json.dumps(caught.value.as_dict())


def test_status_auth_and_proxy_prefix(make_backend):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        return ok({"robot_id": "robot-demo", "online": False, "battery_valid": False})

    backend = make_backend(handler, "https://cloud.example.test/proxy/api/v1/")
    result = backend.get_robot_status("robot-demo")
    assert result["online"] is False
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/proxy/api/v1/robots/robot-demo/status"


def test_identifiers_are_single_encoded_path_segments(make_backend):
    task_id = "task +?#%"
    seen = []

    def handler(request):
        seen.append(request.url.raw_path)
        return ok({"task_id": task_id, "robot_id": "robot-demo", "status": "completed"})

    result = make_backend(handler).get_task(task_id, "robot-demo")
    assert result["task_id"] == task_id
    assert seen == [b"/api/v1/patrol/tasks/task%20%2B%3F%23%25"]


@pytest.mark.parametrize("identifier", ["", ".", "..", "bad\nidentifier", "bad\x7fidentifier"])
def test_invalid_identifiers_never_send_requests(make_backend, identifier):
    def handler(_):
        pytest.fail("invalid identifiers must be rejected before transport")

    with pytest.raises(OmniError, match="identifier"):
        make_backend(handler).get_task(identifier, "robot-demo")


@pytest.mark.parametrize("status,code", [(401, 4011001), (404, 4040000), (503, 503001)])
def test_http_errors_keep_safe_codes_and_redact_upstream_text(make_backend, status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"code": code, "message": TOKEN, "details": {"token": TOKEN}})

    with pytest.raises(OmniError) as caught:
        make_backend(handler).get_robot_status("robot-demo")
    error = caught.value.as_dict()
    assert error["code"] == "upstream_http_error"
    assert error["details"] == {"http_status": status, "upstream_code": code}
    assert TOKEN not in json.dumps(error)
    assert len(calls) == 1


def test_business_error_in_http_success_is_not_success(make_backend):
    backend = make_backend(lambda _: httpx.Response(200, json={"code": 409000, "message": TOKEN}))
    with pytest.raises(OmniError) as caught:
        backend.get_robot_status("robot-demo")
    assert caught.value.code == "upstream_rejected"
    assert caught.value.details["upstream_code"] == 409000
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"not JSON", b'[]', b'{"data": {}}', b'{"code": false, "data": {}}',
        b'{"code": "0", "data": {}}', b'{"code": 0}',
        b'{"code": 0, "data": {"robot_id":"robot-demo", "value":NaN}}',
        b'{"code": 0, "data": {"robot_id":"robot-demo", "value":1e999}}',
        b'{"code": 0, "data": {"robot_id":"robot-demo", "value":-Infinity}}',
    ],
)
def test_malformed_or_nonfinite_json_is_rejected(make_backend, raw):
    backend = make_backend(lambda _: httpx.Response(200, content=raw))
    with pytest.raises(OmniError) as caught:
        backend.get_robot_status("robot-demo")
    assert caught.value.code == "upstream_invalid_response"


def test_redirects_never_follow_even_with_injected_client_default(make_backend):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://attacker.example.test/" + TOKEN})

    backend = make_backend(handler, follow_redirects=True)
    with pytest.raises(OmniError) as caught:
        backend.get_robot_status("robot-demo")
    assert caught.value.code == "upstream_http_error"
    assert seen == ["cloud.example.test"]
    assert TOKEN not in json.dumps(caught.value.as_dict())


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout, "upstream_timeout"), (httpx.ConnectError, "upstream_unavailable")])
def test_transport_failures_are_safe_and_not_retried(make_backend, exception, code):
    seen = []

    def handler(request):
        seen.append(request)
        raise exception(TOKEN, request=request)

    with pytest.raises(OmniError) as caught:
        make_backend(handler).get_robot_status("robot-demo")
    assert caught.value.code == code
    assert TOKEN not in json.dumps(caught.value.as_dict())
    assert len(seen) == 1


def test_maps_distinguish_cloud_and_robot_identifiers(make_backend):
    backend = make_backend(lambda _: ok({"maps": [{"map_id": "cloud-map", "robot_map_id": "native-map", "robot_id": "robot-demo"}], "total": 1}))
    result = backend.list_maps("robot-demo")
    assert result["maps"][0]["cloud_map_id"] == "cloud-map"
    assert result["maps"][0]["robot_map_id"] == "native-map"


def test_routes_paginate_global_catalog_and_scope_by_cloud_map_association(make_backend):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/maps"):
            return ok({"maps": [{"map_id": "cloud-map", "robot_id": "robot-demo"}], "total": 1})
        assert request.url.path == "/api/v1/patrol/routes"
        assert "robot_id" not in request.url.params  # This upstream filter does not exist.
        if request.url.params["offset"] == "0":
            return ok({"routes": [
                {"route_id": "cloud-route", "robot_route_id": "native-route", "map_id": "cloud-map"},
                {"route_id": "other-route", "map_id": "other-map"},
            ], "total": 3})
        assert request.url.params["offset"] == "2"
        return ok({"routes": [{"route_id": "unbound-route"}], "total": 3})

    result = make_backend(handler).list_routes("robot-demo")
    assert result["scope"] == "cloud_map_association"
    assert "not verified" in result["scope_note"]
    assert result["total"] == 1 and result["upstream_total"] == 3
    assert result["routes"][0]["cloud_route_id"] == "cloud-route"
    assert result["routes"][0]["robot_route_id"] == "native-route"
    assert len(seen) == 3 and all(request.method == "GET" for request in seen)


def test_incomplete_route_pagination_is_not_reported_as_complete(make_backend):
    def handler(request):
        if request.url.path.endswith("/maps"):
            return ok({"maps": [], "total": 0})
        return ok({"routes": [], "total": 4})

    with pytest.raises(OmniError) as caught:
        make_backend(handler).list_routes("robot-demo")
    assert caught.value.code == "upstream_invalid_response"


@pytest.mark.parametrize("operation", ["get_robot_status", "list_maps", "get_task"])
def test_wrong_robot_resources_are_rejected(make_backend, operation):
    foreign = {"robot_id": "another-robot", "map_id": "map", "task_id": "task"}
    data = {"maps": [foreign], "total": 1} if operation == "list_maps" else foreign
    backend = make_backend(lambda _: ok(data))
    with pytest.raises(OmniError) as caught:
        if operation == "get_task":
            backend.get_task("task", "robot-demo")
        else:
            getattr(backend, operation)("robot-demo")
    assert caught.value.code == "robot_mismatch"


def test_inspection_result_joins_actual_array_contracts(make_backend):
    seen = []
    task = {"task_id": "task", "robot_id": "robot-demo", "status": "completed", "result": {"completed": 2}}
    event = {"task_id": "task", "event_id": "evt", "event_type": "task.completed"}
    evidence = {"task_id": "task", "robot_id": "robot-demo", "evidence_id": "ev", "status": "ready", "download_url": "/api/v1/inspection/evidence/ev/content"}

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/events"):
            return ok([event])
        if request.url.path.endswith("/evidence"):
            return ok([evidence])
        return ok(task)

    result = make_backend(handler).get_inspection_result("task", "robot-demo")
    assert result == {"task": task, "events": [event], "evidence": [evidence]}
    assert len(seen) == 3
    assert all(request.method == "GET" for request in seen)
    assert not any(request.url.path.endswith("/content") for request in seen)


def test_wrong_task_stops_result_fetch_before_events_or_evidence(make_backend):
    seen = []

    def handler(request):
        seen.append(request)
        return ok({"task_id": "task", "robot_id": "another-robot"})

    with pytest.raises(OmniError) as caught:
        make_backend(handler).get_inspection_result("task", "robot-demo")
    assert caught.value.code == "robot_mismatch"
    assert len(seen) == 1


def test_cross_task_evidence_is_rejected(make_backend):
    def handler(request):
        if request.url.path.endswith("/events"):
            return ok([])
        if request.url.path.endswith("/evidence"):
            return ok([{"task_id": "other-task", "robot_id": "robot-demo"}])
        return ok({"task_id": "task", "robot_id": "robot-demo"})

    with pytest.raises(OmniError) as caught:
        make_backend(handler).get_inspection_result("task", "robot-demo")
    assert caught.value.code == "upstream_invalid_response"


def test_backend_does_not_close_caller_owned_client(make_backend):
    backend = make_backend(lambda _: ok({"robot_id": "robot-demo"}))
    backend.close()
    assert backend.get_robot_status("robot-demo")["robot_id"] == "robot-demo"
