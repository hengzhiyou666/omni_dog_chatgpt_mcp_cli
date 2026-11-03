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
    parser = Parser(prog="omni", description="Omni robot queries via SSH or the inspection HTTP API")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--backend", choices=["ssh", "http"], default=None)
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
    return parser


def run(argv=None, *, service=None):
    args = None
    try:
        args = build_parser().parse_args(argv)
        service = service or OmniService(Settings.from_env(
            backend=args.backend, ssh_host=args.ssh_host, timeout=args.timeout))
        if args.group == "robot":
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
