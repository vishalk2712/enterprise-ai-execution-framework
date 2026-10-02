"""Indexed local relationships. An ownership edge is never an identity edge."""
from collections import deque
import hashlib
import json

from .normalization import identifier

ATTRIBUTES = {"registration_id": "HAS_REGISTRATION", "tax_id": "HAS_TAX_ID", "lei": "HAS_LEI", "postcode": "SHARES_POSTCODE", "bank_account_hash": "HAS_BANK_HASH"}
RELATIONS = {*ATTRIBUTES.values(), "RESOLVED_TO", "INVOICED_TO", "IN_CATEGORY", "ASSOCIATED_WITH"}


def migrate(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS knowledge_nodes (node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS knowledge_edges (
      source TEXT NOT NULL REFERENCES knowledge_nodes(node_id), relation TEXT NOT NULL, target TEXT NOT NULL REFERENCES knowledge_nodes(node_id),
      PRIMARY KEY(source,relation,target));
    CREATE INDEX IF NOT EXISTS knowledge_incoming ON knowledge_edges(target,relation,source);
    """)


def attribute_node(field, value, country):
    scoped = value if field == "lei" else country + ":" + value
    return field + ":" + hashlib.sha256(scoped.encode()).hexdigest()


def rebuild(db, records, invoices, entities, memberships):
    nodes, edges = {}, set()
    for entity in entities:
        nodes[entity["entity_id"]] = ("entity", entity["display_name"])
    for row in records:
        rid = "supplier:" + row["supplier_id"]
        nodes[rid] = ("supplier", row["name"])
        edges.add((rid, "RESOLVED_TO", memberships[row["supplier_id"]]))
        for field, relation in ATTRIBUTES.items():
            value = identifier(row.get(field, ""))
            if not value:
                continue
            key = attribute_node(field, value, row["country"])
            nodes[key] = (field, "Bank hash " + value[:8] if field == "bank_account_hash" else value)
            edges.add((rid, relation, key))
        parent = identifier(row.get("parent_lei", ""))
        if parent:
            key = attribute_node("lei", parent, row["country"])
            nodes.setdefault(key, ("lei", parent))
            edges.add((rid, "ASSOCIATED_WITH", key))
    for invoice in invoices:
        rid = "invoice:" + invoice["invoice_id"]
        category = invoice["category"]
        key = "category:" + hashlib.sha256(category.encode()).hexdigest()
        nodes[rid], nodes[key] = ("invoice", invoice["invoice_id"]), ("category", category)
        edges.update(((rid, "INVOICED_TO", "supplier:" + invoice["supplier_id"]), (rid, "IN_CATEGORY", key)))
    db.execute("DELETE FROM knowledge_edges")
    db.execute("DELETE FROM knowledge_nodes")
    db.executemany("INSERT INTO knowledge_nodes VALUES(?,?,?)", [(key, *value) for key, value in sorted(nodes.items())])
    db.executemany("INSERT INTO knowledge_edges VALUES(?,?,?)", sorted(edges))
    return {"nodes": len(nodes), "edges": len(edges), "storage": "indexed_sqlite", "ownership_implies_identity": False}


def neighbors(db, node_id, hops=2, limit=100, max_posting=150, relation=None):
    if not isinstance(node_id, str) or not node_id or len(node_id) > 200:
        raise ValueError("A graph node ID is required")
    if isinstance(hops, bool) or not isinstance(hops, int) or not 1 <= hops <= 2 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("Use one or two hops and a node limit of 1..500")
    if relation is not None and relation not in RELATIONS:
        raise ValueError("Unknown graph relationship")
    if not db.execute("SELECT 1 FROM knowledge_nodes WHERE node_id=?", (node_id,)).fetchone():
        raise ValueError("Graph node not found; analyze a dataset first")
    frontier, visited, selected, skipped = deque([(node_id, 0)]), {node_id}, set(), []
    budget_hit = False
    while frontier:
        current, depth = frontier.popleft()
        if depth == hops:
            continue
        suffix = " AND relation=?" if relation else ""
        parameters = (current, relation, current, relation, max_posting+1) if relation else (current, current, max_posting+1)
        rows = db.execute("SELECT source,relation,target FROM knowledge_edges WHERE source=?" + suffix +
                          " UNION SELECT source,relation,target FROM knowledge_edges WHERE target=?" + suffix +
                          " ORDER BY source,relation,target LIMIT ?", parameters).fetchall()
        if len(rows) > max_posting:
            skipped.append(current)
            continue
        for row in rows:
            edge = tuple(row)
            other = edge[2] if edge[0] == current else edge[0]
            if other not in visited:
                if len(visited) >= limit:
                    budget_hit = True
                    continue
                visited.add(other)
                frontier.append((other, depth+1))
            selected.add(edge)
    nodes = [dict(row) for key in sorted(visited) for row in db.execute("SELECT node_id AS id,kind AS type,label FROM knowledge_nodes WHERE node_id=?", (key,))]
    return {"nodes": nodes, "edges": [{"source": a, "relation": r, "target": b} for a, r, b in sorted(selected)],
            "start_node": node_id, "hops": hops, "truncated": bool(skipped) or budget_hit,
            "oversized_nodes_skipped": skipped, "node_budget_reached": budget_hit,
            "note": "Associations and shared attributes suggest candidates; they do not establish legal identity."}
