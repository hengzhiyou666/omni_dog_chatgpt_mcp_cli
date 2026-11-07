"""The omni CLI. JSON output never contains Python NaN or diagnostic banners."""
import argparse
import sys

from . import __version__
from .config import Settings
from .errors import OmniError
from .service import OmniService
from .util import json_text


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise OmniError("invalid_argument", message)


def build_parser():
    parser = Parser(prog="omni", description="Omni robot queries and preview-first controls via local ROS, SSH or HTTP")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--backend", choices=["ssh", "local", "http"], default=None)
    parser.add_argument("--ssh-host", default=None)
    parser.add_argument("--timeout", type=float, default=None)
    groups = parser.add_subparsers(dest="group", required=True)
    commands = {}

    def command(group, name, help_text):
        if group not in commands:
            parent = groups.add_parser(group)
            commands[group] = parent.add_subparsers(dest="command", required=True)
        sub = commands[group]
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--json", action="store_true", help="Emit compact machine-readable JSON")
        return cmd

    status = command("robot", "status", "Read robot status")
    status.add_argument("robot_id", nargs="?", default=None)
    for group in ["maps", "routes"]:
        cmd = command(group, "list", f"List {group}")
        cmd.add_argument("--robot", default=None)
    result = command("inspection", "result", "Read inspection results and evidence")
    result.add_argument("task_id")
    result.add_argument("--robot", default=None)
    task = command("task", "status", "Read current task status")
    task.add_argument("task_id")
    task.add_argument("--robot", default=None)
    task.add_argument("--kind", choices=["inspection", "navigation"], default="inspection")
    start = command("inspection", "start", "Preview inspection start; execution requires explicit enablement")
    start.add_argument("--route", required=True)
    start.add_argument("--robot", default=None)
    start.add_argument("--execute", action="store_true", help="Request execution (disabled by default in server configuration)")
    nav = command("navigation", "start", "Preview navigation to a map coordinate")
    nav.add_argument("--robot", default=None)
    nav.add_argument("--map", dest="map_id", required=True)
    nav.add_argument("--map-version", type=int, required=True)
    nav.add_argument("--x", type=float, required=True)
    nav.add_argument("--y", type=float, required=True)
    nav.add_argument("--yaw", type=float, default=0.0, help="Final heading in radians")
    nav.add_argument("--execute", action="store_true")
    cancel = command("task", "cancel", "Preview cancellation; no implicit stop command")
    cancel.add_argument("task_id")
    cancel.add_argument("--robot", default=None)
    cancel.add_argument("--kind", choices=["inspection", "navigation"], default="inspection")
    cancel.add_argument("--execute", action="store_true")

    def posture_options(cmd, method):
        cmd.set_defaults(posture_method=method)
        cmd.add_argument("--robot", default=None)
        cmd.add_argument("--execute", action="store_true",
                         help="Execute the posture change with configured write permission; otherwise preview")

    for name, method, help_text in [
        ("stand", "stand_up", "Preview standing up (站立/起立/站起来)"),
        ("lie", "lie_down", "Preview lying down (坐下/卧下/趴下 all mean lie down)"),
    ]:
        posture_options(command("posture", name, help_text), method)

    for name, method in [
        ("站立", "stand_up"), ("起立", "stand_up"), ("站起来", "stand_up"),
        ("坐下", "lie_down"), ("卧下", "lie_down"), ("趴下", "lie_down"),
    ]:
        alias = groups.add_parser(name, help="Preview standing up" if method == "stand_up" else "Preview lying down")
        alias.add_argument("--json", action="store_true", help="Emit compact machine-readable JSON")
        posture_options(alias, method)

    def forward_options(cmd):
        cmd.set_defaults(motion_method="move_forward")
        cmd.add_argument("--distance", dest="distance_m", type=float, default=1.0,
                         help="Body-forward distance in meters, 0.1..2 (default: 1)")
        cmd.add_argument("--measurement-mode", choices=("odometry", "timed"), default="odometry",
                         help="odometry measures distance; timed estimates at 0.35 m/s without SLAM")
        cmd.add_argument("--robot", default=None)
        cmd.add_argument("--execute", action="store_true",
                         help="Execute with robot-side feedback and configured write permission; otherwise preview")

    forward_options(command("motion", "forward", "Preview a body-forward move using robot-side feedback"))
    forward_alias = groups.add_parser("向前", help="Preview moving forward (default: 1 meter)")
    forward_alias.add_argument("--json", action="store_true", help="Emit compact machine-readable JSON")
    forward_options(forward_alias)

    move = command("motion", "move", "Preview a timed move relative to the robot's heading")
    move.set_defaults(motion_method="move")
    move.add_argument("--direction", choices=("forward", "backward", "left", "right"), required=True)
    move.add_argument("--distance", dest="distance_m", type=float, default=1.0,
                      help="Estimated distance in meters, 0.1..2 (default: 1)")
    rotate = command("motion", "rotate", "Preview an estimated timed rotation in place")
    rotate.set_defaults(motion_method="rotate")
    rotate.add_argument("--angle", dest="angle_deg", type=float, default=90.0,
                        help="Estimated angle in degrees, 1..360 (default: 90)")
    rotate.add_argument("--direction", choices=("clockwise", "counterclockwise"), default="clockwise")
    for cmd in (move, rotate):
        cmd.add_argument("--robot", default=None)
        cmd.add_argument("--execute", action="store_true",
                         help="Execute with configured write permission; otherwise preview")

    map_fields = ("map_id", "map_version", "map_checksum", "initial_x", "initial_y", "initial_z", "initial_yaw")

    def runtime_options(cmd, method, fields=()):
        cmd.set_defaults(runtime_method=method, runtime_fields=fields)
        cmd.add_argument("--robot", default=None)
        cmd.add_argument("--execute", action="store_true", help="Execute and wait for the runtime result; otherwise preview")

    def map_options(cmd):
        cmd.add_argument("--map", dest="map_id", required=True)
        cmd.add_argument("--map-version", type=int, required=True)
        cmd.add_argument("--map-checksum", required=True)
        for field in ("initial_x", "initial_y", "initial_z", "initial_yaw"):
            cmd.add_argument("--" + field.replace("_", "-"), type=float, default=0.0)

    localization = command("localization", "start", "Load the named map and wait for localization READY")
    map_options(localization)
    runtime_options(localization, "start_localization", map_fields)
    runtime_options(command("runtime", "idle", "Return to idle without discarding unfinished work"), "set_runtime_idle")

    record = commands["routes"].add_parser("record", help="Start, save or discard route recording")
    recording = record.add_subparsers(dest="record_command", required=True)
    for action in ("start", "save", "discard"):
        cmd = recording.add_parser(action)
        cmd.add_argument("--json", action="store_true", help="Emit compact machine-readable JSON")
        if action == "start":
            cmd.add_argument("--route", dest="route_id", required=True)
            map_options(cmd)
            runtime_options(cmd, "start_route_recording", ("route_id", *map_fields))
        else:
            cmd.add_argument("--operation", dest="recording_operation_id", required=True)
            cmd.set_defaults(disposition=action)
            runtime_options(cmd, "finish_route_recording", ("recording_operation_id", "disposition"))
    return parser


