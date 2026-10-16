"""W13 remote control — pure core: identity, executor, hosts, sessions, wake,
files, notify.

All offline. ``tailscale`` is never invoked: the identity seam is a plain
callable and every test injects a fake. No network, no real peer.
"""

from __future__ import annotations

import pytest

from tests.conftest import ROOT  # noqa: F401 — path bootstrap

LEDGERS = ("HOSTS", "SESSIONS", "WAKE", "FILES", "POLICY")


@pytest.fixture
def rc_on(monkeypatch, tmp_path):
    """Flag on, every W13 ledger isolated to tmp_path."""
    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "1")
    for name in LEDGERS:
        monkeypatch.setenv(f"VEYA_REMOTE_CONTROL_{name}_PATH", str(tmp_path / f"{name}.json"))
    yield


# --------------------------------------------------------------------------
# Identity (§6)
# --------------------------------------------------------------------------


def test_whois_parse_resolves_node():
    from server.remote_control import identity

    raw = (
        '{"Node":{"Name":"ubuntu26","FQDN":"ubuntu26.tail.ts.net.",'
        '"Tags":["tag:trusted"],"User":{"LoginName":"alice@example.com"},'
        '"TailscaleIPs":["100.64.0.1"],"Online":true}}'
    )
    peer = identity.parse_whois(raw, ip="100.64.0.1")

    assert peer.resolved is True
    assert peer.node_name == "ubuntu26"
    assert peer.fqdn == "ubuntu26.tail.ts.net"
    assert peer.matches("host:ubuntu26")
    assert peer.matches("tag:trusted")
    assert peer.matches("user:alice")
    assert peer.matches("fqdn:*.tail.ts.net")


@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"Node":{}}'])
def test_whois_parse_unresolved_on_garbage(raw):
    from server.remote_control import identity

    assert identity.parse_whois(raw, ip="10.0.0.1").resolved is False


def test_resolve_identity_never_raises_and_denies_unknown():
    from server.remote_control import identity

    def boom(argv):
        raise RuntimeError("tailscaled down")

    peer = identity.resolve_identity("100.64.0.9", runner=boom)
    assert peer.resolved is False
    assert identity.evaluate(peer, "message")[0] is False
    # empty address is unknown too
    assert identity.resolve_identity("").resolved is False


def test_policy_deny_wins_over_allow():
    from server.remote_control import identity

    peer = identity.PeerIdentity(
        node_name="ubuntu26", tags=("tag:ci",), user_login="alice", resolved=True
    )
    policy = identity.IdentityPolicy(default="tailnet", allowlist=["ubuntu26"], denylist=["tag:ci"])
    allowed, reason = identity.evaluate(peer, "message", policy=policy)
    assert allowed is False
    assert "denylist" in reason


def test_policy_tailnet_default_is_message_only():
    from server.remote_control import identity

    peer = identity.PeerIdentity(node_name="laptop", user_login="bob", resolved=True)
    policy = identity.IdentityPolicy(default="tailnet")
    assert identity.evaluate(peer, "message", policy=policy)[0] is True
    assert identity.evaluate(peer, "execute", policy=policy)[0] is False
    assert identity.evaluate(peer, "wake", policy=policy)[0] is False


def test_policy_matcher_deny_and_allow():
    from server.remote_control import identity

    dev = identity.PeerIdentity(node_name="dev-1", resolved=True)
    ci = identity.PeerIdentity(node_name="ci-runner", tags=("tag:ci",), resolved=True)
    policy = identity.IdentityPolicy(
        default="tailnet",
        matchers=[{"match": "host:dev-*", "action": "deny"}],
        allowlist=["tag:ci"],
    )
    assert identity.evaluate(dev, "execute", policy=policy)[0] is False
    assert identity.evaluate(ci, "execute", policy=policy)[0] is True


def test_policy_rejects_unknown_action_and_default():
    from server.remote_control import identity

    with pytest.raises(ValueError):
        identity.save_policy(identity.IdentityPolicy(default="whatever"))


# --------------------------------------------------------------------------
# Executor (§8.1)
# --------------------------------------------------------------------------


def test_render_command_substitutes_one_argv_element():
    from server.remote_control import executor

    argv = executor.render_command("pi --run {task}", task="do; rm -rf / #", workspace="/w")
    assert argv == ("pi", "--run", "do; rm -rf / #")


