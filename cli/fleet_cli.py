"""CLI implementation for Veya Agent Fleet management (spec §52).

Commands:
- veya fleet status --json
- veya fleet agents list --json
- veya fleet agents get <id> --json
- veya fleet queue list --json
- veya fleet placements list --json
- veya fleet resources --json
- veya fleet migrations list --json
- veya fleet drain <agent|fleet>
- veya fleet recover <agent|mission>
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from veya.fleet import FleetController


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2))


def run_fleet_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="veya fleet", description="Veya Fleet Supervision CLI")
    subparsers = parser.add_subparsers(dest="fleet_command")

    # status
    p_status = subparsers.add_parser("status", help="Get fleet status")
    p_status.add_argument("--json", action="store_true", default=True)

    # agents
    p_agents = subparsers.add_parser("agents", help="Agent instances management")
    agents_sub = p_agents.add_subparsers(dest="agents_subcommand")
    p_agents_list = agents_sub.add_parser("list")
    p_agents_list.add_argument("--json", action="store_true", default=True)
    p_agents_get = agents_sub.add_parser("get")
    p_agents_get.add_argument("agent_id")
    p_agents_get.add_argument("--json", action="store_true", default=True)

    # queue
    p_queue = subparsers.add_parser("queue", help="Fleet mission queue")
    queue_sub = p_queue.add_subparsers(dest="queue_subcommand")
    p_queue_list = queue_sub.add_parser("list")
    p_queue_list.add_argument("--json", action="store_true", default=True)

    # placements
    p_placements = subparsers.add_parser("placements", help="Mission placements")
    placements_sub = p_placements.add_subparsers(dest="placements_subcommand")
    p_placements_list = placements_sub.add_parser("list")
    p_placements_list.add_argument("--json", action="store_true", default=True)

    # resources
    p_resources = subparsers.add_parser("resources", help="Resource pools and allocations")
    p_resources.add_argument("--json", action="store_true", default=True)

    # migrations
    p_migrations = subparsers.add_parser("migrations", help="Mission migrations")
    migrations_sub = p_migrations.add_subparsers(dest="migrations_subcommand")
    p_migrations_list = migrations_sub.add_parser("list")
    p_migrations_list.add_argument("--json", action="store_true", default=True)

    # drain
    p_drain = subparsers.add_parser("drain", help="Drain agent or entire fleet")
    p_drain.add_argument("target", help="Agent instance ID or 'fleet'")

    # recover
    p_recover = subparsers.add_parser("recover", help="Recover mission on new agent placement")
    p_recover.add_argument("target", help="Mission ID or Agent instance ID")

    args = parser.parse_args(argv)
    controller = FleetController()

    if args.fleet_command == "status":
        health = controller.get_health()
        _print_json(health.to_dict())
        return 0

    elif args.fleet_command == "agents":
        if getattr(args, "agents_subcommand", None) == "get":
            agent = controller.registry.get_agent(args.agent_id)
            if not agent:
                print(json.dumps({"error": f"Agent '{args.agent_id}' not found"}))
                return 1
            _print_json(agent.to_dict())
            return 0
        else:
            agents = controller.registry.list_agents()
            _print_json([a.to_dict() for a in agents])
            return 0

    elif args.fleet_command == "queue":
        entries = controller.registry.list_queue()
        _print_json([e.to_dict() for e in entries])
        return 0

    elif args.fleet_command == "placements":
        placements = controller.registry.list_placements()
        _print_json([p.to_dict() for p in placements])
        return 0

    elif args.fleet_command == "resources":
        pools = controller.registry.list_pools()
        reservations = controller.registry.list_reservations()
        _print_json(
            {
                "saturation": controller.resources.get_saturation(),
                "pools": [p.to_dict() for p in pools],
                "reservations": [r.to_dict() for r in reservations],
            }
        )
        return 0

    elif args.fleet_command == "migrations":
        migrations = controller.registry.list_migrations()
        _print_json([m.to_dict() for m in migrations])
        return 0

    elif args.fleet_command == "drain":
        if args.target.lower() == "fleet":
            controller.drain_fleet()
            _print_json({"status": "FLEET_DRAINING"})
        else:
            controller.drain_agent(args.target)
            _print_json({"status": "AGENT_DRAINING", "agent_id": args.target})
        return 0

    elif args.fleet_command == "recover":
        try:
            mig = controller.recover_mission(args.target)
            _print_json(mig.to_dict())
            return 0
        except Exception as e:
            _print_json({"error": str(e)})
            return 1

    parser.print_help()
    return 1
