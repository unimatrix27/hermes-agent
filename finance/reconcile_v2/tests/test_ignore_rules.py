"""Tests for the ignore-rules parser."""
from __future__ import annotations

from pathlib import Path

from finance.reconcile_v2.ignore_rules import apply_rules, parse_rules


def test_blank_and_comments_skipped():
    rules = parse_rules(
        "# this is a comment\n"
        "\n"
        "   \n"
        "iban:DE12 → reason A\n"
    )
    assert len(rules) == 1
    assert rules[0].field == "iban"
    assert rules[0].pattern == "DE12"
    assert rules[0].reason == "reason A"


def test_unicode_arrow_and_ascii_arrow_both_work():
    txt = (
        "counterparty:Vodafone -> portal-only\n"
        "verwendungszweck:Lohn|Gehalt → payroll\n"
    )
    rules = parse_rules(txt)
    assert [r.field for r in rules] == ["counterparty", "verwendungszweck"]
    assert "|" in rules[1].pattern


def test_unknown_field_silently_ignored():
    rules = parse_rules("zip_code:90210 → noise\n")
    assert rules == []


def test_apply_rules_iban_match():
    rules = parse_rules("iban:DE89 3704 0044 → payroll account\n")
    txs = [
        {"id": 1, "counterparty_iban": "DE89 3704 0044 0532 0130 00",
         "counterparty_name": "Lohn", "remittance_information": ""},
        {"id": 2, "counterparty_iban": "DE12 5001 0517",
         "counterparty_name": "Vodafone", "remittance_information": ""},
    ]
    kept, skipped = apply_rules(txs, rules)
    assert [t["id"] for t in kept] == [2]
    assert [s["tx"]["id"] for s in skipped] == [1]
    assert skipped[0]["reason"] == "payroll account"


def test_apply_rules_verwendungszweck_or_alternatives():
    rules = parse_rules("verwendungszweck:Lohn|Gehalt|Salary → payroll\n")
    txs = [
        {"id": 1, "counterparty_iban": None, "counterparty_name": "Anyone",
         "remittance_information": "Salary May 2026"},
        {"id": 2, "counterparty_iban": None, "counterparty_name": "Anyone",
         "remittance_information": "Coffee"},
    ]
    kept, skipped = apply_rules(txs, rules)
    assert [t["id"] for t in kept] == [2]
    assert [s["tx"]["id"] for s in skipped] == [1]


def test_case_insensitive_match():
    rules = parse_rules("counterparty:vodafone → portal-only\n")
    txs = [
        {"id": 1, "counterparty_iban": None,
         "counterparty_name": "VODAFONE GmbH",
         "remittance_information": ""},
    ]
    _, skipped = apply_rules(txs, rules)
    assert len(skipped) == 1
