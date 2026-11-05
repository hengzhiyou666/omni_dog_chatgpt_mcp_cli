from types import SimpleNamespace

import pytest

from omni_tools.remote_probe import LOCALIZATION_TOPICS, TOPICS, ProbeError, perform


def test_idle_status_remains_complete_without_optional_global_odometry():
    samples = {key: {} for key in TOPICS}
    observations = {key: {"available": key in samples} for key in TOPICS | LOCALIZATION_TOPICS}
    reader = SimpleNamespace(topics=lambda keys: (samples, observations))
    result = perform({"operation": "get_robot_status", "robot_id": "dog4"}, reader)
    assert result["complete"] is True
    assert result["body_odom_global"] is None
    assert result["tf_ready_status"] is None


def test_status_preserves_tf_and_stamped_odometry_for_localization_diagnostics():
    odom = {"header": {"stamp": {"sec": 100, "nanosec": 0}, "frame_id": "omni_map"},
            "child_frame_id": "omni_base_link"}
    samples = {"runtime_status": {"phase": 2, "ready": True},
               "tf_ready_status": {"data": True}, "body_odom_global": odom}
    observations = {"tf_ready_status": {"available": True, "may_be_retained": True},
                    "body_odom_global": {"available": True, "source_stamp_age_seconds": 0.1}}
    reader = SimpleNamespace(topics=lambda keys: (samples, observations))
    result = perform({"operation": "get_robot_status", "robot_id": "dog4"}, reader)
    assert result["body_odom_global"] == odom
    assert result["tf_ready_status"] == {"data": True}
    assert result["observations"]["tf_ready_status"]["may_be_retained"] is True
    assert result["observations"]["body_odom_global"]["source_stamp_age_seconds"] == 0.1


def test_retained_tf_alone_is_not_live_robot_status():
    reader = SimpleNamespace(topics=lambda keys: ({"tf_ready_status": {"data": True}}, {}))
    with pytest.raises(ProbeError, match="No live robot status"):
        perform({"operation": "get_robot_status", "robot_id": "dog4"}, reader)
