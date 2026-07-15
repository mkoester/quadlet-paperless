#!/usr/bin/env python3
"""Audit a Paperless-ngx taxonomy (tags, document types, and/or correspondents) and get
merge/delete suggestions from a local LLM.

Pulls every item with its document_count from the Paperless-ngx REST API, prints summary
stats plus cheap local near-duplicate groups, then (unless --no-llm) asks an Ollama model
to propose merges and removals. No RAG / vector DB needed — a whole taxonomy fits in one
prompt. Stdlib only; no pip install required.

By default it is READ-ONLY (analysis): it only prints suggestions. With --apply it asks the
model for a machine-readable plan, prints the FULL resolved plan first, then confirms each
change individually before executing it via the API — reassigning documents onto a canonical
item, then deleting the emptied duplicates (a member is only deleted after its document_count
is verified to be 0). Pair --apply with --dry-run to preview the whole plan without any writes.

For messy taxonomies where the model's grouping needs correcting, use the editable-plan
workflow: --plan FILE writes the model's plan to a simple text file you edit (fix/split/drop
groups), then --apply-plan FILE executes exactly what you edited (add --yes to skip prompts).

Credentials come from `taxonomy-audit.env` next to this script (copy the .template). Real
environment variables of the same name take precedence over the file.

Usage:
    ./taxonomy-audit.py                              # analyse all three resources (read-only)
    ./taxonomy-audit.py --resource correspondents    # one resource only
    ./taxonomy-audit.py --no-llm                     # local analysis only (no Ollama call)
    ./taxonomy-audit.py --apply --dry-run            # show the merge/delete plan, change nothing
    ./taxonomy-audit.py --apply                      # interactively apply merges/deletes (WRITES)
    ./taxonomy-audit.py --resource document-types --plan   # → document-types_<datetime>.plan
    ./taxonomy-audit.py --apply-plan document-types_<datetime>.plan   # apply it (resource from file)
    ./taxonomy-audit.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

VERBOSE = False


def vlog(msg: str) -> None:
    """Print a debug line to stderr when --verbose is set."""
    if VERBOSE:
        print(f"[debug] {msg}", file=sys.stderr)

# Each auditable resource: API endpoint, singular label, a hint framing the taxonomy for
# the LLM, and whether to add shared-first-word grouping (useful for org names whose legal
# forms vary: "Allianz", "Allianz …-AG", "Allianz …-Aktiengesellschaft").
#
# The hints deliberately mirror the CONVENTIONS from the paperless-ai-next tagging system
# prompt (see quadlet-paperless-ai-next/README.md) — shortest-form correspondents, precise
# broad document types, thematic tags avoiding generic/too-narrow labels — so the cleanup
# targets the same shape the tagger produces going forward and the taxonomy stays stable.
# Keep them aligned if that prompt changes. (We copy only the conventions, not the tagger's
# per-document JSON schema / date-parsing rules, which don't apply to taxonomy analysis.)
RESOURCES = {
    "tags": {
        "endpoint": "/api/tags/",
        "label": "tag",
        "hint": (
            "Tags are thematic keywords about content; a document may carry several (typically "
            "1–4). Canonical tags are German and SINGULAR (Immobilie not Immobilien; "
            "Rundfunkgebühr not -gebühren) — merge plural/spelling/hyphen variants into the "
            "singular base. Flag as DELETE candidates (not merge targets) tags that don't "
            "describe content: sender/company/person names (they belong to correspondents), "
            "pure numbers/IDs (customer/order/booking/insurance numbers), dates/years/semesters, "
            "addresses, OCR fragments, and too-generic labels ('Dokument')."
        ),
        "prefix_grouping": False,
    },
    "document-types": {
        "endpoint": "/api/document_types/",
        "label": "document type",
        "hint": (
            "A document type is a document's single class; each document has exactly one, so "
            "the set should be SMALL. Use a broad German base form from the curated set the "
            "tagger targets: Rechnung, Bescheid, Bescheinigung, Bestätigung, Brief, "
            "Information, Vertrag, Kontoauszug, Versicherungsschein, Gehaltsabrechnung, Befund, "
            "Antrag, Erinnerung, Zeugnis, AGB. Fold into these: English names (Invoice → "
            "Rechnung), '…schreiben' compounds (Informationsschreiben → Information), and "
            "slash-combined names — a name like 'Rechnung/Bescheid' or 'Brief / Information' is "
            "not its own class, map it to ONE base class. But do NOT merge genuinely different "
            "kinds (Rechnung vs Abrechnung, Vertrag vs Versicherungsschein, Antrag vs Formular, "
            "Bestätigung vs Bescheinigung). Delete generic 'Dokument'/'Text'/'Vorlage'."
        ),
        "prefix_grouping": False,
    },
    "correspondents": {
        "endpoint": "/api/correspondents/",
        "label": "correspondent",
        "hint": (
            "A correspondent is the SENDING institution/company — never the recipient or "
            "account holder. A bare private-person name (the archive owner / addressee) is "
            "usually a wrong entry: flag it as a DELETE candidate. Merge variants of one entity "
            "into the shortest common form: drop legal-form/suffix noise (AG, GmbH, GmbH & Co. "
            "KG, S.A., SE, a.G., Holding, 'Niederlassung …', 'Fachbereich …', postal codes, "
            "'-ServiceTeam') and use the common short name/abbreviation ('Amazon', not 'Amazon "
            "EU SARL'; DKB, BBVA, SCHUFA). Keep genuinely distinct subsidiaries or divisions "
            "apart (Allianz Leben vs. Allianz Sach). Generic-category entries ('Krankenkasse', "
            "'Immobilien', 'Nicht angegeben') are not correspondents — flag them as deletes."
        ),
        "prefix_grouping": True,
    },
}

ENV_KEYS = (
    "PAPERLESS_URL",
    "PAPERLESS_TOKEN",
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
    "OLLAMA_API_KEY",
    "OLLAMA_NUM_CTX",
    "OLLAMA_TEMPERATURE",
    "SINGLETON_THRESHOLD",
)
ENV_FILE = Path(__file__).resolve().parent / "taxonomy-audit.env"


# --- pure helpers -------------------------------------------------------------------


def normalize_base_url(url: str) -> str:
    """Trim a Paperless base URL to its origin/prefix, tolerating a trailing '/api'.

    The old paperless-ai config format included the '/api/' suffix; the tool appends
    '/api/...' itself, so we strip a trailing '/api' (and slashes) to avoid '/api/api/...'.
    """
    u = url.strip().rstrip("/")
    if u.endswith("/api"):
        u = u[: -len("/api")].rstrip("/")
    return u


def parse_env_text(text: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file (ignoring blanks, comments, and `export `)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def normalize_name(name: str) -> str:
    """Fold a name for cheap near-duplicate detection.

    Lowercases, unifies German umlauts/eszett, strips non-alphanumerics, and drops a
    trailing plural/inflection marker so e.g. "Rechnung" / "Rechnungen" / "rechnung " collide.
    """
    s = name.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = "".join(ch for ch in s if ch.isalnum())
    for suffix in ("en", "er", "s", "n"):
        if len(s) > len(suffix) + 2 and s.endswith(suffix):
            return s[: -len(suffix)]
    return s


def group_near_duplicates(items: list[dict]) -> list[list[dict]]:
    """Group items whose names normalize to the same key. Only groups of 2+ returned."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        buckets[normalize_name(item["name"])].append(item)
    groups = [g for g in buckets.values() if len(g) > 1]
    groups.sort(key=lambda g: -sum(i["document_count"] for i in g))
    return groups


def first_word_key(name: str) -> str:
    """Normalized first whitespace-delimited word of a name (for org-prefix grouping)."""
    parts = name.strip().split()
    return normalize_name(parts[0]) if parts else ""


