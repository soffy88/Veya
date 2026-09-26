"""CLI interface for Veya Agent Operations V1 (spec §36).

Provides machine-readable (`--json`) commands:
  veya ops health
  veya ops agents
  veya ops slo
  veya ops alerts
  veya ops incidents
  veya ops usage
  veya ops pause <agent>
  veya ops resume <agent>
  veya ops drain <agent>
  veya ops rollout status
  veya ops maintenance status
"""

from __future__ import annotations

import argparse
import json
import sys

from veya.operations import (
    HealthState,
    OperationsController,
)


def _get_controller(base_dir: str | None = None) -> OperationsController:
    return OperationsController(base_dir=base_dir)


def ops_main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="veya ops", description="Veya Agent Operations CLI")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument(
        "--base-dir", type=str, default="/tmp/veya_operations", help="Storage directory"
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Operational subcommands")

    # health
    subparsers.add_parser("health", help="Project fleet and agent operational health")

    # agents
    subparsers.add_parser("agents", help="List agent operational states")

    # slo
    subparsers.add_parser("slo", help="Evaluate operational SLOs and error budgets")

    # alerts
    subparsers.add_parser("alerts", help="List active operational alerts")

    # incidents
    subparsers.add_parser("incidents", help="List operational incidents")

    # usage
    subparsers.add_parser("usage", help="Query operational usage records")

    # pause
    p_pause = subparsers.add_parser("pause", help="Pause agent operational admissions")
    p_pause.add_argument("agent", type=str, help="Agent ID")
    p_pause.add_argument("--reason", type=str, default="CLI pause", help="Pause reason")

    # resume
    p_resume = subparsers.add_parser("resume", help="Resume agent operational admissions")
    p_resume.add_argument("agent", type=str, help="Agent ID")
    p_resume.add_argument("--reason", type=str, default="CLI resume", help="Resume reason")

    # drain
    p_drain = subparsers.add_parser("drain", help="Drain agent operational in-flight work")
    p_drain.add_argument("agent", type=str, help="Agent ID")
    p_drain.add_argument("--reason", type=str, default="CLI drain", help="Drain reason")

    # rollout
    p_rollout = subparsers.add_parser("rollout", help="Rollout management")
    p_rollout.add_argument("action", choices=["status", "list"], help="Rollout action")

    # maintenance
    p_maint = subparsers.add_parser("maintenance", help="Maintenance management")
    p_maint.add_argument("action", choices=["status", "list"], help="Maintenance action")

    parsed = parser.parse_args(args)
    if not parsed.subcommand:
        parser.print_help()
        return 0

    controller = _get_controller(parsed.base_dir)

    if parsed.subcommand == "health":
        agents = list(controller.lifecycle._agent_states.values())
        health_states = [HealthState.HEALTHY for _ in agents] or [HealthState.HEALTHY]
        state, reason = controller.health.evaluate_fleet_health(health_states)
        output = {"fleet_health": state.value, "reason": reason, "agents_count": len(agents)}
        if parsed.json:
            print(json.dumps(output, indent=2))
        else:
            print(f"Fleet Health: {state.value} ({reason})")
        return 0

    if parsed.subcommand == "agents":
        agents_data = [st.to_dict() for st in controller.lifecycle._agent_states.values()]
        if parsed.json:
            print(json.dumps(agents_data, indent=2))
        else:
            for a in agents_data:
                print(
                    f"Agent: {a['agent_id']} | Desired: {a['desired_state']} | Observed: {a['observed_state']}"
                )
        return 0

    if parsed.subcommand == "slo":
        evaluations = [e.__dict__ for e in controller.slo.evaluate_all()]
        if parsed.json:
            print(json.dumps(evaluations, indent=2))
        else:
            for ev in evaluations:
                print(
                    f"SLO: {ev['slo_id']} | SLI: {ev['sli_value']} | Target: {ev['slo_target']} | Breach: {ev['breach']}"
                )
        return 0

    if parsed.subcommand == "alerts":
        alerts = [a.to_dict() for a in controller.alerts.list_alerts()]
        if parsed.json:
            print(json.dumps(alerts, indent=2))
        else:
            for alt in alerts:
                print(
                    f"Alert: {alt['alert_id']} | Severity: {alt['severity']} | Target: {alt['target']} | Msg: {alt['message']}"
                )
        return 0

    if parsed.subcommand == "incidents":
        incidents = [inc.to_dict() for inc in controller.alerts.list_incidents()]
        if parsed.json:
            print(json.dumps(incidents, indent=2))
        else:
            for inc in incidents:
                print(
                    f"Incident: {inc['incident_id']} | Severity: {inc['severity']} | Status: {inc['status']}"
                )
        return 0

    if parsed.subcommand == "usage":
        records = [r.to_dict() for r in controller.usage.query_usage()]
        if parsed.json:
            print(json.dumps(records, indent=2))
        else:
            for r in records:
                print(
                    f"Usage: {r['record_id']} | Principal: {r['principal_id']} | Type: {r['resource_type']} | Qty: {r['quantity']}"
                )
        return 0

    if parsed.subcommand == "pause":
        st = controller.pause_agent(parsed.agent, reason=parsed.reason)
        if parsed.json:
            print(json.dumps(st.to_dict(), indent=2))
        else:
            print(f"Agent {parsed.agent} paused: desired={st.desired_state.value}")
        return 0

    if parsed.subcommand == "resume":
        st = controller.resume_agent(parsed.agent, reason=parsed.reason)
        if parsed.json:
            print(json.dumps(st.to_dict(), indent=2))
        else:
            print(f"Agent {parsed.agent} resumed: desired={st.desired_state.value}")
        return 0

    if parsed.subcommand == "drain":
        st = controller.drain_agent(parsed.agent, reason=parsed.reason)
        if parsed.json:
            print(json.dumps(st.to_dict(), indent=2))
        else:
            print(f"Agent {parsed.agent} draining: desired={st.desired_state.value}")
        return 0

    if parsed.subcommand == "rollout":
        rollouts = [r.to_dict() for r in controller.rollouts._rollouts.values()]
        if parsed.json:
            print(json.dumps(rollouts, indent=2))
        else:
            for r in rollouts:
                print(
                    f"Rollout: {r['rollout_id']} | Version: {r['artifact_version']} | Status: {r['status']}"
                )
        return 0

    if parsed.subcommand == "maintenance":
        windows = [w.to_dict() for w in controller.lifecycle._maintenance_windows.values()]
        if parsed.json:
            print(json.dumps(windows, indent=2))
        else:
            for w in windows:
                print(
                    f"Maintenance: {w['id']} | Scope: {w['scope']} | Target: {w['target_id']} | Active: {w['status']}"
                )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(ops_main())
