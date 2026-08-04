import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, log_file: str = "security_audit.log"):
        self.log_file = Path(log_file)
        self.logger = self._setup_logger()

    def _setup_logger(self):
        """Setup file logger for audit trail"""
        logger = logging.getLogger("security_audit")
        logger.setLevel(logging.INFO)

        # Avoid duplicate handlers
        if not logger.handlers:
            handler = logging.FileHandler(self.log_file)
            formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        return logger

    def log_action(
        self,
        user: str,
        action: str,
        resource: str,
        success: bool,
        details: dict[str, Any] | None = None,
    ):
        """Log security-relevant actions"""
        status = "SUCCESS" if success else "FAILED"
        message = f"{user} {action} {resource} [{status}]"

        if details:
            message += f" | Details: {json.dumps(details)}"

        self.logger.info(message)

        # Also write raw JSON log for structured analysis
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "user": user,
            "action": action,
            "resource": resource,
            "success": success,
            "details": details or {},
        }

        with open(self.log_file.with_suffix(".json"), "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    def log_permission_check(self, user: str, permission: str, granted: bool, context: str = ""):
        """Log permission checks"""
        self.log_action(
            user=user,
            action=f"CHECK_PERMISSION_{permission.upper()}",
            resource="SYSTEM",
            success=granted,
            details={"context": context} if context else {},
        )

    def log_file_operation(self, user: str, operation: str, file_path: str, success: bool):
        """Log file system operations"""
        self.log_action(user=user, action=operation.upper(), resource=file_path, success=success)

    def log_api_call(self, user: str, endpoint: str, method: str, success: bool):
        """Log API calls"""
        self.log_action(
            user=user, action=f"API_{method.upper()}", resource=endpoint, success=success
        )


# Global audit logger instance
audit_logger = AuditLogger()

__all__ = ["AuditLogger", "audit_logger"]