def group_by_first_word(items: list[dict], min_key_len: int = 4) -> list[list[dict]]:
    """Group items that share a distinctive normalized first word (e.g. all "Allianz …").

    Advisory only: may group genuinely distinct entities that happen to share a leading
    word ("Deutsche Bank" vs. "Deutsche Bahn"), so it is for review, not auto-merging.
    Groups whose members already fold together exactly are skipped (shown as near-dups).
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        key = first_word_key(item["name"])
        if len(key) >= min_key_len:
            buckets[key].append(item)
    groups = []
    for g in buckets.values():
        if len(g) > 1 and len({normalize_name(i["name"]) for i in g}) > 1:
            groups.append(g)
    groups.sort(key=lambda g: -sum(i["document_count"] for i in g))
    return groups


def categorize(items: list[dict], singleton_threshold: int) -> dict:
    """Split items into low-use (<= threshold) and unused (0) buckets, plus totals."""
    unused = [i for i in items if i["document_count"] == 0]
    low_use = [i for i in items if 0 < i["document_count"] <= singleton_threshold]
    return {
        "total": len(items),
        "total_docs_linked": sum(i["document_count"] for i in items),
        "unused": sorted(unused, key=lambda i: i["name"].lower()),
        "low_use": sorted(low_use, key=lambda i: (i["document_count"], i["name"].lower())),
    }


# Shared language guidance for both prompts (analysis + apply), for all three resources —
# the corpus is German-primary with a genuine English minority.
LANGUAGE_NOTE = (
    "The names are mostly German with a few English ones. Keep canonical names in German and "
    "do not translate merely to rename — but an English item that just duplicates a German "
    "concept should merge into the German name (e.g. 'Invoice' -> 'Rechnung', 'Bank Statement' "
    "-> 'Kontoauszug')."
)


def build_prompt(items: list[dict], label: str, hint: str) -> str:
    """Build the LLM prompt from the full item list (name + document_count)."""
    lines = [f"{i['name']} ({i['document_count']})" for i in sorted(items, key=lambda i: i["name"].lower())]
    return (
        f"You are helping clean up the {label} taxonomy of a Paperless-ngx document archive.\n"
        f"{hint}\n"
        f"There are {len(items)} {label}s. {LANGUAGE_NOTE} Each line below is a "
        f"{label} with its document count in parentheses.\n\n"
        f"Propose how to shrink this set of {label}s. Return concise Markdown with three sections:\n"
        "1. **Merge groups** — sets that mean the same thing or are redundant variants "
        "(singular/plural, spelling, synonyms, sub/superset). For each group give the items "
        "to merge and a single suggested canonical name.\n"
        "2. **Delete candidates** — items too generic, redundant, or too rarely used to keep. "
        "Say briefly why.\n"
        "3. **Keep** — a one-line note on which high-value items to leave alone.\n\n"
        "Prefer merging into the more-used or more-standard name. Do not invent items that are "
        "not in the list. Be decisive and specific.\n\n"
        f"{label.upper()}S (name (count)):\n" + "\n".join(lines)
    )


def build_plan_prompt(items: list[dict], label: str, hint: str) -> str:
    """Prompt asking for a MACHINE-READABLE merge/delete plan (used by --apply)."""
    lines = [f"{i['name']} ({i['document_count']})" for i in sorted(items, key=lambda i: i["name"].lower())]
    return (
        f"You are cleaning up the {label} taxonomy of a Paperless-ngx archive. {hint}\n"
        f"There are {len(items)} {label}s. {LANGUAGE_NOTE} Each line below is a name with its "
        "document count.\n\n"
        "Return ONLY a JSON object — no prose, no Markdown, no code fences — of this shape:\n"
        '{"merges": [{"canonical": "<final name>", "members": ["<existing name>", "<existing name>"]}], '
        '"deletes": ["<existing name>"]}\n'
        "Rules:\n"
        "- Copy every name in \"members\" and \"deletes\" EXACTLY as written in the list below.\n"
        "- \"canonical\" is the best final name for the merged group; it is usually one of the members.\n"
        "- Prefer the shortest clean base form as the canonical: German and singular (Immobilie, "
        "not Immobilien), the base word not a compound (Information, not Informationsschreiben), "
        "and never a slash-combined name ('Rechnung/Bescheid' is not a class).\n"
        "- Only include a merge when 2+ members are genuinely the same thing.\n"
        "- Be conservative: merge only entries that are the SAME kind. Keep related-but-distinct "
        "kinds separate (e.g. Rechnung vs Abrechnung, Vertrag vs Versicherungsschein, Antrag vs "
        "Formular, Bestätigung vs Bescheinigung). When unsure, leave an entry on its own.\n"
        "- Only include a delete for a name that is safe to remove entirely (too generic/redundant/unused).\n"
        "- A name must appear in at most one merge, and not in both merges and deletes.\n\n"
        f"{label.upper()}S (name (count)):\n" + "\n".join(lines)
    )


def parse_llm_plan(text: str) -> dict:
    """Extract and validate a merge/delete plan from the model's JSON reply.

    Tolerant of surrounding prose/code fences: parses the outermost {...} block. Returns
    {"merges": [{"canonical", "members"}], "deletes": [str]} keeping only well-formed
    entries (canonical + >=2 members for a merge).
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    data = json.loads(text[start : end + 1])
    merges = []
    for m in data.get("merges", []) or []:
        if not isinstance(m, dict):
            continue
        canonical = str(m.get("canonical", "")).strip()
        members = [str(x).strip() for x in (m.get("members") or []) if str(x).strip()]
        if canonical and len(members) >= 2:
            merges.append({"canonical": canonical, "members": members})
    deletes = [str(x).strip() for x in (data.get("deletes") or []) if str(x).strip()]
    return {"merges": merges, "deletes": deletes}