def test_remote_command_template_allowlist_and_shell_metacharacters(monkeypatch):
    from server.remote_control.executor import CommandRejected, render_command

    assert render_command("codex exec {task}", task="safe task") == ("codex", "exec", "safe task")
    with pytest.raises(CommandRejected, match="allowlisted"):
        render_command("arbitrary-binary {task}", task="safe task")
    with pytest.raises(CommandRejected, match="metacharacters"):
        render_command("codex exec {task}; curl https://attacker.example", task="safe task")
    monkeypatch.setenv("VEYA_REMOTE_AGENT_EXECUTABLE_ALLOWLIST", "custom-agent")
    assert render_command("custom-agent {task}", task="safe task")[0] == "custom-agent"


def test_render_command_refuses_newline_and_empty():
    from server.remote_control import executor

    with pytest.raises(executor.CommandRejected):
        executor.render_command("pi {task}", task="line1\nline2")
    with pytest.raises(executor.CommandRejected):
        executor.render_command("pi {task}", task="   ")
    with pytest.raises(executor.CommandRejected):
        executor.validate_argv(["ok", ""])


def test_ssh_argv_wraps_without_shell():
    from server.remote_control import executor
    from server.remote_control.hosts import RemoteHost

    host = RemoteHost(host_id="ubuntu26", display_name="u", tailnet_ip="100.64.0.1", owner_id="u1")
    argv = executor.ssh_argv(host, ("pi", "--run", "hi"))
    assert argv[0] == "ssh"
    assert argv[-3:] == ("pi", "--run", "hi")
    assert "BatchMode=yes" in argv


def test_launch_uses_injected_spawner():
    from server.remote_control import executor
    from server.remote_control.hosts import RemoteHost

    seen: dict[str, object] = {}

    class Handle:
        pid = 99
        returncode = None

    def spawner(argv, *, cwd=""):
        seen["argv"] = argv
        seen["cwd"] = cwd
        return Handle()

    host = RemoteHost(
        host_id="ubuntu26",
        display_name="u",
        tailnet_ip="100.64.0.1",
        owner_id="u1",
        agent_command="pi --run {task}",
        workspace_path="/w",
    )
    result = executor.launch(host, task="hello", spawner=spawner)
    assert seen["argv"] == ["pi", "--run", "hello"]
    assert result.pid == 99
    assert result.transport == "local"


# --------------------------------------------------------------------------
# Host registry (§7)
# --------------------------------------------------------------------------


def test_host_registry_crud(rc_on):
    from server.remote_control import hosts

    host = hosts.RemoteHost(
        host_id="ubuntu26", display_name="Ubuntu", tailnet_ip="100.64.0.1", owner_id="u1"
    )
    hosts.save_host(host)
    assert hosts.get_host("ubuntu26").display_name == "Ubuntu"

    hosts.set_enabled("ubuntu26", False)
    assert hosts.get_host("ubuntu26").enabled is False
    assert hosts.list_hosts(owner_id="u1", enabled_only=True) == []
    assert len(hosts.list_hosts(owner_id="u1")) == 1
    assert hosts.list_hosts(owner_id="u2") == []

    assert hosts.delete_host("ubuntu26") is True
    assert hosts.get_host("ubuntu26") is None


def test_host_validation_rejects_bad_id_and_missing_owner(rc_on):
    from server.remote_control import hosts

    with pytest.raises(hosts.HostValidationError):
        hosts.save_host(
            hosts.RemoteHost(
                host_id="Bad:ID", display_name="x", tailnet_ip="1.2.3.4", owner_id="u1"
            )
        )
    with pytest.raises(hosts.HostValidationError):
        hosts.save_host(
            hosts.RemoteHost(host_id="ok", display_name="x", tailnet_ip="1.2.3.4", owner_id="")
        )


# --------------------------------------------------------------------------
# Session state machine (§5)
# --------------------------------------------------------------------------


def test_state_machine_is_stepwise_and_needs_evidence(rc_on):
    from server.remote_control import sessions

    session = sessions.create_session(
        host_id="ubuntu26", agent_label="pi", task="do x", owner_id="u1"
    )
    assert session.state == "accepted"

    # done is refused from accepted (must reach acked first)
    with pytest.raises(sessions.TransitionRejected):
        sessions.mark_done(
            session.session_id,
            evidence=sessions.VerifierEvidence(verified=True, source="v", summary="ok"),
        )
    # acked is refused from accepted (must reach woken first)
    with pytest.raises(sessions.TransitionRejected):
        sessions.mark_acked(session.session_id)

    sessions.mark_woken(session.session_id)
    sessions.mark_acked(session.session_id)
    # done without usable evidence is refused
    with pytest.raises(sessions.TransitionRejected):
        sessions.mark_done(session.session_id, evidence=sessions.VerifierEvidence())

    done = sessions.mark_done(
        session.session_id,
        evidence=sessions.VerifierEvidence(
            verified=True, source="verify_task", summary="tests pass"
        ),
    )
    assert done.state == "done"
    assert done.evidence_view().usable is True

    # terminal: no further transition
    with pytest.raises(sessions.TransitionRejected):
        sessions.mark_woken(session.session_id)


