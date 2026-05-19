#!/usr/bin/env python3
"""Sync recent Enable Banking transactions into Supabase.

This script is intentionally idempotent: it reads recent transactions from
Enable Banking and inserts only rows that do not already exist in
bank.transactions. Existing rows are left untouched.

Configuration is read from either ENABLEBANKING_CONFIG_JSON or
ENABLEBANKING_CONFIG_FILE. Minimal shape:

{
  "lookback_days": 7,
  "accounts": [
    {
      "source": "eb-dkb",
      "bank": "DKB",
      "product": "DKB-Business",
      "connector": "DkbConnector",
      "settings": {
        "sandbox": false,
        "consentId": "...",
        "accessToken": "...",
        "refreshToken": "...",
        "redirectUri": "...",
        "country": "DE",
        "clientId": "...",
        "clientSecret": "...",
        "signKeyPath": "/path/to/key.pem"
      },
      "account_uid": "Enable Banking account resource id",
      "iban": "optional override"
    }
  ]
}

The script uses the official `enablebanking-api` Python SDK when available.
Connector modules are not bundled with that SDK; install/copy the required
Enable Banking connector package(s) into the runtime environment first.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import jwt
import psycopg
import requests
from psycopg.types.json import Jsonb


DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_DIRECT_CONFIG_PATH = Path.home() / ".hermes/finance/enablebanking.json"
DEFAULT_DIRECT_SESSIONS_PATH = Path.home() / ".hermes/finance/enablebanking-sessions.json"
DEFAULT_DIRECT_PRIVATE_KEY_PATH = Path.home() / ".hermes/finance/enablebanking-private.pem"


@dataclass(frozen=True)
class AccountConfig:
    source: str
    bank: str
    product: str | None
    connector: str
    settings: dict[str, Any]
    account_uid: str
    iban: str | None = None
    account_holder: str | None = None
    currency: str | None = None


def _json_default(value: Any) -> str:
    if isinstance(value, (dt.date, dt.datetime, Decimal)):
        return str(value)
    return repr(value)


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    raise TypeError(f"cannot convert {type(obj)!r} to dict")


def _read_config() -> dict[str, Any]:
    raw = os.environ.get("ENABLEBANKING_CONFIG_JSON")
    if raw:
        return json.loads(raw)
    path = os.environ.get("ENABLEBANKING_CONFIG_FILE")
    if path:
        return json.loads(Path(path).read_text())
    if DEFAULT_DIRECT_CONFIG_PATH.exists() and DEFAULT_DIRECT_SESSIONS_PATH.exists():
        return _read_direct_config(DEFAULT_DIRECT_CONFIG_PATH, DEFAULT_DIRECT_SESSIONS_PATH)
    raise RuntimeError(
        "missing Enable Banking config: set ENABLEBANKING_CONFIG_JSON or "
        "ENABLEBANKING_CONFIG_FILE, or create ~/.hermes/finance/enablebanking.json "
        "and ~/.hermes/finance/enablebanking-sessions.json"
    )


def _read_direct_config(config_path: Path, sessions_path: Path) -> dict[str, Any]:
    """Build account config from direct Enable Banking API credential files."""
    app = json.loads(config_path.read_text())
    sessions = json.loads(sessions_path.read_text())
    private_key_path = os.environ.get("ENABLEBANKING_PRIVATE_KEY_FILE", str(DEFAULT_DIRECT_PRIVATE_KEY_PATH))
    accounts: list[dict[str, Any]] = []
    for session_name, session in sessions.items():
        bank_name = (session.get("aspsp") or {}).get("name") or session_name
        for account in session.get("accounts") or []:
            details = account.get("details")
            product = account.get("product") or bank_name
            if details:
                product = f"{product} {details}"
            accounts.append(
                {
                    "source": f"eb-{session_name}",
                    "bank": bank_name,
                    "product": product,
                    "connector": "direct",
                    "settings": {
                        "api_base": app.get("api_base", "https://api.enablebanking.com"),
                        "app_id": app["app_id"],
                        "private_key_path": private_key_path,
                        "session_id": session["session_id"],
                    },
                    "account_uid": account["uid"],
                    "iban": account.get("iban"),
                    "account_holder": account.get("name"),
                    "currency": account.get("currency"),
                }
            )
    return {"mode": "direct", "lookback_days": DEFAULT_LOOKBACK_DAYS, "accounts": accounts}


def _accounts_from_config(config: dict[str, Any]) -> list[AccountConfig]:
    accounts = []
    for item in config.get("accounts") or []:
        accounts.append(
            AccountConfig(
                source=item["source"],
                bank=item["bank"],
                product=item.get("product"),
                connector=item["connector"],
                settings=dict(item.get("settings") or {}),
                account_uid=item["account_uid"],
                iban=item.get("iban"),
                account_holder=item.get("account_holder"),
                currency=item.get("currency"),
            )
        )
    if not accounts:
        raise RuntimeError("Enable Banking config contains no accounts")
    return accounts


def _import_enablebanking() -> Any:
    try:
        return importlib.import_module("enablebanking")
    except ImportError as exc:
        raise RuntimeError(
            "Python package `enablebanking-api` is not installed in this venv. "
            "Install it together with the required connector modules before enabling live sync."
        ) from exc


def _import_connector(connector_name: str) -> Any:
    candidates = [
        "enablebanking.connectors",
        f"enablebanking.connectors.{connector_name}",
    ]
    last_exc: Exception | None = None
    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
            if hasattr(module, connector_name):
                return getattr(module, connector_name)
        except Exception as exc:  # pragma: no cover - depends on external connector package
            last_exc = exc
    raise RuntimeError(
        f"Enable Banking connector {connector_name!r} not found. "
        "Install/copy the connector module supplied by Enable Banking."
    ) from last_exc


def _extract_iban(account: dict[str, Any], fallback: str | None) -> str | None:
    if fallback:
        return fallback
    account_id = account.get("account_id") or account.get("accountId") or {}
    if isinstance(account_id, dict):
        return account_id.get("iban") or account_id.get("IBAN")
    return None


def _extract_amount(tx: dict[str, Any]) -> tuple[Decimal, str]:
    amount_obj = tx.get("transaction_amount") or tx.get("transactionAmount") or {}
    if not isinstance(amount_obj, dict):
        raise ValueError(f"transaction has invalid amount object: {amount_obj!r}")
    amount = Decimal(str(amount_obj.get("amount")))
    currency = str(amount_obj.get("currency") or "EUR")
    return amount, currency


def _credit_debit(tx: dict[str, Any]) -> str:
    value = tx.get("credit_debit_indicator") or tx.get("creditDebitIndicator")
    if value in {"CRDT", "C"}:
        return "C"
    if value in {"DBIT", "D"}:
        return "D"
    raise ValueError(f"unknown credit/debit indicator: {value!r}")


def _signed_amount(amount: Decimal, credit_debit: str) -> Decimal:
    return amount if credit_debit == "C" else -amount


def _flatten_remittance(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("value") or item))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p).strip() or None
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or value) or None
    return str(value) or None


def _party(tx: dict[str, Any], credit_debit: str) -> tuple[str | None, str | None, str | None]:
    # For debit transactions our counterparty is the creditor; for credits it is the debtor.
    prefix = "creditor" if credit_debit == "D" else "debtor"
    party = tx.get(prefix) or {}
    account = tx.get(f"{prefix}_account") or tx.get(f"{prefix}Account") or {}
    agent = tx.get(f"{prefix}_agent") or tx.get(f"{prefix}Agent") or {}
    name = party.get("name") if isinstance(party, dict) else None
    iban = account.get("iban") if isinstance(account, dict) else None
    bic = None
    if isinstance(agent, dict):
        bic = agent.get("bicFi") or agent.get("bic_fi") or agent.get("bic")
    return name, iban, bic


def _entry_reference(tx: dict[str, Any], account_iban: str) -> str:
    ref = tx.get("entry_reference") or tx.get("entryReference") or tx.get("transaction_id") or tx.get("transactionId")
    if ref:
        return str(ref)
    # Last-resort deterministic id to preserve idempotency for banks that omit references.
    payload = json.dumps(tx, sort_keys=True, default=_json_default)
    return "generated-" + hashlib.sha256(f"{account_iban}:{payload}".encode()).hexdigest()[:32]


def _date_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)[:10]


def _bank_code(tx: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    btc = tx.get("bank_transaction_code") or tx.get("bankTransactionCode")
    if not isinstance(btc, dict):
        return None, None, None
    return (
        btc.get("domain") or btc.get("code"),
        btc.get("family") or btc.get("subCode"),
        btc.get("sub_family") or btc.get("description"),
    )


def _normalise_transaction(tx_obj: Any, account_iban: str, source: str) -> dict[str, Any]:
    tx = _to_dict(tx_obj)
    amount, currency = _extract_amount(tx)
    cd = _credit_debit(tx)
    counterparty_name, counterparty_iban, counterparty_bic = _party(tx, cd)
    code, sub_code, description = _bank_code(tx)
    booking_date = _date_value(tx.get("booking_date") or tx.get("bookingDate") or tx.get("transaction_date") or tx.get("transactionDate"))
    if not booking_date:
        raise ValueError(f"transaction has no booking date: {tx!r}")
    return {
        "account_iban": account_iban,
        "entry_reference": _entry_reference(tx, account_iban),
        "booking_date": booking_date,
        "value_date": _date_value(tx.get("value_date") or tx.get("valueDate")),
        "amount": amount,
        "currency": currency,
        "credit_debit": cd,
        "signed_amount": _signed_amount(amount, cd),
        "counterparty_name": counterparty_name,
        "counterparty_iban": counterparty_iban,
        "counterparty_bic": counterparty_bic,
        "remittance_information": _flatten_remittance(tx.get("remittance_information") or tx.get("remittanceInformation")),
        "bank_tx_code": code,
        "bank_tx_sub_code": sub_code,
        "bank_tx_description": description,
        "status": tx.get("status"),
        "source": source,
        "raw": tx,
    }



class DirectEnableBankingClient:
    def __init__(self, settings: dict[str, Any]):
        self.api_base = str(settings.get("api_base") or "https://api.enablebanking.com").rstrip("/")
        self.app_id = settings["app_id"]
        self.private_key_path = Path(settings["private_key_path"])
        self.session_id = settings["session_id"]

    def _token(self) -> str:
        now = int(time.time())
        private_key = self.private_key_path.read_text()
        return jwt.encode(
            {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600},
            private_key,
            algorithm="RS256",
            headers={"kid": self.app_id},
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.get(f"{self.api_base}{path}", headers=self._headers(), params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def get_accounts(self) -> dict[str, Any]:
        session = self._get(f"/sessions/{self.session_id}")
        accounts = []
        accounts_data = session.get("accounts_data") or {}
        for account_uid in session.get("accounts") or []:
            account = accounts_data.get(account_uid) if isinstance(accounts_data, dict) else None
            if isinstance(account, dict):
                account = {**account, "resource_id": account_uid}
            else:
                account = {"resource_id": account_uid}
            accounts.append(account)
        return {"accounts": accounts}

    def get_account_transactions(
        self,
        account_uid: str,
        date_from: dt.datetime | dt.date | str | None = None,
        date_to: dt.datetime | dt.date | str | None = None,
        transaction_status: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if date_from is not None:
            params["date_from"] = str(date_from)[:10]
        if date_to is not None:
            params["date_to"] = str(date_to)[:10]
        # The direct Enable Banking endpoint rejects `transaction_status` for some
        # ASPSPs. Date filtering is sufficient; we normalise the returned status.

        all_transactions: list[Any] = []
        continuation_key: str | None = None
        use_params = True
        while True:
            page_params = dict(params) if use_params else {}
            if continuation_key:
                page_params["continuation_key"] = continuation_key
            try:
                payload = self._get(f"/accounts/{account_uid}/transactions", page_params)
            except requests.HTTPError as exc:
                # Some ASPSPs (observed: Finom) reject date filters even though
                # others accept them. Retry unfiltered and filter locally.
                if use_params and exc.response is not None and exc.response.status_code == 400:
                    use_params = False
                    continuation_key = None
                    all_transactions = []
                    continue
                raise
            all_transactions.extend(payload.get("transactions") or [])
            continuation_key = payload.get("continuation_key")
            if not continuation_key:
                break

        if not use_params and (date_from is not None or date_to is not None):
            start = str(date_from)[:10] if date_from is not None else None
            end = str(date_to)[:10] if date_to is not None else None
            filtered = []
            for tx in all_transactions:
                tx_date = str(tx.get("booking_date") or tx.get("bookingDate") or tx.get("transaction_date") or tx.get("transactionDate") or "")[:10]
                if start and tx_date < start:
                    continue
                if end and tx_date >= end:
                    continue
                filtered.append(tx)
            all_transactions = filtered
        return {"transactions": all_transactions}

    def get_account_balances(self, account_uid: str) -> dict[str, Any]:
        return self._get(f"/accounts/{account_uid}/balances")

def _build_api(account_cfg: AccountConfig) -> Any:
    if account_cfg.connector == "direct":
        return DirectEnableBankingClient(account_cfg.settings)
    enablebanking = _import_enablebanking()
    connector = _import_connector(account_cfg.connector)
    client = enablebanking.ApiClient(connector, account_cfg.settings)
    return enablebanking.AispApi(client)


def _fetch_account(api: Any, account_uid: str) -> dict[str, Any]:
    # Not all connectors expose get_account; get_accounts + local match is broadly compatible.
    try:
        accounts_obj = api.get_accounts()
        accounts = _to_dict(accounts_obj).get("accounts") or []
        for account in accounts:
            acc = _to_dict(account)
            if str(acc.get("resource_id") or acc.get("resourceId")) == str(account_uid):
                return acc
    except Exception:
        return {}
    return {}


def _fetch_transactions(api: Any, account_uid: str, start: dt.date, end: dt.date) -> list[Any]:
    data = api.get_account_transactions(
        account_uid,
        date_from=dt.datetime.combine(start, dt.time.min),
        date_to=dt.datetime.combine(end, dt.time.min),
        transaction_status="BOOK",
    )
    payload = _to_dict(data)
    # SDK uses `transactions`; tolerate common HAL shapes as well.
    return payload.get("transactions") or payload.get("booked") or payload.get("items") or []


def _upsert_account(cur: Any, cfg: AccountConfig, account: dict[str, Any], iban: str, currency: str) -> None:
    holder = cfg.account_holder
    product = cfg.product or account.get("product")
    cur.execute(
        """
        INSERT INTO bank.accounts
            (iban, bank, product, currency, account_uid, account_holder, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (iban) DO UPDATE SET
            bank = EXCLUDED.bank,
            product = COALESCE(EXCLUDED.product, bank.accounts.product),
            currency = EXCLUDED.currency,
            account_uid = EXCLUDED.account_uid,
            account_holder = COALESCE(EXCLUDED.account_holder, bank.accounts.account_holder),
            updated_at = now()
        """,
        (iban, cfg.bank, product, currency, cfg.account_uid, holder),
    )


def _insert_transaction(cur: Any, row: dict[str, Any], dry_run: bool) -> bool:
    if dry_run:
        cur.execute(
            "SELECT 1 FROM bank.transactions WHERE account_iban=%s AND entry_reference=%s",
            (row["account_iban"], row["entry_reference"]),
        )
        return cur.fetchone() is None
    cur.execute(
        """
        INSERT INTO bank.transactions
            (account_iban, entry_reference, booking_date, value_date, amount, currency,
             credit_debit, signed_amount, counterparty_name, counterparty_iban,
             counterparty_bic, remittance_information, bank_tx_code, bank_tx_sub_code,
             bank_tx_description, status, source, raw, synced_at)
        VALUES
            (%(account_iban)s, %(entry_reference)s, %(booking_date)s, %(value_date)s,
             %(amount)s, %(currency)s, %(credit_debit)s, %(signed_amount)s,
             %(counterparty_name)s, %(counterparty_iban)s, %(counterparty_bic)s,
             %(remittance_information)s, %(bank_tx_code)s, %(bank_tx_sub_code)s,
             %(bank_tx_description)s, %(status)s, %(source)s, %(raw)s, now())
        ON CONFLICT (account_iban, entry_reference) DO NOTHING
        """,
        {**row, "raw": Jsonb(row["raw"])},
    )
    return cur.rowcount == 1


def run(dry_run: bool = False, lookback_days: int | None = None) -> dict[str, Any]:
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise RuntimeError("SUPABASE_DB_URL is not set")
    config = _read_config()
    days = lookback_days or int(config.get("lookback_days") or DEFAULT_LOOKBACK_DAYS)
    accounts = _accounts_from_config(config)
    start = dt.date.today() - dt.timedelta(days=days)
    end = dt.date.today() + dt.timedelta(days=1)  # date_to is exclusive in Enable Banking SDK

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "lookback_days": days,
        "accounts": 0,
        "fetched": 0,
        "inserted": 0,
        "skipped_existing": 0,
        "errors": [],
    }

    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        if not dry_run:
            # This Supabase pooler connection currently defaults to read-only.
            # Explicitly mark the sync transaction read-write before any SQL.
            conn.execute("SET TRANSACTION READ WRITE")
        with conn.cursor() as cur:
            for cfg in accounts:
                try:
                    api = _build_api(cfg)
                    account = _fetch_account(api, cfg.account_uid)
                    account_iban = _extract_iban(account, cfg.iban)
                    if not account_iban:
                        raise RuntimeError(f"cannot determine IBAN for account_uid={cfg.account_uid}")
                    transactions = _fetch_transactions(api, cfg.account_uid, start, end)
                    summary["accounts"] += 1
                    summary["fetched"] += len(transactions)

                    first_currency = cfg.currency
                    normalised = []
                    for tx in transactions:
                        row = _normalise_transaction(tx, account_iban, cfg.source)
                        first_currency = first_currency or row["currency"]
                        normalised.append(row)
                    if not dry_run:
                        _upsert_account(cur, cfg, account, account_iban, first_currency or "EUR")

                    for row in normalised:
                        inserted = _insert_transaction(cur, row, dry_run=dry_run)
                        if inserted:
                            summary["inserted"] += 1
                        else:
                            summary["skipped_existing"] += 1
                except Exception as exc:  # keep other accounts syncing
                    summary["errors"].append({"source": cfg.source, "account_uid": cfg.account_uid, "error": str(exc)})
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="fetch and compare but do not write")
    parser.add_argument("--lookback-days", type=int, default=None)
    args = parser.parse_args()
    try:
        result = run(dry_run=args.dry_run, lookback_days=args.lookback_days)
        print(json.dumps(result, indent=2, sort_keys=True, default=_json_default))
        return 1 if result.get("errors") else 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
