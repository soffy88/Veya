"""server.skill_catalog — 大规模 SKILL.md 目录的索引/搜索/晋降管理。

veya 自己的 skill_hub 只认 manifest.json+run.py 格式的可执行技能, 对开放的
SKILL.md 格式 (K-Dense/SkillsGate/agentskills.io 通用标准) 完全无感知——而机器
上恰好有 2000+ 这种知识型 skill (~/.agents/skills 热路径 + paper-skills-archive
冷存储), 此前管理全靠 README 里几行手写 `mv` shell 命令, 零索引/零搜索, 规模
还在涨, 明显撑不住。

参照 SkillsGate 的核心机制内化 (不是它的 Electron/跨 agent 分发壳, 那和 veya
架构不同构): **本地 SQLite 索引 + 全文搜索 + 冷热晋降**, 支持增量刷新 (按 mtime
只重解析变动过的文件, 不必每次全量重扫)。

刻意不接入 skill_hub 的元路由热路径 (那是另一层决定, 需要先验证索引/搜索本身
有用)。纯粹的目录管理工具: build_index / search / list_skills / promote / demote。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_HOT_DIR = str(Path.home() / ".agents" / "skills")
_DEFAULT_COLD_DIR = str(Path.home() / ".agents" / "paper-skills-archive")
_DEFAULT_DB_PATH = str(Path.home() / ".veya" / "skill_catalog.db")

_FM = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)
# paper-<domain>-<en|zh>-<hash> 命名里唯一稳定可用的分类线索; domain=="paper" 时
# (占存量 93%) 没有实际分类信息, 不当类目 (会议全部挤进一个无意义的桶)。
_DOMAIN_RE = re.compile(r"^paper-([a-z]+)-(?:en|zh)-[0-9a-f]+$")


def hot_dir() -> str:
    return os.environ.get("VEYA_AGENTS_SKILLS_DIR", _DEFAULT_HOT_DIR)


def cold_dir() -> str:
    return os.environ.get("VEYA_AGENTS_ARCHIVE_DIR", _DEFAULT_COLD_DIR)


def db_path() -> str:
    return os.environ.get("VEYA_SKILL_CATALOG_DB", _DEFAULT_DB_PATH)


def _derive_category(name: str) -> str | None:
    m = _DOMAIN_RE.match(name)
    if m and m.group(1) != "paper":
        return m.group(1)
    return None


def _parse_frontmatter(skill_md: str) -> tuple[str | None, str | None]:
    fm = _FM.match(skill_md or "")
    if not fm:
        return None, None
    front = fm.group(1)
    nm = _NAME_RE.search(front)
    dm = _DESC_RE.search(front)
    name = nm.group(1).strip().strip('"').strip("'") if nm else None
    desc = dm.group(1).strip().strip('"').strip("'") if dm else None
    return name, desc


def _connect(path: str | None = None) -> sqlite3.Connection:
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS skills ("
        "  name TEXT PRIMARY KEY,"
        "  location TEXT NOT NULL,"
        "  dir_path TEXT NOT NULL,"
        "  description TEXT,"
        "  category TEXT,"
        "  mech_score REAL,"
        "  mtime REAL NOT NULL"
        ")"
    )
    try:
        # trigram (非默认 unicode61): unicode61 把整段连续 CJK 字符当一个不可分割
        # token, 子串查询 (如 "需求弹性" 搜 "需求弹性理论...") 永远 MISS —— 这批
        # skill 里中文 concept-card 描述占相当比例, 必须能子串命中。trigram 按
        # 3-char n-gram 索引, 中英文子串匹配都成立, 代价是查询串 <3 字符时无 n-gram
        # 可用 (search() 对此有 LIKE 兜底)。
        conn.execute(
            'CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(name UNINDEXED, description, tokenize="trigram")'
        )
    except sqlite3.OperationalError:
        pass  # FTS5/trigram 编译选项缺失 (少见); search() 探测表是否存在, 优雅降级 LIKE
    return conn


def _fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='skills_fts'"
    ).fetchone()
    return row is not None


@dataclass
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: int = 0
    duplicates: int = 0  # 同名跨 root 存在 (如 hot+cold 各一份), 后出现的被跳过


def build_index(
    *,
    roots: list[tuple[str, str]] | None = None,
    db: str | None = None,
    score_mechanical: bool = True,
) -> IndexStats:
    """扫描 roots (默认 [(hot_dir,'hot'), (cold_dir,'cold')]) 增量建索引。

    按 mtime 判定是否需要重解析 (未变动的行跳过, 不必每次全量重读 2000+ 文件)。
    磁盘上已消失的 skill 会从索引里一并清除 (防止目录增删后索引腐化)。
    """
    if roots is None:
        roots = [(hot_dir(), "hot"), (cold_dir(), "cold")]
    conn = _connect(db)
    fts = _fts_available(conn)
    stats = IndexStats()
    seen: set[str] = set()
    # 同名跨 root 出现时 (磁盘上确有 hot+cold 各一份的重复), 若不设防, 后扫到的
    # root 会靠 ON CONFLICT DO UPDATE 静默覆盖前者的 location —— 索引跟着 root
    # 遍历顺序说谎, 而非反映"这个技能实际在哪"。roots 顺序即优先级 (调用方传
    # [(hot,...), (cold,...)] 时 hot 优先), 后出现的同名一律跳过不覆盖。
    claimed: set[str] = set()

    for root, location in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        for d in sorted(rp.iterdir()):
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            stats.scanned += 1
            name = d.name
            seen.add(name)
            if name in claimed:
                stats.duplicates += 1
                continue
            claimed.add(name)
            try:
                mtime = f.stat().st_mtime
                row = conn.execute("SELECT mtime FROM skills WHERE name = ?", (name,)).fetchone()
                if row is not None and row[0] == mtime:
                    stats.unchanged += 1
                    continue
                skill_md = f.read_text(encoding="utf-8", errors="ignore")
                fm_name, desc = _parse_frontmatter(skill_md)
                mech = None
                if score_mechanical:
                    from server.paper_skill_scorer import mechanical_score

                    mech, _ = mechanical_score(skill_md, skill_dir=d)
                category = _derive_category(name)
                conn.execute(
                    "INSERT INTO skills (name, location, dir_path, description, category,"
                    " mech_score, mtime) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(name) DO UPDATE SET location=excluded.location,"
                    " dir_path=excluded.dir_path, description=excluded.description,"
                    " category=excluded.category, mech_score=excluded.mech_score,"
                    " mtime=excluded.mtime",
                    (name, location, str(d), desc or fm_name, category, mech, mtime),
                )
                if fts:
                    conn.execute("DELETE FROM skills_fts WHERE name = ?", (name,))
                    conn.execute(
                        "INSERT INTO skills_fts (name, description) VALUES (?, ?)",
                        (name, f"{fm_name or name} {desc or ''}"),
                    )
                if row is None:
                    stats.indexed += 1
                else:
                    stats.updated += 1
            except Exception:  # noqa: BLE001 — 单个 skill 解析失败不拖垮整批索引
                stats.errors += 1

    # 磁盘上已删除的 skill 从索引里一并清理。
    stale = [r[0] for r in conn.execute("SELECT name FROM skills").fetchall() if r[0] not in seen]
    for name in stale:
        conn.execute("DELETE FROM skills WHERE name = ?", (name,))
        if fts:
            conn.execute("DELETE FROM skills_fts WHERE name = ?", (name,))
    stats.removed = len(stale)

    conn.commit()
    conn.close()
    return stats


def _fts_query(q: str) -> str:
    """把自由文本 query 转成 trigram MATCH 的合法短语字面量 (转义内嵌双引号)。

    trigram 按原始字符 3-gram 索引/查询, 不按词切分 (与 unicode61 相反) —— 直接
    把整个 query 当一个短语传, 不必 (也不该) 先按 \\w+ 拆词, 否则 CJK 连续文本
    会被拆成一整个大 token 反而查不到 (unicode61 的老毛病)。
    """
    return '"' + q.replace('"', '""') + '"'


def search(query: str, *, db: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """全文搜索 name+description。FTS5 不可用/query<3 字符 (trigram 无 n-gram 可用)
    时优雅降级为 LIKE 子串匹配。
    """
    conn = _connect(db)
    fts = _fts_available(conn)
    if fts and len(query.strip()) >= 3:
        try:
            rows = conn.execute(
                "SELECT s.name, s.location, s.dir_path, s.description, s.category, s.mech_score"
                " FROM skills_fts f JOIN skills s ON s.name = f.name"
                " WHERE skills_fts MATCH ? ORDER BY rank LIMIT ?",
                (_fts_query(query), limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    else:
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT name, location, dir_path, description, category, mech_score FROM skills"
            " WHERE name LIKE ? OR description LIKE ? LIMIT ?",
            (like, like, limit),
        ).fetchall()
    conn.close()
    return [
        {
            "name": r[0],
            "location": r[1],
            "dir_path": r[2],
            "description": r[3],
            "category": r[4],
            "mech_score": r[5],
        }
        for r in rows
    ]


def list_skills(
    *, location: str | None = None, category: str | None = None, db: str | None = None
) -> list[dict[str, Any]]:
    conn = _connect(db)
    clauses, params = [], []
    if location:
        clauses.append("location = ?")
        params.append(location)
    if category:
        clauses.append("category = ?")
        params.append(category)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT name, location, dir_path, description, category, mech_score FROM skills{where}"
        " ORDER BY name",
        params,
    ).fetchall()
    conn.close()
    return [
        {
            "name": r[0],
            "location": r[1],
            "dir_path": r[2],
            "description": r[3],
            "category": r[4],
            "mech_score": r[5],
        }
        for r in rows
    ]


def _move(name: str, *, to_location: str, target_root: str, db: str | None) -> bool:
    conn = _connect(db)
    row = conn.execute("SELECT location, dir_path FROM skills WHERE name = ?", (name,)).fetchone()
    if row is None:
        conn.close()
        return False
    location, dir_path = row
    if location == to_location:
        conn.close()
        return True  # 已经在目标位置, 幂等
    src = Path(dir_path)
    dst = Path(target_root) / name
    if not src.is_dir():
        conn.close()
        return False
    Path(target_root).mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    conn.execute(
        "UPDATE skills SET location = ?, dir_path = ? WHERE name = ?",
        (to_location, str(dst), name),
    )
    conn.commit()
    conn.close()
    return True


def promote(name: str, *, db: str | None = None) -> bool:
    """冷存储 → 热路径 (等价于 README 手写的 mv 命令, 但会同步更新索引)。"""
    return _move(name, to_location="hot", target_root=hot_dir(), db=db)


def demote(name: str, *, db: str | None = None) -> bool:
    """热路径 → 冷存储 (反向, 释放常驻 token)。"""
    return _move(name, to_location="cold", target_root=cold_dir(), db=db)


def _cli() -> None:
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(prog="skill_catalog", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="增量建/刷新索引")

    sp = sub.add_parser("search", help="全文搜索")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)

    lp = sub.add_parser("list", help="按位置/分类列出")
    lp.add_argument("--location", choices=["hot", "cold"])
    lp.add_argument("--category")

    pp = sub.add_parser("promote", help="冷存储 → 热路径")
    pp.add_argument("name")
    dp = sub.add_parser("demote", help="热路径 → 冷存储")
    dp.add_argument("name")

    args = ap.parse_args()
    if args.cmd == "build":
        stats = build_index()
        print(_json.dumps(stats.__dict__, ensure_ascii=False, indent=2))
    elif args.cmd == "search":
        for r in search(args.query, limit=args.limit):
            print(f"[{r['location']}] {r['name']}  ({r['mech_score']})  {r['description']}")
    elif args.cmd == "list":
        for r in list_skills(location=args.location, category=args.category):
            print(f"[{r['location']}] {r['name']}")
    elif args.cmd == "promote":
        print("ok" if promote(args.name) else "not found / already there")
    elif args.cmd == "demote":
        print("ok" if demote(args.name) else "not found / already there")


if __name__ == "__main__":
    _cli()
