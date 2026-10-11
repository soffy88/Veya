from typing import Dict, List, Any, Optional

def cli_hub_tool_ladder(task_tags: List[str], available_tools: Dict[str, List[str]]) -> Dict[str, Any]:
    ranked = []
    
    api_mcp = available_tools.get("api", []) + available_tools.get("mcp", [])
    if api_mcp:
        ranked.append({"source": "api", "tools": api_mcp, "reason": "API preferred"})
        
    cli = available_tools.get("cli_hub", [])
    if cli:
        ranked.append({"source": "cli_hub", "tools": cli, "reason": "CLI preferred over hands"})
        
    hands = available_tools.get("hands", [])
    if hands:
        ranked.append({"source": "hands", "tools": hands, "reason": "No CLI usable for this task"})
        
    if not ranked:
        ranked.append({"source": "none", "reason": "No tools"})
        
    return {"ranked": ranked}


def cli_hub_verify_output(file_path: str, expected_type: str, content: bytes, max_size_mb: Optional[float] = None) -> Dict[str, Any]:
    if not content:
        return {"verified": False, "reason": "Empty content"}
    
    if max_size_mb and len(content) > max_size_mb * 1024 * 1024:
        return {"verified": False, "reason": "Size exceeds bounds"}

    magic_bytes = {
        "pdf": b"%PDF",
        "png": b"\x89PNG\r\n\x1a\n",
        "zip": b"PK\x03\x04",
        "docx": b"PK\x03\x04",
        "xlsx": b"PK\x03\x04",
        "pptx": b"PK\x03\x04",
    }
    
    mime_types = {
        "pdf": "application/pdf",
        "png": "image/png"
    }

    if expected_type in magic_bytes:
        if not content.startswith(magic_bytes[expected_type]):
            return {"verified": False, "reason": "Invalid magic bytes"}
    elif expected_type == "mp4":
        if b"ftyp" not in content[:12]:
            return {"verified": False, "reason": "Invalid magic bytes"}
    elif expected_type == "wav":
        if not (content.startswith(b"RIFF") and b"WAVE" in content[8:12]):
            return {"verified": False, "reason": "Invalid magic bytes"}

    return {"verified": True, "mime_type": mime_types.get(expected_type)}