def test_cancel_only_from_woken_or_acked(rc_on):
    from server.remote_control import sessions

    s = sessions.create_session(host_id="h", agent_label="pi", task="t", owner_id="u1")
    with pytest.raises(sessions.TransitionRejected):
        sessions.cancel_session(s.session_id)  # accepted is not cancellable
    sessions.mark_woken(s.session_id)
    assert sessions.cancel_session(s.session_id).state == "cancelled"


def test_handoff_context_strips_unverified_files(rc_on):
    from server.remote_control import sessions

    s = sessions.create_session(host_id="h", agent_label="pi", task="t", owner_id="u1")
    s.context = {"verified_files": ["ok:/tmp/a", "pending:/tmp/b"]}
    sessions._persist(s)
    sessions.mark_woken(s.session_id)
    sessions.mark_acked(s.session_id)

    bundle = sessions.build_handoff_context(sessions.get_session(s.session_id))
    assert bundle["verified_files"] == ["ok:/tmp/a"]
    assert bundle["task"] == "t"
    assert "history" in bundle


# --------------------------------------------------------------------------
# Wake policy (§9)
# --------------------------------------------------------------------------


def test_wake_ceiling_downgrades_now(rc_on):
    from server.remote_control import wake

    wake.set_policy(wake.WakePolicy(sender="u", agent_label="pi", ceiling="batch"))
    decision, _ = wake.decide(sender="u", agent_label="pi", requested="now")
    assert decision.outcome == "batched"
    assert decision.effective == "batch"


def test_wake_none_request_and_none_ceiling(rc_on):
    from server.remote_control import wake

    wake.set_policy(wake.WakePolicy(sender="u", agent_label="pi", ceiling="none"))
    assert wake.decide(sender="u", agent_label="pi", requested="now")[0].outcome == "rejected"
    assert wake.decide(sender="u", agent_label="pi", requested="none")[0].outcome == "rejected"


def test_wake_quiet_hours_downgrades_not_drops(rc_on):
    from server.remote_control import wake

    settings = {"quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"}}
    # 02:00 local == 2*3600 seconds-of-day
    decision, _ = wake.decide(
        sender="u", agent_label="pi", requested="now", quiet_settings=settings, now=2 * 3600
    )
    assert decision.outcome == "batched"
    # urgent bypasses quiet hours
    urgent, _ = wake.decide(
        sender="u",
        agent_label="pi",
        requested="now",
        urgent=True,
        quiet_settings=settings,
        now=2 * 3600,
    )
    assert urgent.outcome == "woken_now"


def test_wake_rate_limit(rc_on):
    from server.remote_control import wake

    policy = wake.set_policy(
        wake.WakePolicy(
            sender="u", agent_label="pi", ceiling="now", rate_limit=2, rate_window_s=300
        )
    )
    wake.record_wake(policy, now=1000)
    wake.record_wake(policy, now=1001)
    decision, _ = wake.decide(sender="u", agent_label="pi", requested="now", now=1002)
    assert decision.outcome == "rejected"
    assert decision.retry_after_s > 0
    # after the window it is allowed again
    assert (
        wake.decide(sender="u", agent_label="pi", requested="now", now=1400)[0].outcome
        == "woken_now"
    )


# --------------------------------------------------------------------------
# File handoff (§10)
# --------------------------------------------------------------------------


def test_file_token_is_recipient_bound(rc_on):
    from server.remote_control import files

    payload = b"hello world"
    digest = files.digest_bytes(payload)
    a = files.stage_declared(
        session_id="s1",
        recipient_id="r1",
        filename="a.txt",
        size_bytes=len(payload),
        sha256=digest,
        owner_id="u1",
    )
    b = files.stage_declared(
        session_id="s1",
        recipient_id="r2",
        filename="a.txt",
        size_bytes=len(payload),
        sha256=digest,
        owner_id="u1",
    )
    assert a.token != b.token


