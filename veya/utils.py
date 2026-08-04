"""
Utility functions and classes for veya project.
"""


class CostTracker:
    """
    Simple cost tracker for tracking expenses during operations.
    """

    def __init__(self):
        self.total_cost = 0.0
        self.operations = []

    def add_cost(self, amount: float, operation: str = "unknown"):
        """
        Add cost for an operation.
        :param amount: Cost in USD
        :param operation: Description of the operation
        """
        self.total_cost += amount
        self.operations.append(
            {"operation": operation, "amount": amount, "cumulative": self.total_cost}
        )

    def get_total_cost(self) -> float:
        """Get total accumulated cost."""
        return self.total_cost

    def reset(self):
        """Reset the cost tracker."""
        self.total_cost = 0.0
        self.operations = []

    def get_operations(self):
        """Get list of operations with costs."""
        return self.operations

    def record(self, *, tokens: int = 0, cost_usd: float = 0.0) -> None:
        """compat 兼容方法:按 token/成本记录(§1.4 单源——compat.CostTracker 别名本类)。"""
        self.total_cost += cost_usd
        self.operations.append(
            {
                "operation": "llm",
                "amount": cost_usd,
                "tokens": tokens,
                "cumulative": self.total_cost,
            }
        )

    def to_dict(self) -> dict:
        """compat 兼容方法:序列化视图。"""
        return {
            "total_usd": self.total_cost,
            "total_tokens": sum(op.get("tokens", 0) for op in self.operations),
        }


# Global instance for convenience
default_cost_tracker = CostTracker()


def get_cost_tracker():
    """Get the global cost tracker instance."""
    return default_cost_tracker


def track_cost(amount: float, operation: str = "unknown"):
    """Convenience function to add cost to global tracker."""
    default_cost_tracker.add_cost(amount, operation)