def run(argv=None, *, service=None):
    args = None
    try:
        args = build_parser().parse_args(argv)
        service = service or OmniService(Settings.from_env(
            backend=args.backend, ssh_host=args.ssh_host, timeout=args.timeout))
        if getattr(args, "runtime_method", None):
            fields = {field: getattr(args, field) for field in args.runtime_fields}
            result = getattr(service, args.runtime_method)(**fields, robot_id=args.robot, execute=args.execute)
        elif getattr(args, "posture_method", None):
            result = getattr(service, args.posture_method)(args.robot, args.execute)
        elif getattr(args, "motion_method", None):
            if args.motion_method == "move":
                result = service.move(args.direction, args.distance_m, args.robot, args.execute)
            elif args.motion_method == "rotate":
                result = service.rotate(args.angle_deg, args.direction, args.robot, args.execute)
            elif args.measurement_mode == "odometry":
                result = service.move_forward(args.distance_m, args.robot, args.execute)
            else:
                result = service.move_forward(args.distance_m, args.robot, args.execute, args.measurement_mode)
        elif args.group == "robot":
            result = service.get_robot_status(args.robot_id)
        elif args.group == "maps":
            result = service.list_maps(args.robot)
        elif args.group == "routes":
            result = service.list_routes(args.robot)
        elif args.group == "inspection" and args.command == "start":
            result = service.start_inspection(args.route, args.robot, args.execute)
        elif args.group == "inspection":
            result = service.get_inspection_result(args.task_id, args.robot)
        elif args.group == "navigation":
            result = service.navigate_to(args.map_id, args.map_version, args.x, args.y,
                                         args.yaw, args.robot, args.execute)
        elif args.command == "cancel":
            result = service.cancel_task(args.task_id, args.robot, args.kind, args.execute)
        else:
            result = service.get_task(args.task_id, args.robot, args.kind)
        print(json_text(result, pretty=not args.json))
        return 0
    except OmniError as exc:
        print(json_text({"ok": False, "error": exc.as_dict()}, pretty=not (args and args.json)))
        return 2
    except KeyboardInterrupt:
        print(json_text({"ok": False, "error": {"code": "interrupted", "message": "Query interrupted"}}))
        return 130


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
