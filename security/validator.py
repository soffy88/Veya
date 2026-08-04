from pathlib import Path
from urllib.parse import urlparse

import yaml


def load_security_config():
    """Load security config from YAML file"""
    config_path = Path(__file__).parent.parent / "config" / "security.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def validate_input(input_str: str, context: str = "general") -> bool:
    """
    Validate input string based on security rules

    Args:
        input_str: Input to validate
        context: Context for validation ("file_path", "api_call", "code_execution", etc.)

    Returns:
        True if valid, False otherwise
    """
    config = load_security_config()
    blacklist_patterns = config.get("blacklist", {}).get("forbidden_patterns", [])

    # Check for blacklisted patterns
    for pattern in blacklist_patterns:
        if pattern.lower() in input_str.lower():
            return False

    # Context-specific validation
    if context == "file_path":
        return _validate_file_path(input_str, config)
    elif context == "api_call":
        return _validate_api_call(input_str, config)
    elif context == "network_host":
        return _validate_network_host(input_str, config)

    return True


def _validate_file_path(file_path: str, config: dict) -> bool:
    """Validate file path against whitelist and traversal attempts"""
    try:
        path_obj = Path(file_path).resolve()
        allowed_paths = config.get("whitelist", {}).get("allowed_paths", [])

        # Check for path traversal
        if ".." in file_path.split("/"):
            return False

        # Check if path is within allowed directories
        for allowed_path in allowed_paths:
            if str(path_obj).startswith(allowed_path):
                return True

        return False
    except Exception:
        return False


def _validate_api_call(api_endpoint: str, config: dict) -> bool:
    """Validate API endpoint against whitelist"""
    allowed_hosts = config.get("whitelist", {}).get("allowed_hosts", [])

    # Extract host from URL
    parsed = urlparse(api_endpoint)
    host = parsed.hostname or parsed.netloc

    if host in allowed_hosts:
        return True

    # Block if not in whitelist
    return False


def _validate_network_host(host: str, config: dict) -> bool:
    """Validate network host against whitelist"""
    allowed_hosts = config.get("whitelist", {}).get("allowed_hosts", [])
    return host in allowed_hosts


def sanitize_input(input_str: str) -> str:
    """Basic input sanitization"""
    # Remove dangerous characters/sequences
    dangerous = ["'", '"', ";", "|", "&", "$", "`", "\\", "(", ")"]
    sanitized = input_str
    for char in dangerous:
        sanitized = sanitized.replace(char, "")
    return sanitized.strip()
