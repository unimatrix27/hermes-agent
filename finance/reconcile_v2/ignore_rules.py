"""Parser for ``ignore_rules.md``.

The file is human-readable and human-editable; the LLM may also append
to it when Sebastian says "ignoriere IBAN X komplett". Format:

    # Comment line, blank lines, or:
    iban:DE12345...                   → flat-rate reason
    counterparty:Vodafone GmbH        → portal-only vendor; receipts in customer portal
    verwendungszweck:Lohn|Gehalt      → payroll; not a business expense

Whitespace around tokens is tolerated. The right-hand side after the
arrow is the reason that gets recorded when ``list_open_txs`` auto-skips
a transaction by rule. The arrow itself can be ``->`` or ``→``.

Each rule's *pattern* is a case-insensitive substring match against the
named field; the verwendungszweck pattern additionally supports ``|``
to express "any of these tokens".

Why a flat text file:

* Sebastian wants to edit it by hand; YAML / JSON adds ceremony.
* A diff over git shows exactly which rule was added when.
* The LLM appends in the same shape it reads — no schema migration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


_ARROW_RE = re.compile(r"\s*(?:->|→)\s*")
_FIELDS = ("iban", "counterparty", "verwendungszweck")


@dataclass(frozen=True)
class IgnoreRule:
    field: str            # one of _FIELDS
    pattern: str          # original pattern text (may contain '|')
    reason: str
    raw: str              # the original line for debugging
    line_no: int

    def matches(self, tx: dict[str, Any]) -> bool:
        if self.field == "iban":
            hay = (tx.get("counterparty_iban") or "")
        elif self.field == "counterparty":
            hay = (tx.get("counterparty_name") or "")
        elif self.field == "verwendungszweck":
            hay = (tx.get("remittance_information") or "")
        else:  # pragma: no cover — guarded at parse time
            return False
        hay = hay.lower()
        for alt in self.pattern.split("|"):
            tok = alt.strip().lower()
            if tok and tok in hay:
                return True
        return False


def parse_rules(text: str) -> list[IgnoreRule]:
    """Parse the file. Blank / ``#``-prefixed lines are skipped.

    Lines that don't match the ``field:pattern → reason`` shape are
    silently ignored; the file's own header comments tell humans what
    the format is, so we don't want to reject the file because someone
    pasted an explanatory sentence.
    """
    out: list[IgnoreRule] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _ARROW_RE.split(line, maxsplit=1)
        if len(parts) != 2:
            continue
        lhs, reason = parts[0].strip(), parts[1].strip()
        if ":" not in lhs:
            continue
        field, pattern = lhs.split(":", 1)
        field = field.strip().lower()
        pattern = pattern.strip()
        if field not in _FIELDS or not pattern or not reason:
            continue
        out.append(IgnoreRule(
            field=field, pattern=pattern, reason=reason,
            raw=line, line_no=i,
        ))
    return out


def parse_rules_file(path: Path) -> list[IgnoreRule]:
    if not path.exists():
        return []
    return parse_rules(path.read_text(encoding="utf-8"))


def apply_rules(
    txs: Iterable[dict[str, Any]],
    rules: list[IgnoreRule],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split (kept, would_be_ignored).

    ``would_be_ignored`` carries ``rule`` and ``reason`` keys so the
    agent can decide whether to call ``mark_ignored`` (persistent) or
    just skip silently this run.
    """
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for tx in txs:
        match: Optional[IgnoreRule] = None
        for rule in rules:
            if rule.matches(tx):
                match = rule
                break
        if match is None:
            kept.append(tx)
        else:
            skipped.append({
                "tx":     tx,
                "rule":   match.raw,
                "field":  match.field,
                "reason": match.reason,
            })
    return kept, skipped


DEFAULT_IGNORE_RULES_PATH = (
    Path.home() / ".hermes" / "skills"
    / "finance" / "reconcile-receipts-v2" / "ignore_rules.md"
)
