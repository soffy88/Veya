"""Strict gate for Phase 2: Causal inference, Bayesian ToM, Honeypot."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# Make the 3O package tree importable (single-layer: platform/3O/<lib>)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veya.platform import load

load("obase")
load("omodul")
load("oprim")
load("oskill")

from obase.causal_graph_store import CausalGraphStore
from omodul.adversarial_honeypot_observe import (
    adversarial_honeypot_observe,
)
from omodul.causal_fault_diagnose import causal_fault_diagnose
from oprim._do_calculus_intervention import _do_calculus_intervention
from oskill._bayesian_belief_update import (
    _bayesian_belief_update,
    sequential_update,
)

# ---------------------------------------------------------------------------
# 1. Bayesian belief update – malicious intent must exceed 0.9 after 3 signals
# ---------------------------------------------------------------------------

def test_bayesian_belief_update_malicious_exceeds_0_9():
    """
    Hypotheses order (must match likelihood columns):
      0: cooperative
      1: neutral
      2: frustrated
      3: malicious
    """
    # Uniform prior
    prior = np.array([0.25, 0.25, 0.25, 0.25], dtype=np.float64)

    # Three successive "abnormal" observations.
    # Likelihoods are hand-crafted so that malicious receives high probability
    # under each abnormal evidence.
    # Evidence type A: "rejects proposal three times"  → strong malicious signal
    lik_reject = np.array([0.05, 0.10, 0.25, 0.90])  # P(E|H)
    # Evidence type B: "sends malformed JSON payload"
    lik_malformed = np.array([0.02, 0.08, 0.30, 0.85])
    # Evidence type C: "attempts privilege escalation in sandbox"
    lik_escalation = np.array([0.01, 0.05, 0.15, 0.95])

    # Sequential update
    post1 = _bayesian_belief_update(prior, lik_reject)
    post2 = _bayesian_belief_update(post1, lik_malformed)
    post3 = _bayesian_belief_update(post2, lik_escalation)

    malicious_idx = 3
    assert post3[malicious_idx] > 0.9, (
        f"After 3 abnormal observations, P(malicious) = {post3[malicious_idx]:.4f} "
        f"did not exceed 0.9. Full posterior: {post3}"
    )
    # Sanity: still a valid distribution
    assert np.isclose(post3.sum(), 1.0, atol=1e-9)
    assert np.all(post3 >= 0)


def test_bayesian_batch_update_equivalent():
    """Batch (product) update should match sequential for independent evidence."""
    prior = np.array([0.4, 0.3, 0.2, 0.1])
    liks = np.array([
        [0.1, 0.2, 0.3, 0.8],
        [0.05, 0.15, 0.4, 0.85],
        [0.02, 0.1, 0.25, 0.9],
    ])
    batch_post = _bayesian_belief_update(prior, liks)
    seq_post = sequential_update(prior, [liks[i] for i in range(3)])
    np.testing.assert_allclose(batch_post, seq_post, atol=1e-10)


# ---------------------------------------------------------------------------
# 2. Honeypot – reading DB_PASS_HONEYPOT must trigger 100 % interception
# ---------------------------------------------------------------------------

def test_honeypot_env_read_triggers_interception():
    """
    Any code that calls os.environ.get("DB_PASS_HONEYPOT") (or os.getenv)
    inside the sandbox MUST be flagged as hostile and produce an escalation.
    """
    hostile_code = """
import os
# Legitimate-looking code that secretly probes for secrets
val = os.environ.get("DB_PASS_HONEYPOT")
print("got:", val)
"""
    obs = adversarial_honeypot_observe(hostile_code, timeout=3.0)

    assert obs.is_hostile is True, "Honeypot read was not detected as hostile"
    assert obs.result.detected_honeypot_access is True
    assert obs.escalation_payload is not None
    assert obs.escalation_payload["event"] == "adversarial_agent_detected"
    assert "DB_PASS_HONEYPOT" in obs.injected_keys


def test_honeypot_clean_code_passes():
    """Benign code that never touches honeypot keys must be allowed."""
    clean_code = """
