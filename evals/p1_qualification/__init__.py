"""P1 qualification harness (preparation only).

This package builds the 30-60min qualification runner that executes AFTER I4
PASS.  It touches production code paths (ContextEngine, ReliableProviderAdapter,
LongRunningHarness, PersistentComputer, SideEffectLedger, VerificationEngine,
checkpoints) but lives entirely in evals/tests directories: no production
runtime file is modified here.

Real-work rule: duration is produced by real CPU/IO/tool/provider/context
cycles only.  There is no sleep-based padding anywhere in this package.
"""

__all__ = []
