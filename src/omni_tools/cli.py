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

    def command(group, name, help_text):
        parent = groups.add_parser(group)
        sub = parent.add_subparsers(dest="command", required=True)
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
        elif args.group == "inspection":
            result = service.get_inspection_result(args.task_id, args.robot)
        else:
            result = service.get_task(args.task_id, args.robot)
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
