"""3O-PURE — group_roles: deterministic permission matrix and reply-mode routing.

Pure predicates over roles, actions, and bot turn selection for NANO-W10
Addendum P. Contains no IO, no store access, and no network calls.
"""

from __future__ import annotations

import re

# Allowed reply modes
REPLY_MODES = ("everyone", "admin-first", "mention-only")

# Defined roles
ROLE_USER = "user"
ROLE_MAIN_BOT = "main_bot"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

# Actions
ACTION_MANAGE_ALL = "manage_all"
ACTION_MANAGE_GROUP = "manage_group"
ACTION_EDIT_NAME = "edit_name"
ACTION_EDIT_NOTICE = "edit_notice"
ACTION_EDIT_MEMBERS = "edit_members"
ACTION_EDIT_REPLY_MODE = "edit_reply_mode"
ACTION_DELETE_GROUP = "delete_group"
ACTION_SPEAK = "speak"
ACTION_MENTION = "mention"

_ADMIN_ACTIONS = {
    ACTION_MANAGE_GROUP,
    ACTION_EDIT_NAME,
    ACTION_EDIT_NOTICE,
    ACTION_EDIT_MEMBERS,
    ACTION_EDIT_REPLY_MODE,
    ACTION_SPEAK,
    ACTION_MENTION,
}

_MEMBER_ACTIONS = {
    ACTION_SPEAK,
    ACTION_MENTION,
}

_MENTION_PATTERN = re.compile(r"@([a-zA-Z0-9_\-\.]+)")


def evaluate_group_permission(
    actor_role: str,
    action: str,
    *,
    is_own_group: bool = True,
) -> bool:
    """Evaluate whether actor_role can perform action.

    Permission matrix:
    - user: everything in their own group (if is_own_group)
    - main_bot (Chef, etc.): manage all groups and bots (global)
    - admin: own group only: name, notice, members, reply mode, speak, mention
    - member: speak and @ (mention)
    """
    role = (actor_role or "").strip().lower()
    act = (action or "").strip().lower()

    if role == ROLE_USER:
        return bool(is_own_group)

    if role in (ROLE_MAIN_BOT, "chef"):
        # Main bot can manage all groups and bots globally
        return True

    if role == ROLE_ADMIN:
        if not is_own_group:
            return False
        return act in _ADMIN_ACTIONS

    if role == ROLE_MEMBER:
        return act in _MEMBER_ACTIONS

    return False


def extract_mentions(text: str, candidate_bots: list[str]) -> list[str]:
    """Extract mentioned bot ids from text matching candidate_bots.

    Returns bot ids in order of mention without duplicates.
    Case-insensitive match against candidate bot ids.
    """
    if not text or "@" not in text:
        return []

    tokens = [m.lower() for m in _MENTION_PATTERN.findall(text)]
    candidate_map = {b.lower(): b for b in candidate_bots}

    mentioned: list[str] = []
    for token in tokens:
        matched = candidate_map.get(token)
        if matched and matched not in mentioned:
            mentioned.append(matched)

    return mentioned


def select_replying_bots(
    reply_mode: str,
    admin_bot_id: str,
    member_bot_ids: list[str],
    text: str,
) -> list[str]:
    """Select which member bots take a turn for a given user message.

    Modes:
    - 'everyone': all members take turns (in membership order).
    - 'admin-first': admin answers by default; other members take turns only if @mentioned.
    - 'mention-only': only @mentioned bots answer; if no @mention, admin answers.
    """
    mode = (reply_mode or "everyone").strip().lower()
    if mode not in REPLY_MODES:
        mode = "everyone"

    members = [str(m) for m in member_bot_ids if m]
    if not members:
        return []

    admin = admin_bot_id if admin_bot_id in members else (members[0] if members else "")

    if mode == "everyone":
        return list(members)

    mentions = extract_mentions(text, members)

    if mode == "admin-first":
        selected: list[str] = []
        if admin:
            selected.append(admin)
        for bot_id in mentions:
            if bot_id not in selected:
                selected.append(bot_id)
        return selected

    if mode == "mention-only":
        if mentions:
            return mentions
        return [admin] if admin else []

    return list(members)


__all__ = [
    "ACTION_DELETE_GROUP",
    "ACTION_EDIT_MEMBERS",
    "ACTION_EDIT_NAME",
    "ACTION_EDIT_NOTICE",
    "ACTION_EDIT_REPLY_MODE",
    "ACTION_MANAGE_ALL",
    "ACTION_MANAGE_GROUP",
    "ACTION_MENTION",
    "ACTION_SPEAK",
    "REPLY_MODES",
    "ROLE_ADMIN",
    "ROLE_MAIN_BOT",
    "ROLE_MEMBER",
    "ROLE_USER",
    "evaluate_group_permission",
    "extract_mentions",
    "select_replying_bots",
]