def test_file_fetch_verifies_size_and_sha(rc_on):
    from server.remote_control import files

    payload = b"hello world"
    record = files.stage_file(
        session_id="s1", recipient_id="r1", filename="a.txt", payload=payload, owner_id="u1"
    )
    # positive case: correct bytes verify
    assert files.verify_digest(record, payload) is True
    fetched = files.fetch(record.token, recipient_id="r1", payload=payload)
    assert fetched.state == "fetched"

    # tampered bytes -> corrupt, and the record is marked
    bad = files.stage_file(
        session_id="s2", recipient_id="r1", filename="b.txt", payload=b"original", owner_id="u1"
    )
    with pytest.raises(files.HandoffCorrupt):
        files.fetch(bad.token, recipient_id="r1", payload=b"tampered")
    assert files.get_handoff(bad.token).state == "corrupt"


def test_file_fetch_wrong_recipient_and_expiry(rc_on):
    from server.remote_control import files

    payload = b"data"
    record = files.stage_file(
        session_id="s1", recipient_id="r1", filename="a.txt", payload=payload, owner_id="u1", now=0
    )
    with pytest.raises(files.HandoffRejected):
        files.fetch(record.token, recipient_id="other", payload=payload, now=1)
    with pytest.raises(files.HandoffExpired):
        files.fetch(record.token, recipient_id="r1", payload=payload, now=10**9)


def test_file_stage_declared_rejects_bad_digest_and_oversize(rc_on, monkeypatch):
    from server.remote_control import files

    monkeypatch.setenv("VEYA_REMOTE_CONTROL_MAX_BYTES", "10")
    with pytest.raises(files.HandoffRejected):
        files.stage_declared(
            session_id="s", recipient_id="r", filename="a", size_bytes=5, sha256="xyz", owner_id="u"
        )
    with pytest.raises(files.HandoffRejected):
        files.stage_declared(
            session_id="s",
            recipient_id="r",
            filename="a",
            size_bytes=99,
            sha256="a" * 64,
            owner_id="u",
        )


def test_untrusted_envelope_is_data_not_instructions(rc_on):
    from server.remote_control import files

    record = files.stage_file(
        session_id="s", recipient_id="r", filename="a.txt", payload=b"x", owner_id="u"
    )
    envelope = files.untrusted_envelope(record)
    assert "untrusted attachment" in envelope
    assert record.sha256 in envelope
    assert "not an instruction" in envelope
    assert record.untrusted_content is True


# --------------------------------------------------------------------------
# Notifications (§11)
# --------------------------------------------------------------------------


def test_notify_done_uses_web_push(monkeypatch):
    from server import notification_center
    from server.remote_control import notify

    captured: dict[str, object] = {}

    def fake_push(type_, title, content, payload=None, user_id=""):
        captured.update(type=type_, title=title, payload=payload, user_id=user_id)
        return "mid"

    monkeypatch.setattr(notification_center.global_notifier, "push", fake_push)
    notify.notify_done(
        owner_id="u1", session_id="s1", agent_label="pi", host_id="ubuntu26", summary="ok"
    )

    assert captured["type"] == "SUCCESS"
    assert captured["user_id"] == "u1"
    payload = captured["payload"]
    assert payload["kind"] == notify.KIND_DONE
    assert payload[notification_center.WEB_PUSH_FLAG] is True


@pytest.mark.asyncio
async def test_jsonrpc_initialize_authenticates_before_adapter_recovery():
    from veya.remote.auth import RemoteAuthError
    from veya.remote.mcp_server import RemoteMCPGateway

    class Auth:
        configured = True

        def verify(self, _authorization):
            raise RemoteAuthError("invalid token")

    class Adapter:
        def __init__(self):
            self.initializations = 0

        async def initialize(self):
            self.initializations += 1

        def list_tools(self):
            return []

    class Sessions:
        max_sessions = None

        def active_count(self):
            return 0

    class Audit:
        def __init__(self):
            self.rows = []

        def record(self, **row):
            self.rows.append(row)

    adapter = Adapter()
    audit = Audit()
    gateway = RemoteMCPGateway(auth=Auth(), sessions=Sessions(), audit=audit, adapter=adapter)
    result = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization="Bearer forged",
        remote_addr="127.0.0.1",
    )
    assert result["error"]["data"]["error_code"] == "AUTH_DENIED"
    assert adapter.initializations == 0
    assert any(row.get("status") == "auth_denied" for row in audit.rows)