import os
print("hello from sandbox")
print("PATH present:", "PATH" in os.environ)
"""
    obs = adversarial_honeypot_observe(clean_code, timeout=3.0)
    assert obs.is_hostile is False
    assert obs.result.detected_honeypot_access is False
    assert obs.escalation_payload is None
    assert obs.result.success is True


# ---------------------------------------------------------------------------
# 3. Causal graph + do-calculus smoke tests
# ---------------------------------------------------------------------------

def test_causal_graph_store_roundtrip():
    store = CausalGraphStore()
    store.add_node("api_gateway", component_type="service")
    store.add_node("rate_limiter", component_type="middleware")
    store.add_node("db", component_type="storage")
    store.add_node("task_outcome", component_type="sink")
    store.add_edge("api_gateway", "rate_limiter")
    store.add_edge("rate_limiter", "db")
    store.add_edge("db", "task_outcome")
    store.add_edge("api_gateway", "task_outcome")  # direct path

    assert store.topological_sort()  # must not raise
    ser = store.serialize()
    store2 = CausalGraphStore()
    store2.deserialize(ser)
    assert set(store2.nodes()) == set(store.nodes())
    assert set(store2.edges()) == set(store.edges())


def test_do_calculus_intervention_mutilates_incoming():
    g = CausalGraphStore()
    g.add_node("A")
    g.add_node("B")
    g.add_node("C")
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    g.add_edge("X", "B")  # another parent of B

    dag = g.get_graph()
    result = _do_calculus_intervention(
        dag,
        target_node="B",
        intervention_value=0,
        outcome_nodes=["C"],
    )
    assert result["status"] == "ok"
    # All incoming edges to B must have been recorded as cut
    cut = set(tuple(e) for e in result["mutilated_edges"])
    assert ("A", "B") in cut
    assert ("X", "B") in cut
    assert result["remaining_parents"] == []


def test_causal_fault_diagnose_finds_root_cause():
    store = CausalGraphStore()
    # Simple failure chain: external_api → timeout_handler → task_outcome
    for n in ("external_api", "timeout_handler", "db_query", "task_outcome"):
        store.add_node(n)
    store.add_edge("external_api", "timeout_handler")
    store.add_edge("timeout_handler", "task_outcome")
    store.add_edge("db_query", "task_outcome")

    report = causal_fault_diagnose(
        "Task crashed with TimeoutError after 3 retries",
        store=store,
        failure_node="task_outcome",
    )
    assert len(report.root_cause_candidates) >= 1
    assert report.confidence > 0.0
    assert "external_api" in report.root_cause_candidates or \
           "timeout_handler" in report.root_cause_candidates
    assert len(report.recommended_actions) >= 1


# ---------------------------------------------------------------------------
# 4. Full quantitative CPD path (do-calculus + VariableElimination)
# ---------------------------------------------------------------------------

def test_quantitative_do_calculus_reduces_failure_prob():
    """
    Build a small binary failure network, intervene on a high-impact node,
    and verify that P(task_outcome=fault | do(node=ok)) drops substantially.
    """
    from oprim._do_calculus_intervention import build_binary_failure_cpd_map

    store = CausalGraphStore()
    nodes = ["external_api", "rate_limit", "db", "task_outcome"]
    for n in nodes:
        store.add_node(n)
    store.add_edge("external_api", "rate_limit")
    store.add_edge("rate_limit", "task_outcome")
    store.add_edge("db", "task_outcome")

    dag = store.get_graph()
    cpd_map = build_binary_failure_cpd_map(dag, fault_prior=0.25, transmission=0.8)

    # Intervene on the root that feeds the failure path
    result = _do_calculus_intervention(
        dag,
        target_node="external_api",
        intervention_value="ok",
        outcome_nodes=["task_outcome"],
        cpd_map=cpd_map,
    )

    assert result["status"] in ("ok", "partial")
    assert result["inference_backend"] == "pgmpy_variable_elimination"
    assert result["post_intervention_distribution"] is not None

    dist = result["post_intervention_distribution"]["task_outcome"]
    p_fault = dist.get("fault", dist.get("1", 1.0))
    # After forcing external_api = ok, failure probability must be lower than
    # the unconditional prior (which is > 0.2 under these parameters)
    assert p_fault < 0.35, f"Expected reduced P(fault), got {p_fault}"


def test_causal_fault_diagnose_quantitative_ranking():
    """
    End-to-end: auto-build CPDs → diagnose → top root-cause should be the
    node whose intervention yields the largest drop in P(fault).
    """
    store = CausalGraphStore()
    for n in ("external_api", "timeout_handler", "db_query", "task_outcome"):
        store.add_node(n)
    store.add_edge("external_api", "timeout_handler")
    store.add_edge("timeout_handler", "task_outcome")
    store.add_edge("db_query", "task_outcome")

    report = causal_fault_diagnose(
        "Task crashed with TimeoutError after 3 retries",
        store=store,
        failure_node="task_outcome",
        auto_build_cpds=True,
        intervention_value="ok",
    )

    assert report.quantitative is True
    assert len(report.root_cause_candidates) >= 1
    assert report.confidence > 0.5

    # At least one intervention should report a concrete p_fault_after_do
    quant_hits = [i for i in report.interventions if i.p_fault_after_do is not None]
    assert len(quant_hits) >= 1

    # The top candidate should produce a meaningful reduction
    top = report.root_cause_candidates[0]
    top_res = next(i for i in report.interventions if i.node_id == top)
    assert top_res.effect_on_failure in (
        "eliminates_failure", "strongly_reduces", "reduces", "on_causal_path"
    )


if __name__ == "__main__":
    # Allow running without pytest for quick CI smoke
    test_bayesian_belief_update_malicious_exceeds_0_9()
    print("[PASS] Bayesian ToM – malicious > 0.9")
    test_honeypot_env_read_triggers_interception()
    print("[PASS] Honeypot interception 100 %")
    test_honeypot_clean_code_passes()
    print("[PASS] Clean code allowed")
    test_causal_graph_store_roundtrip()
    print("[PASS] CausalGraphStore ser/de")
    test_do_calculus_intervention_mutilates_incoming()
    print("[PASS] do-calculus mutilation")
    test_causal_fault_diagnose_finds_root_cause()
    print("[PASS] Causal fault diagnose (structural)")
    test_quantitative_do_calculus_reduces_failure_prob()
    print("[PASS] Quantitative do-calculus CPD path")
    test_causal_fault_diagnose_quantitative_ranking()
    print("[PASS] Causal fault diagnose (quantitative ranking)")
    print("\n=== All Phase 2 gate tests (incl. full CPD) passed ===")