def resolve_merge(merge: dict, index: dict[str, dict]) -> dict | None:
    """Map a plan merge's names to real items via `index` (lower(name)->item).

    Returns {"target", "others", "canonical"} or None if there's nothing to merge. Cases:
    - `canonical` names an existing item that is NOT among the members: use that item as the
      target and merge all members into it (the common "fold small tags into a big existing
      one" case; needs >=1 member, and no rename since the target already has that name).
    - Otherwise `canonical` is one of the members or a brand-new name: pick the member matching
      `canonical`, else the member with the most documents, and rename it to `canonical`
      (needs >=2 members). Names not present in `index` are dropped.
    """
    members, seen = [], set()
    for name in merge["members"]:
        item = index.get(name.lower())
        if item and item["id"] not in seen:
            members.append(item)
            seen.add(item["id"])
    canonical = merge["canonical"]
    canonical_item = index.get(canonical.strip().lower())
    if canonical_item and canonical_item["id"] not in seen:
        if not members:
            return None
        # Merge into the existing canonical item; keep its real name so no rename is attempted.
        return {"target": canonical_item, "others": members, "canonical": canonical_item["name"]}
    if len(members) < 2:
        return None
    target = next((m for m in members if m["name"].lower() == canonical.lower()), None)
    if target is None:
        target = max(members, key=lambda m: m["document_count"])
    others = [m for m in members if m["id"] != target["id"]]
    return {"target": target, "others": others, "canonical": canonical}


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (~2.5 chars/token). German compound words tokenize much
    denser than the English ~4/token rule of thumb, so we deliberately over-count to avoid
    filling the context window (which yields an empty done_reason='length' response)."""
    return len(text) * 2 // 5


def chunk_capacity(num_ctx: int) -> int:
    """Max items to put in one plan prompt. Sized by ITEM COUNT, not a token estimate:
    dense German taxonomy names cost ~20 tokens each and estimating that reliably is hopeless,
    so we budget num_ctx/50 items (~20 tokens each ≈ 40% of the window) and leave the rest for
    the model's output. Raising OLLAMA_NUM_CTX proportionally allows bigger chunks."""
    return max(50, num_ctx // 50)


def plan_chunks(items: list[dict], num_ctx: int) -> list[list[dict]]:
    """Split items into context-fitting chunks for plan generation.

    A taxonomy that fits in one chunk is returned as-is. Otherwise a small set of the
    highest-count items becomes shared "anchors" (included in every chunk so tail entries can
    still merge into a big pillar), and the low-count tail — sorted by name so spelling/plural
    variants stay adjacent — is split into batches within the per-chunk item budget.
    """
    cap = chunk_capacity(num_ctx)
    if len(items) <= cap:
        return [items]
    max_anchors = max(20, cap // 4)
    by_count = sorted(items, key=lambda i: (-i["document_count"], i["name"].lower()))
    anchors = by_count[:max_anchors]
    anchor_ids = {i["id"] for i in anchors}
    tail = sorted((i for i in items if i["id"] not in anchor_ids), key=lambda i: i["name"].lower())
    per_chunk = max(1, cap - len(anchors))
    return [anchors + tail[s:s + per_chunk] for s in range(0, len(tail), per_chunk)]


def dedup_merges(merges: list[dict]) -> list[dict]:
    """Drop duplicate merges (same canonical + member set), e.g. anchors repeated per chunk."""
    seen, out = set(), []
    for m in merges:
        key = (m["canonical"].strip().lower(), frozenset(x.strip().lower() for x in m["members"]))
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


# --- I/O ----------------------------------------------------------------------------


def load_config() -> dict[str, str]:
    """Load env file (if present) then overlay real environment variables."""
    cfg: dict[str, str] = {}
    if ENV_FILE.exists():
        cfg.update(parse_env_text(ENV_FILE.read_text(encoding="utf-8")))
    for key in ENV_KEYS:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    if cfg.get("PAPERLESS_URL"):
        cfg["PAPERLESS_URL"] = normalize_base_url(cfg["PAPERLESS_URL"])
    return cfg


def http_json(url: str, *, headers: dict[str, str], data: bytes | None = None, timeout: int = 600) -> dict:
    method = "POST" if data else "GET"
    vlog(f"{method} {url}")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status, raw = resp.status, resp.read()
    vlog(f"  -> HTTP {status}, {len(raw)} bytes")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        snippet = raw[:180].decode("utf-8", "replace").replace("\n", " ")
        raise ValueError(
            f"expected JSON from {url} but got HTTP {status} with a non-JSON body "
            f"(is PAPERLESS_URL the Paperless instance, not a login page?): {snippet!r}"
        )


def same_origin(next_url: str, base_url: str) -> str:
    """Rewrite a paginated `next` URL onto base_url's scheme+host.

    Paperless behind a reverse proxy often emits `next` links with the wrong scheme
    (http:// instead of https://); following those relies on a redirect that can drop the
    Authorization header. Forcing our own scheme/host keeps every page authenticated.
    """
    b, n = urlsplit(base_url), urlsplit(next_url)
    return urlunsplit((b.scheme, b.netloc, n.path, n.query, n.fragment))


def fetch_all(base_url: str, token: str, endpoint: str) -> list[dict]:
    """Follow pagination through an endpoint and return [{id, name, document_count}]."""
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    url = base_url.rstrip("/") + endpoint + "?page_size=250&ordering=name"
    items: list[dict] = []
    while url:
        payload = http_json(url, headers=headers)
        if "results" not in payload:
            raise ValueError(f"unexpected response from {url}: keys={list(payload)[:6]}")
        vlog(f"  count={payload.get('count')} page_results={len(payload['results'])}")
        for i in payload["results"]:
            items.append({"id": i["id"], "name": i["name"], "document_count": i.get("document_count", 0)})
        nxt = payload.get("next")
        url = same_origin(nxt, base_url) if nxt else None
    return items


def extract_ollama_response(payload: dict) -> str:
    """Pull the generated text from an /api/generate reply, surfacing errors/empties.

    Ollama returns HTTP 200 even for failures (e.g. {"error": "..."} or an empty "response"
    with done_reason "load"), so a silent .get("response","") hides the real problem.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected Ollama response: {payload!r}")
    if payload.get("error"):
        raise ValueError(f"Ollama error: {payload['error']}")
    resp = (payload.get("response") or "").strip()
    if not resp:
        raise ValueError(
            f"Ollama returned an empty response (done_reason={payload.get('done_reason')!r}). "
            "The model likely failed to load at this context size or ran out of memory — check "
            "`ollama ps` and the Ollama logs, try a smaller OLLAMA_NUM_CTX, or reload the model."
        )
    return resp


def query_ollama(host: str, model: str, prompt: str, num_ctx: int, api_key: str | None = None,
                 temperature: float = 0.2) -> str:
    url = host.rstrip("/") + "/api/generate"
    headers = {"Content-Type": "application/json"}
    if api_key:  # for an Ollama endpoint behind an auth proxy; native Ollama ignores it
        headers["Authorization"] = f"Bearer {api_key}"
    # Low temperature: taxonomy mapping wants conservative, repeatable output, not creativity.
    options = {"num_ctx": num_ctx, "temperature": temperature}
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "options": options}).encode("utf-8")
    return extract_ollama_response(http_json(url, headers=headers, data=body))


# --- Paperless mutation helpers (only used by --apply) ------------------------------

# Document-list filter and bulk_edit method per resource (verified against paperless-ngx
# src/documents/filters.py and bulk_edit.py).
DOC_FILTER = {
    "tags": "tags__id__all",
    "correspondents": "correspondent__id",
    "document-types": "document_type__id",
}


def _auth(cfg: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Token {cfg['PAPERLESS_TOKEN']}", "Accept": "application/json"}
    if extra:
        headers.update(extra)
    return headers


def http_send(url: str, method: str, headers: dict[str, str], data: bytes | None = None, timeout: int = 600) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def fetch_document_ids(cfg: dict[str, str], resource_key: str, item_id: int) -> list[int]:
    """All document ids linked to one taxonomy item (paginated)."""
    base = cfg["PAPERLESS_URL"].rstrip("/")
    url = f"{base}/api/documents/?{DOC_FILTER[resource_key]}={item_id}&page_size=250"
    ids: list[int] = []
    while url:
        payload = http_json(url, headers=_auth(cfg))
        ids += [d["id"] for d in payload.get("results", [])]
        nxt = payload.get("next")
        url = same_origin(nxt, base) if nxt else None
    return ids


def bulk_edit_documents(cfg: dict[str, str], doc_ids: list[int], method: str, parameters: dict) -> None:
    base = cfg["PAPERLESS_URL"].rstrip("/")
    body = json.dumps({"documents": doc_ids, "method": method, "parameters": parameters}).encode("utf-8")
    http_json(f"{base}/api/documents/bulk_edit/", headers=_auth(cfg, {"Content-Type": "application/json"}), data=body)


def item_document_count(cfg: dict[str, str], endpoint: str, item_id: int) -> int:
    base = cfg["PAPERLESS_URL"].rstrip("/")
    payload = http_json(f"{base}{endpoint}{item_id}/", headers=_auth(cfg))
    return payload.get("document_count", -1)


def delete_item(cfg: dict[str, str], endpoint: str, item_id: int) -> None:
    base = cfg["PAPERLESS_URL"].rstrip("/")
    http_send(f"{base}{endpoint}{item_id}/", "DELETE", _auth(cfg))


def rename_item(cfg: dict[str, str], endpoint: str, item_id: int, name: str) -> None:
    base = cfg["PAPERLESS_URL"].rstrip("/")
    body = json.dumps({"name": name}).encode("utf-8")
    http_send(f"{base}{endpoint}{item_id}/", "PATCH", _auth(cfg, {"Content-Type": "application/json"}), body)


def reassign_params(resource_key: str, target_id: int, member_id: int) -> tuple[str, dict]:
    """bulk_edit method + parameters to move a member's documents onto the target."""
    if resource_key == "tags":
        return "modify_tags", {"add_tags": [target_id], "remove_tags": [member_id]}
    if resource_key == "correspondents":
        return "set_correspondent", {"correspondent": target_id}
    return "set_document_type", {"document_type": target_id}


def execute_merge(cfg: dict[str, str], resource_key: str, resolved: dict, dry_run: bool) -> None:
    """Reassign each member's docs onto the target, delete the emptied member, rename target."""
    endpoint = RESOURCES[resource_key]["endpoint"]
    target, canonical = resolved["target"], resolved["canonical"]
    for member in resolved["others"]:
        method, params = reassign_params(resource_key, target["id"], member["id"])
        if dry_run:
            n = member["document_count"]
            print(f"    [dry-run] reassign ~{n} doc(s) from '{member['name']}' → '{target['name']}', then delete '{member['name']}'")
            continue
        doc_ids = fetch_document_ids(cfg, resource_key, member["id"])
        if doc_ids:
            bulk_edit_documents(cfg, doc_ids, method, params)
        remaining = item_document_count(cfg, endpoint, member["id"])
        if remaining == 0:
            delete_item(cfg, endpoint, member["id"])
            print(f"    merged '{member['name']}' → '{target['name']}' and deleted it")
        else:
            print(f"    WARNING: '{member['name']}' still has {remaining} doc(s) after reassign; NOT deleting")
    if canonical and canonical != target["name"]:
        if dry_run:
            print(f"    [dry-run] rename target '{target['name']}' → '{canonical}'")
        else:
            try:
                rename_item(cfg, endpoint, target["id"], canonical)
                print(f"    renamed '{target['name']}' → '{canonical}'")
            except urllib.error.HTTPError as e:
                print(f"    (kept name '{target['name']}' — rename to '{canonical}' failed: HTTP {e.code})")


def prompt_yn(msg: str) -> bool:
    """Ask a y/N question. 'q' aborts the whole run (raises KeyboardInterrupt)."""
    try:
        ans = input(f"{msg} [y/N/q] ").strip().lower()
    except EOFError:
        return False
    if ans == "q":
        raise KeyboardInterrupt
    return ans in ("y", "yes")


def describe_action(action: dict, label: str) -> list[str]:
    """Human-readable lines for one planned action (merge or delete)."""
    if action["kind"] == "merge":
        target, canonical, others = action["target"], action["canonical"], action["others"]
        rename = f"  → rename to '{canonical}'" if canonical != target["name"] else ""
        lines = [f"Merge into '{canonical}':", f"    keep:         {target['name']} ({target['document_count']}){rename}"]
        lines += [f"    merge+delete: {o['name']} ({o['document_count']})" for o in others]
        return lines
    item = action["item"]
    return [f"Delete '{item['name']}' ({item['document_count']} doc(s) lose this {label}; other metadata untouched)"]


def action_codes(actions: list[dict]) -> list[str]:
    """Per-kind labels (M1, M2, … for merges; D1, D2, … for deletes), aligned to `actions`."""
    codes, m, d = [], 0, 0
    for a in actions:
        if a["kind"] == "merge":
            m += 1
            codes.append(f"M{m}")
        else:
            d += 1
            codes.append(f"D{d}")
    return codes


def build_action_plan(plan: dict, index: dict[str, dict]) -> list[dict]:
    """Resolve the raw LLM plan into concrete, executable actions (skipping unknowns).

    A name already consumed by a merge is not also deleted, so the plan is conflict-free.
    """
    actions: list[dict] = []
    used_ids: set[int] = set()
    for merge in plan["merges"]:
        resolved = resolve_merge(merge, index)
        if not resolved:
            continue
        ids = {resolved["target"]["id"], *(o["id"] for o in resolved["others"])}
        if ids & used_ids:
            continue  # a name here is already in another action — skip to stay conflict-free
        actions.append({"kind": "merge", **resolved})
        used_ids |= ids
    for name in plan["deletes"]:
        item = index.get(name.lower())
        if item and item["id"] not in used_ids:
            actions.append({"kind": "delete", "item": item})
            used_ids.add(item["id"])
    return actions


PLAN_FILE_HEADER = """\
# Editable {label} cleanup plan for Paperless-ngx. Review/edit, then run:
#   {prog} --apply-plan {basename}
# resource: {resource}
#
# Format (indentation optional; parsing is by keyword):
#   MERGE <surviving name>     the indented types below are reassigned onto it, then deleted
#     <type>                   a type merged into the MERGE above
#   DELETE <name>              removed entirely (its documents keep all other metadata)
#
# '#' starts a comment; blank lines are ignored. To drop a type from a group delete its
# line; to skip a whole action delete its block; to regroup, move a line under another
# MERGE. The MERGE name is the survivor: name an EXISTING type to fold others into it, or
# a NEW name to rename the largest member. Names must match existing {label}s
# (case-insensitive); unknown names are reported and skipped. The 'resource:' line above
# tells --apply-plan which taxonomy this is — keep it.
"""


def format_plan_file(resource_key: str, actions: list[dict], basename: str = "<this-file>") -> str:
    """Serialize resolved actions to the editable plan-file text format."""
    res = RESOURCES[resource_key]
    header = PLAN_FILE_HEADER.format(
        label=res["label"], prog="./taxonomy-audit.py", resource=resource_key, basename=basename
    )
    out = [header]
    for action in actions:
        if action["kind"] == "merge":
            target, canonical, others = action["target"], action["canonical"], action["others"]
            keep = "keep" if canonical == target["name"] else f"new name; was {target['name']}"
            out.append(f"MERGE {canonical}    # {keep}, {target['document_count']} docs")
            # Include the survivor as a member only when it's a new name (so re-resolution can
            # pick and rename it); an existing survivor stays implicit (kept, not a member).
            members = ([] if canonical == target["name"] else [target]) + others
            for m in members:
                out.append(f"  {m['name']}    # ({m['document_count']})")
            out.append("")
        else:
            it = action["item"]
            out.append(f"DELETE {it['name']}    # {it['document_count']} docs lose this {res['label']}")
    return "\n".join(out).rstrip() + "\n"


def _strip_comment(line: str) -> str:
    """Remove a '#' comment and surrounding whitespace (names never contain '#')."""
    return (line.split("#", 1)[0]).strip()


def read_plan_resource(text: str) -> str | None:
    """Return the resource key from a plan file's '# resource: <key>' marker, if valid."""
    for raw in text.splitlines():
        s = raw.strip()
        if s.lower().startswith("# resource:"):
            key = s.split(":", 1)[1].strip()
            return key if key in RESOURCES else None
    return None


def parse_plan_file(text: str) -> dict:
    """Parse the editable plan-file format into the same {merges, deletes} shape as the LLM."""
    merges: list[dict] = []
    deletes: list[str] = []
    current: dict | None = None
    for raw in text.splitlines():
        content = _strip_comment(raw)
        if not content:
            continue
        if content.startswith("MERGE ") or content == "MERGE":
            current = {"canonical": content[len("MERGE"):].strip(), "members": []}
            merges.append(current)
        elif content.startswith("DELETE ") or content == "DELETE":
            deletes.append(content[len("DELETE"):].strip())
            current = None
        elif current is not None:
            current["members"].append(content)  # a member of the current MERGE block
        # else: a stray line before any MERGE — ignore
    merges = [m for m in merges if m["canonical"] and m["members"]]
    return {"merges": merges, "deletes": [d for d in deletes if d]}


def warn_unknown_names(plan: dict, index: dict[str, dict], label: str) -> None:
    """Print a stderr warning for any plan name not present in the live taxonomy."""
    names = [m["canonical"] for m in plan["merges"]]
    names += [n for m in plan["merges"] for n in m["members"]]
    names += plan["deletes"]
    unknown = sorted({n for n in names if n.lower() not in index})
    if unknown:
        print(f"WARNING: {len(unknown)} name(s) in the plan match no existing {label} and will "
              f"be skipped: {', '.join(unknown)}", file=sys.stderr)


def fetch_items_or_exit(resource_key: str, cfg: dict[str, str]) -> list[dict]:
    res = RESOURCES[resource_key]
    label, url = res["label"], cfg["PAPERLESS_URL"] + res["endpoint"]
    try:
        items = fetch_all(cfg["PAPERLESS_URL"], cfg["PAPERLESS_TOKEN"], res["endpoint"])
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
        print(f"Cannot fetch {label}s from {url}: {e}", file=sys.stderr)
        sys.exit(1)
    if not items:
        print(empty_hint(label, url), file=sys.stderr)
        sys.exit(1)
    return items


def render_and_execute(resource_key: str, cfg: dict[str, str], items: list[dict], plan: dict,
                       dry_run: bool, assume_yes: bool) -> None:
    """Resolve a {merges, deletes} plan against live items, print it in full, then execute."""
    res = RESOURCES[resource_key]
    label = res["label"]
    index = {i["name"].lower(): i for i in items}
    actions = build_action_plan(plan, index)
    if not actions:
        print("\nNo actionable merges or deletes. Nothing to do.")
        return

    codes = action_codes(actions)
    n_merge = sum(a["kind"] == "merge" for a in actions)
    n_delete = sum(a["kind"] == "delete" for a in actions)
    print(f"\nPlan for {label}s: {n_merge} merge(s), {n_delete} delete(s)\n")
    for code, action in zip(codes, actions):
        lines = describe_action(action, label)
        print(f"  [{code}] {lines[0]}")
        for extra in lines[1:]:
            print(f"      {extra}")
    print()

    if dry_run:
        print("[dry-run] complete — the full plan above; no changes were made to Paperless.")
        return

    if assume_yes:
        print("Applying all actions in the plan...\n")
    else:
        print("Now confirm each action (y = apply, N = skip, q = abort the rest):\n")
    applied = 0
    for code, action in zip(codes, actions):
        if not assume_yes and not prompt_yn(f"  [{code}] {describe_action(action, label)[0]}"):
            print("      skipped")
            continue
        if action["kind"] == "merge":
            if assume_yes:
                print(f"  [{code}] {describe_action(action, label)[0]}")
            execute_merge(cfg, resource_key, action, dry_run=False)
        else:
            delete_item(cfg, res["endpoint"], action["item"]["id"])
            print(f"      deleted '{action['item']['name']}'")
        applied += 1

    print(f"\nApplied {applied} of {len(actions)} planned change(s) to {label}s.")


def model_plan(resource_key: str, cfg: dict[str, str], items: list[dict]) -> dict | None:
    """Ask Ollama for a merge/delete plan for `items` (chunked if it won't fit one context).

    Returns the combined {merges, deletes} plan, or None if no chunk produced a usable plan.
    """
    res = RESOURCES[resource_key]
    num_ctx = int(cfg.get("OLLAMA_NUM_CTX", "16384"))
    temp = float(cfg.get("OLLAMA_TEMPERATURE", "0.2"))
    chunks = plan_chunks(items, num_ctx)
    if len(chunks) > 1:
        print(f"{len(items)} {res['label']}s won't fit one prompt at num_ctx={num_ctx} — "
              f"splitting into {len(chunks)} chunks (high-count anchors repeated as merge targets).")

    merges: list[dict] = []
    deletes: list[str] = []
    ok = 0
    for n, chunk in enumerate(chunks, 1):
        tag = f"chunk {n}/{len(chunks)} ({len(chunk)} items)" if len(chunks) > 1 else "plan"
        prompt = build_plan_prompt(chunk, res["label"], res["hint"])
        print(f"Asking {cfg['OLLAMA_MODEL']} for a merge/delete {tag} "
              f"(num_ctx={num_ctx}, temperature={temp})...")
        try:
            raw = query_ollama(cfg["OLLAMA_HOST"], cfg["OLLAMA_MODEL"], prompt, num_ctx,
                               cfg.get("OLLAMA_API_KEY"), temp)
        except urllib.error.URLError as e:
            print(f"ERROR contacting Ollama at {cfg['OLLAMA_HOST']}: {e}", file=sys.stderr)
            sys.exit(1)
        except ValueError as e:  # empty/error response surfaced by query_ollama
            print(f"  {tag}: no output — {e}", file=sys.stderr)
            continue
        try:
            p = parse_llm_plan(raw)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"  {tag}: could not parse a plan ({e}); skipping this chunk.", file=sys.stderr)
            continue
        merges += p["merges"]
        deletes += p["deletes"]
        ok += 1

    if ok == 0:
        print(f"No usable plan for {res['label']}s from any chunk.", file=sys.stderr)
        return None
    return {"merges": dedup_merges(merges), "deletes": sorted(set(deletes), key=str.lower)}


def write_plan(resource_key: str, cfg: dict[str, str], path: Path) -> bool:
    """Generate a model plan for one resource and write it to an editable file (no changes).

    Returns True if a file was written, False if the model produced no usable plan.
    """
    res = RESOURCES[resource_key]
    items = fetch_items_or_exit(resource_key, cfg)
    print(f"\n=== PLAN: {res['label']}s ({len(items)} total) -> {path} ===")
    plan = model_plan(resource_key, cfg, items)
    if plan is None:
        print(f"  ✗ No plan file written for {res['label']}s — the model returned no usable "
              "plan (see the error above).", file=sys.stderr)
        return False
    index = {i["name"].lower(): i for i in items}
    actions = build_action_plan(plan, index)
    path.write_text(format_plan_file(resource_key, actions, path.name), encoding="utf-8")
    n_merge = sum(a["kind"] == "merge" for a in actions)
    n_delete = sum(a["kind"] == "delete" for a in actions)
    print(f"  ✓ Wrote {n_merge} merge(s) + {n_delete} delete(s) to {path}. Edit it, then:")
    print(f"      ./taxonomy-audit.py --apply-plan {path}")
    return True


def apply_plan_file(resource_key: str, cfg: dict[str, str], path: Path, dry_run: bool, assume_yes: bool) -> None:
    """Apply an edited plan file to one resource."""
    res = RESOURCES[resource_key]
    try:
        plan = parse_plan_file(path.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"Cannot read plan file {path}: {e}", file=sys.stderr)
        sys.exit(1)
    items = fetch_items_or_exit(resource_key, cfg)
    index = {i["name"].lower(): i for i in items}
    tag = "[DRY-RUN] " if dry_run else ""
    print(f"\n{'=' * 78}\n=== {tag}APPLY PLAN: {res['label']}s from {path} ===\n{'=' * 78}")
    warn_unknown_names(plan, index, res["label"])
    render_and_execute(resource_key, cfg, items, plan, dry_run, assume_yes)


def apply_resource(resource_key: str, cfg: dict[str, str], dry_run: bool, assume_yes: bool) -> None:
    """Generate a model plan for one resource and apply it interactively (the --apply path)."""
    res = RESOURCES[resource_key]
    items = fetch_items_or_exit(resource_key, cfg)
    tag = "[DRY-RUN] " if dry_run else ""
    print(f"\n{'=' * 78}\n=== {tag}APPLY: {res['label']}s ({len(items)} total) ===\n{'=' * 78}")
    plan = model_plan(resource_key, cfg, items)
    if plan is None:
        return
    render_and_execute(resource_key, cfg, items, plan, dry_run, assume_yes)


# --- report -------------------------------------------------------------------------


def empty_hint(label: str, url: str) -> str:
    """Message when the API responds 200 but returns zero items — usually not a bad token."""
    return (
        f"No {label}s returned from {url}, though the request succeeded (HTTP 200). "
        "First check that URL is right (note the resolved path above — a base URL already "
        "ending in /api would double it). Otherwise a wrong URL/token fails outright, so 0 "
        "items usually means the API token belongs to a user who can't see them — Paperless "
        "tokens are per-user and only return objects that user owns or may view. Use a "
        "superuser's token. Re-run with --verbose to see the request and the reported count."
    )


def audit_resource(resource_key: str, cfg: dict[str, str], run_llm: bool) -> None:
    res = RESOURCES[resource_key]
    label = res["label"]
    full_url = cfg["PAPERLESS_URL"] + res["endpoint"]
    try:
        items = fetch_all(cfg["PAPERLESS_URL"], cfg["PAPERLESS_TOKEN"], res["endpoint"])
    except urllib.error.HTTPError as e:
        print(f"Paperless API error {e.code} on {full_url}: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, ValueError) as e:
        print(f"Cannot reach/parse Paperless at {full_url}: {e}", file=sys.stderr)
        sys.exit(1)

    if not items:
        print(empty_hint(label, full_url), file=sys.stderr)
        return

    threshold = int(cfg.get("SINGLETON_THRESHOLD", "2"))
    cats = categorize(items, threshold)
    dupes = group_near_duplicates(items)

    print(f"\n{'=' * 78}")
    print(f"=== {label.upper()}S: {cats['total']} total, {cats['total_docs_linked']} {label}-document links ===")
    print(f"{'=' * 78}\n")
    print(f"Unused {label}s (0 docs): {len(cats['unused'])}")
    print(f"Low-use {label}s (1–{threshold} docs): {len(cats['low_use'])}")
    print(f"Near-duplicate groups (local name-folding): {len(dupes)}\n")

    if cats["unused"]:
        print(f"-- Unused {label}s (safe to delete) --")
        print("  " + ", ".join(i["name"] for i in cats["unused"]) + "\n")

    if cats["low_use"]:
        print(f"-- Low-use {label}s (1–{threshold} docs, review) --")
        for i in cats["low_use"]:
            print(f"  {i['document_count']:>3}  {i['name']}")
        print()

    if dupes:
        print("-- Near-duplicate groups (likely merges) --")
        for g in dupes:
            names = ", ".join(f"{i['name']} ({i['document_count']})" for i in sorted(g, key=lambda i: -i["document_count"]))
            print(f"  • {names}")
        print()

    prefix_groups = group_by_first_word(items) if res.get("prefix_grouping") else []
    if prefix_groups:
        print("-- Shared first-word groups (advisory — review; may include distinct entities) --")
        for g in prefix_groups:
            names = ", ".join(f"{i['name']} ({i['document_count']})" for i in sorted(g, key=lambda i: -i["document_count"]))
            print(f"  • {names}")
        print()

    if not run_llm:
        print(f"(--no-llm: skipping model suggestions for {label}s.)")
        return

    prompt = build_prompt(items, label, res["hint"])
    num_ctx = int(cfg.get("OLLAMA_NUM_CTX", "16384"))
    est = estimate_tokens(prompt)
    print(f"-- Asking {cfg['OLLAMA_MODEL']} at {cfg['OLLAMA_HOST']} (num_ctx={num_ctx}, ~{est} prompt tokens) --")
    if est > num_ctx * 0.7:
        print(f"  WARNING: prompt (~{est} tokens) is close to num_ctx ({num_ctx}); raise OLLAMA_NUM_CTX.")
    print()
    try:
        answer = query_ollama(cfg["OLLAMA_HOST"], cfg["OLLAMA_MODEL"], prompt, num_ctx, cfg.get("OLLAMA_API_KEY"))
    except urllib.error.URLError as e:
        print(
            f"  ERROR contacting Ollama at {cfg['OLLAMA_HOST']}: {e}\n"
            "  A timeout means nothing answered at that host:port from where this script runs "
            "(not an auth problem — a bad token would return an HTTP error). Check the GPU box "
            "is up and OLLAMA_HOST is reachable from here (a link-local/private IP only routes "
            "from the server).",
            file=sys.stderr,
        )
        sys.exit(1)
    except ValueError as e:  # empty/error response (e.g. a big taxonomy filling the context)
        print(f"  No suggestions for {label}s — {e}\n  For a large taxonomy use --plan/--apply, "
              "which chunk the list to fit the context.", file=sys.stderr)
        return
    print(answer)


# --- self-test ----------------------------------------------------------------------


def self_test() -> None:
    assert parse_env_text("# c\nA=1\nexport B = \"two\"\n\nC='x y'") == {"A": "1", "B": "two", "C": "x y"}
    assert normalize_name("Rechnung") == normalize_name("Rechnungen")
    assert normalize_name("Gebühr") == normalize_name("Gebuehren")
    sample = [
        {"id": 1, "name": "Rechnung", "document_count": 40},
        {"id": 2, "name": "Rechnungen", "document_count": 3},
        {"id": 3, "name": "Steuer", "document_count": 0},
        {"id": 4, "name": "Notiz", "document_count": 1},
    ]
    cats = categorize(sample, 2)
    assert cats["total"] == 4
    assert [i["name"] for i in cats["unused"]] == ["Steuer"]
    assert [i["name"] for i in cats["low_use"]] == ["Notiz"]  # Rechnungen (3) is above threshold 2
    groups = group_near_duplicates(sample)
    assert len(groups) == 1 and {i["name"] for i in groups[0]} == {"Rechnung", "Rechnungen"}
    prompt = build_prompt(sample, "document type", RESOURCES["document-types"]["hint"])
    assert "Rechnung (40)" in prompt and "DOCUMENT TYPES" in prompt
    # Both prompts, for every resource, must carry the German-corpus language note.
    for res_key in RESOURCES:
        h = RESOURCES[res_key]["hint"]
        assert LANGUAGE_NOTE in build_prompt(sample, "x", h)
        assert LANGUAGE_NOTE in build_plan_prompt(sample, "x", h)
    assert estimate_tokens("a" * 40) == 16  # ~2.5 chars/token (conservative for German)
    # Chunking: a small taxonomy is one chunk; a large one splits, with anchors in every chunk.
    assert plan_chunks(sample, 16384) == [sample]
    big = [{"id": i, "name": "Wohngebäudeversicherung", "document_count": (99 if i < 4 else 1)} for i in range(400)]
    chs = plan_chunks(big, 2048)  # tiny context forces several chunks
    assert len(chs) > 1
    top_ids = {i["id"] for i in sorted(big, key=lambda i: -i["document_count"])[:4]}
    assert all(top_ids <= {i["id"] for i in c} for c in chs)  # anchors present in every chunk
    assert sum(len(c) for c in chs) >= len(big)  # every item covered (anchors repeat)
    assert dedup_merges([{"canonical": "A", "members": ["b", "c"]},
                         {"canonical": "a", "members": ["C", "b"]}]) == [{"canonical": "A", "members": ["b", "c"]}]
    # Shared-first-word grouping catches the Allianz-style case that whole-name folding misses.
    corr = [
        {"id": 1, "name": "Allianz Lebensversicherungs-Aktiengesellschaft", "document_count": 6},
        {"id": 2, "name": "Allianz Lebensversicherungs-AG", "document_count": 18},
        {"id": 3, "name": "Allianz", "document_count": 2},
        {"id": 4, "name": "Deutsche Bank", "document_count": 5},
    ]
    assert group_near_duplicates(corr) == []  # whole-name folding finds nothing here
    pg = group_by_first_word(corr)
    assert len(pg) == 1 and {i["id"] for i in pg[0]} == {1, 2, 3}  # the three Allianz, not the lone Deutsche
    assert set(RESOURCES) == {"tags", "document-types", "correspondents"}

    # --- apply-mode planning (pure) ---
    plan = parse_llm_plan(
        'noise before {"merges": [{"canonical": "Allianz Lebensversicherung", '
        '"members": ["Allianz Lebensversicherungs-AG", "Allianz Lebensversicherungs-Aktiengesellschaft", " Unknown "]}], '
        '"deletes": ["Allianz", ""]} trailing'
    )
    assert plan["deletes"] == ["Allianz"]  # blank dropped
    assert len(plan["merges"]) == 1 and len(plan["merges"][0]["members"]) == 3
    index = {i["name"].lower(): i for i in corr}
    resolved = resolve_merge(plan["merges"][0], index)
    assert resolved is not None
    assert resolved["target"]["id"] == 2  # highest count (18); canonical is a new name
    assert {o["id"] for o in resolved["others"]} == {1}  # "Unknown" dropped as not in index
    assert resolve_merge({"canonical": "X", "members": ["Allianz", "nope"]}, index) is None  # <2 known
    # Fold small tags into an EXISTING bigger tag: use it as target directly, no rename.
    big_index = {i["name"].lower(): i for i in [
        {"id": 10, "name": "Bank", "document_count": 143},
        {"id": 11, "name": "Bankgebühren", "document_count": 1},
        {"id": 12, "name": "Bankbeleg", "document_count": 2},
    ]}
    r = resolve_merge({"canonical": "Bank", "members": ["Bankgebühren", "Bankbeleg"]}, big_index)
    assert r["target"]["id"] == 10 and {o["id"] for o in r["others"]} == {11, 12}
    assert r["canonical"] == "Bank"  # equals target name → execute_merge does not rename
    r1 = resolve_merge({"canonical": "Bank", "members": ["Bankgebühren"]}, big_index)  # single member ok
    assert r1 is not None and r1["target"]["id"] == 10 and {o["id"] for o in r1["others"]} == {11}
    r2 = resolve_merge({"canonical": "Bankbeleg", "members": ["Bankgebühren", "Bankbeleg"]}, big_index)
    assert r2["target"]["id"] == 12 and {o["id"] for o in r2["others"]} == {11}  # canonical is a member
    assert reassign_params("tags", 9, 3) == ("modify_tags", {"add_tags": [9], "remove_tags": [3]})
    assert reassign_params("correspondents", 9, 3) == ("set_correspondent", {"correspondent": 9})
    assert reassign_params("document-types", 9, 3) == ("set_document_type", {"document_type": 9})
    try:
        parse_llm_plan("sorry, no json here")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # Full plan resolved up front; a name already merged is not also deleted.
    actions = build_action_plan(
        {
            "merges": [{"canonical": "Allianz Leben", "members": [
                "Allianz Lebensversicherungs-AG", "Allianz Lebensversicherungs-Aktiengesellschaft", "Allianz"]}],
            "deletes": ["Allianz", "Deutsche Bank"],
        },
        index,
    )
    assert [a["kind"] for a in actions] == ["merge", "delete"]  # delete of merged "Allianz" dropped
    assert actions[1]["item"]["id"] == 4  # only Deutsche Bank remains as a delete
    assert describe_action(actions[0], "correspondent")[0] == "Merge into 'Allianz Leben':"
    # Overlapping merges (e.g. from combined chunks) are made conflict-free: the 2nd is skipped.
    ov = build_action_plan({"merges": [{"canonical": "Bank", "members": ["Bankgebühren", "Bankbeleg"]},
                                       {"canonical": "Bankbeleg", "members": ["Bankbeleg", "Bankgebühren"]}],
                            "deletes": []}, big_index)
    assert len(ov) == 1 and ov[0]["target"]["id"] == 10
    ns = build_parser().parse_args(["--resource", "tags", "--apply", "--dry-run", "--verbose"])
    assert ns.resource == "tags" and ns.apply and ns.dry_run and ns.verbose and not ns.no_llm
    assert "HTTP 200" in empty_hint("tag", "https://host/api/tags/")
    # Paginated `next` links are forced back onto the base URL's scheme+host.
    assert same_origin("http://host/api/tags/?page=2", "https://host") == "https://host/api/tags/?page=2"
    # A base URL that already ends in /api (old paperless-ai format) is trimmed, not doubled.
    assert normalize_base_url("https://host/api/") == "https://host"
    assert normalize_base_url("https://host/api") == "https://host"
    assert normalize_base_url("https://host/") == "https://host"
    assert normalize_base_url("https://host/paperless") == "https://host/paperless"
    # Plan-file parsing: comments, blank lines, and indentation are all tolerated.
    parsed = parse_plan_file(
        "# header\nMERGE Bank    # keep, 143 docs\n  Bankgebühren   # (1)\n  Bankbeleg\n"
        "DELETE Dokument   # 185 docs\n\nMERGE Vertrag\n  Contract\n"
    )
    assert parsed["deletes"] == ["Dokument"]
    assert parsed["merges"][0] == {"canonical": "Bank", "members": ["Bankgebühren", "Bankbeleg"]}
    assert parsed["merges"][1] == {"canonical": "Vertrag", "members": ["Contract"]}
    # Round-trip: build actions -> file -> parse -> build actions yields the same result.
    p0 = {"merges": [{"canonical": "Bank", "members": ["Bankgebühren", "Bankbeleg"]}], "deletes": []}
    a0 = build_action_plan(p0, big_index)
    file_text = format_plan_file("tags", a0)
    a1 = build_action_plan(parse_plan_file(file_text), big_index)
    assert a1[0]["target"]["id"] == 10 and {o["id"] for o in a1[0]["others"]} == {11, 12}
    # The resource marker is embedded and read back (so --apply-plan needs no --resource).
    assert read_plan_resource(file_text) == "tags"
    assert read_plan_resource(format_plan_file("document-types", a0)) == "document-types"
    assert read_plan_resource("# just a comment\nMERGE X\n  Y\n") is None
    ns2 = build_parser().parse_args(["--resource", "document-types", "--plan", "x.plan"])
    assert ns2.plan == "x.plan" and ns2.resource == "document-types"
    ns3 = build_parser().parse_args(["--resource", "tags", "--apply-plan", "y.plan", "--yes"])
    assert ns3.apply_plan == "y.plan" and ns3.yes
    ns4 = build_parser().parse_args(["--plan"])  # bare --plan → default per-resource filenames
    assert ns4.plan == "" and ns4.apply_plan is None
    # Ollama response handling surfaces errors and empty replies instead of hiding them.
    assert extract_ollama_response({"response": "  hi  "}) == "hi"
    for bad in ({"error": "model not found"}, {"response": "", "done_reason": "load"}, {"response": None}):
        try:
            extract_ollama_response(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    print("self-test OK")


# --- entrypoint ---------------------------------------------------------------------


HELP_EPILOG = f"""\
Modes:
  (default)            Read-only analysis: stats, unused/low-use items, near-duplicate
                       groups, and (unless --no-llm) an LLM's merge/delete suggestions.
  --apply              Interactively apply changes: the model returns a plan, the FULL
                       resolved plan is printed, then each merge/delete is confirmed one
                       at a time (y/N/q) and executed via the Paperless API. WRITES DATA.
  --apply --dry-run    Print the full resolved plan but make no changes (safe preview).
  --plan [FILE]        Write the model's plan to an EDITABLE FILE and stop (no changes). With
                       no FILE, writes <resource>_<YYYYmmdd_HHMMSS>.plan for each selected
                       resource (so bare --plan covers all three, each independently); an
                       explicit FILE needs one --resource. Prompts before overwriting.
  --apply-plan FILE    Apply an edited (already-curated) plan file (skips the model): confirm
                       once, then apply the whole plan. Resource comes from the file's
                       'resource:' marker (or pass --resource). Use --dry-run to preview it.

  --apply / --plan / --apply-plan are mutually exclusive. The edited-plan workflow lets you
  correct groupings the model gets wrong before anything is written.

Configuration ({ENV_FILE.name} next to this script; real env vars override it):
  PAPERLESS_URL        Base URL of Paperless-ngx (e.g. https://paperless.example.tld).
  PAPERLESS_TOKEN      API token (Settings -> create token). Must belong to a user who can
                       see the tags/types/correspondents (a superuser token is safest).
  OLLAMA_HOST          Ollama base URL (e.g. http://gpu-box.lan:11434). Needed unless --no-llm.
  OLLAMA_MODEL         Model name (e.g. gemma4-paperless).
  OLLAMA_NUM_CTX       Optional context window (default 16384).
  OLLAMA_TEMPERATURE   Optional sampling temperature (default 0.2 — low = conservative).
  SINGLETON_THRESHOLD  Optional; items with <= this many docs are flagged low-use (default 2).

Examples:
  taxonomy-audit.py                                     # analyse all three (read-only)
  taxonomy-audit.py --resource correspondents           # one resource
  taxonomy-audit.py --no-llm --verbose                  # local stats only, log requests
  taxonomy-audit.py --resource document-types --plan dt.plan       # generate an editable plan
  # ...edit dt.plan (fix/split/remove groups)...
  taxonomy-audit.py --resource document-types --apply-plan dt.plan # apply the edited plan
  taxonomy-audit.py --resource tags --apply --dry-run   # or: model plan, preview, no writes
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="taxonomy-audit.py",
        description="Audit and optionally clean up a Paperless-ngx taxonomy (tags, document "
        "types, correspondents) with a local LLM — no RAG needed.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--resource", choices=[*RESOURCES, "all"], default="all",
                   help="Which taxonomy to work on (default: all).")
    p.add_argument("--no-llm", action="store_true",
                   help="Analysis only: skip the Ollama suggestions (no Ollama config needed).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="Interactively apply merges/deletes (WRITES to Paperless).")
    mode.add_argument("--plan", nargs="?", const="", default=None, metavar="FILE",
                      help="Write the model's plan to an editable file and stop. With no FILE, "
                           "writes <resource>_<YYYYmmdd_HHMMSS>.plan for each selected resource; "
                           "an explicit FILE needs a single --resource. Prompts before overwriting.")
    mode.add_argument("--apply-plan", metavar="FILE", dest="apply_plan",
                      help="Apply an edited (already-curated) plan FILE instead of asking the "
                           "model: confirm once, then apply the whole plan. Resource comes from "
                           "the file's 'resource:' marker, or pass --resource.")
    p.add_argument("--yes", action="store_true",
                   help="With --apply: apply all actions without the per-action prompts "
                        "(--apply-plan already applies the whole plan after one confirmation).")
    p.add_argument("--dry-run", action="store_true",
                   help="With --apply/--apply-plan: show the full resolved plan but change nothing.")
    p.add_argument("--verbose", action="store_true",
                   help="Log each HTTP request, status, and result count to stderr (debugging).")
    p.add_argument("--self-test", action="store_true",
                   help="Run internal unit tests and exit.")
    return p


def main(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)
    global VERBOSE
    VERBOSE = args.verbose

    if args.self_test:
        self_test()
        return

    resource_keys = list(RESOURCES) if args.resource == "all" else [args.resource]
    run_llm, apply, dry_run = not args.no_llm, args.apply, args.dry_run
    plan_mode = args.plan is not None  # "" (default names) or an explicit filename
    # An edited plan file is already curated, so --apply-plan applies all actions after the
    # single "Proceed?" gate; --apply (uncurated model plan) confirms each unless --yes.
    assume_yes = args.yes or bool(args.apply_plan)

    # An explicit --plan FILE targets a single resource (bare --plan fans out per resource).
    if plan_mode and args.plan and args.resource == "all":
        print("--plan FILE needs a single --resource; use bare --plan to write "
              "<resource>_<date-time>.plan for each resource.", file=sys.stderr)
        sys.exit(2)

    # --apply-plan resolves its resource from --resource, else the file's 'resource:' marker.
    apply_key: str | None = None
    if args.apply_plan:
        if not Path(args.apply_plan).is_file():
            print(f"Plan file not found: {args.apply_plan}", file=sys.stderr)
            sys.exit(2)
        if args.resource != "all":
            apply_key = args.resource
        else:
            apply_key = read_plan_resource(Path(args.apply_plan).read_text(encoding="utf-8"))
            if apply_key is None:
                print(f"Can't tell which taxonomy {args.apply_plan} is for — it has no "
                      "'# resource:' marker. Pass --resource tags|document-types|correspondents.",
                      file=sys.stderr)
                sys.exit(2)
        resource_keys = [apply_key]

    # The LLM (and Ollama config) is needed for analysis, --apply, and --plan generation.
    # --apply-plan reads an existing file, so it needs no Ollama.
    need_ollama = (run_llm or apply or plan_mode) and not args.apply_plan
    cfg = load_config()
    required = ["PAPERLESS_URL", "PAPERLESS_TOKEN"] + (["OLLAMA_HOST", "OLLAMA_MODEL"] if need_ollama else [])
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(
            f"Missing config: {', '.join(missing)}.\n"
            f"Copy {ENV_FILE.name}.template to {ENV_FILE.name} and fill it in "
            "(or set the variables in the environment).",
            file=sys.stderr,
        )
        sys.exit(2)

    # Generate editable plan file(s) and stop — no writes to Paperless. Default names are
    # <resource>_<YYYYmmdd_HHMMSS>.plan so successive runs don't clobber and files sort by
    # resource. A model failure for one resource does not affect the others.
    if plan_mode:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        written, failed = [], []
        for key in resource_keys:
            path = Path(args.plan) if args.plan else Path(f"{key}_{stamp}.plan")
            if path.exists() and not prompt_yn(f"\n{path} already exists — overwrite?"):
                print(f"Kept existing {path}; skipping {key}.")
                continue
            (written if write_plan(key, cfg, path) else failed).append(key)
        if len(resource_keys) > 1:
            print(f"\nPlan files written for: {', '.join(written) or 'none'}."
                  + (f"  No plan for: {', '.join(failed)} — the model returned nothing "
                     "(check `ollama ps` / the errors above)." if failed else ""))
        return

    writes = (apply or args.apply_plan) and not dry_run
    if writes:
        print("WRITE MODE — this will MODIFY Paperless (reassign documents, delete/rename items).")
        print(f"Target: {cfg['PAPERLESS_URL']} | resources: {', '.join(resource_keys)}"
              + (" | applying the whole plan after this" if assume_yes else ""))
        print("Press 'q' at any prompt to abort.\n")
        if not prompt_yn("Proceed?"):
            print("Aborted.")
            return

    try:
        if args.apply_plan:
            apply_plan_file(apply_key, cfg, Path(args.apply_plan), dry_run, assume_yes)
        elif apply:
            for key in resource_keys:
                apply_resource(key, cfg, dry_run, args.yes)
        else:
            for key in resource_keys:
                audit_resource(key, cfg, run_llm)
    except KeyboardInterrupt:
        print("\nAborted — remaining changes were not applied.")
        sys.exit(130)


if __name__ == "__main__":
    main(sys.argv[1:])
