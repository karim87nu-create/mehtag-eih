"""Small additive schema migrations for deployments with an existing SQLite DB.

``metadata.create_all`` only creates missing tables.  Railway already has a
persistent SQLite volume, so new ownership columns must be added explicitly.
These migrations are deliberately additive, idempotent and safe for legacy rows:
unattributed rows stay NULL (quarantined), while a case linked to exactly one
valid UUID customer is attributed to that customer.
"""

from __future__ import annotations

from collections import defaultdict
import warnings

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .ownership import canonical_customer_ref


_OWNERSHIP_COLUMNS: dict[str, dict[str, str]] = {
    "requests": {
        "customer_ref": "VARCHAR(36)",
        "locale": "VARCHAR(20)",
        "region": "VARCHAR(16)",
        "currency": "VARCHAR(8)",
        "source_turn_id": "VARCHAR(36)",
    },
    "execution_cases": {"customer_ref": "VARCHAR(36)"},
    "external_cases": {
        "customer_ref": "VARCHAR(36)",
        "locale": "VARCHAR(20)",
        "region": "VARCHAR(16)",
        "currency": "VARCHAR(8)",
    },
    "detected_transactions": {
        "customer_ref": "VARCHAR(36)",
        "locale": "VARCHAR(20)",
        "region": "VARCHAR(16)",
        "currency": "VARCHAR(8)",
    },
    "mobile_source_events": {
        "customer_ref": "VARCHAR(36)",
        "locale": "VARCHAR(20)",
        "region": "VARCHAR(16)",
        "currency": "VARCHAR(8)",
    },
    "memory_facts": {"customer_ref": "VARCHAR(36)"},
    "learned_preferences": {"customer_ref": "VARCHAR(36)"},
    "customer_rules": {"customer_ref": "VARCHAR(36)"},
}


def _quote(identifier: str) -> str:
    # Identifiers are exclusively sourced from the constant mapping above.
    return '"' + identifier.replace('"', '""') + '"'


def _add_missing_columns(engine: Engine) -> None:
    existing_tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        for table, wanted in _OWNERSHIP_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {column["name"] for column in inspect(engine).get_columns(table)}
            for column, sql_type in wanted.items():
                if column in present:
                    continue
                connection.execute(text(
                    f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(column)} {sql_type}"
                ))


def _valid_link_owners(connection, case_type: str) -> dict[int, tuple[str, str | None, str | None, str | None]]:
    rows = connection.execute(text("""
        SELECT l.case_id, t.customer_ref, t.locale, t.region, t.currency
          FROM conversation_case_links AS l
          JOIN conversation_threads AS t ON t.id = l.thread_id
         WHERE l.case_type = :case_type
    """), {"case_type": case_type}).mappings()
    candidates: dict[int, dict[str, tuple[str, str | None, str | None, str | None]]] = defaultdict(dict)
    for row in rows:
        owner = canonical_customer_ref(row["customer_ref"])
        if owner:
            candidates[int(row["case_id"])][owner] = (
                owner, row["locale"], row["region"], row["currency"],
            )
    # Ambiguous links are intentionally left quarantined.
    return {
        case_id: next(iter(owners.values()))
        for case_id, owners in candidates.items()
        if len(owners) == 1
    }


def _backfill_unambiguous_links(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    required = {"conversation_case_links", "conversation_threads"}
    if not required.issubset(tables):
        return
    with engine.begin() as connection:
        for table, case_type in (("requests", "REQUEST"), ("external_cases", "EXTERNAL")):
            if table not in tables:
                continue
            for case_id, values in _valid_link_owners(connection, case_type).items():
                owner, locale, region, currency = values
                current = connection.execute(text(f"""
                    SELECT customer_ref FROM {_quote(table)} WHERE id = :case_id
                """), {"case_id": case_id}).scalar_one_or_none()
                # Preserve an already-valid owner (including a conflicting one)
                # for investigation. NULL and obsolete non-UUID sentinels can be
                # attributed only because the link set proved one unique owner.
                if canonical_customer_ref(current) is None:
                    connection.execute(text(f"""
                        UPDATE {_quote(table)}
                           SET customer_ref = :owner,
                               locale = COALESCE(locale, :locale),
                               region = COALESCE(region, :region),
                               currency = COALESCE(currency, :currency)
                         WHERE id = :case_id
                    """), {
                        "owner": owner, "locale": locale, "region": region,
                        "currency": currency, "case_id": case_id,
                    })

        if {"execution_cases", "requests"}.issubset(tables):
            # Request ownership is authoritative, but only canonical UUID4
            # owners are eligible. Old values such as ``anonymous`` remain
            # quarantined instead of being propagated into child rows.
            rows = connection.execute(text("""
                SELECT execution_cases.id AS child_id,
                       execution_cases.customer_ref AS child_owner,
                       requests.customer_ref AS parent_owner
                  FROM execution_cases
                  JOIN requests ON requests.id = execution_cases.request_id
            """)).mappings()
            for row in rows:
                parent_owner = canonical_customer_ref(row["parent_owner"])
                if parent_owner and canonical_customer_ref(row["child_owner"]) is None:
                    connection.execute(text("""
                        UPDATE execution_cases SET customer_ref = :owner
                         WHERE id = :child_id
                    """), {"owner": parent_owner, "child_id": row["child_id"]})

        if {"memory_facts", "requests"}.issubset(tables):
            rows = connection.execute(text("""
                SELECT memory_facts.id AS child_id,
                       memory_facts.customer_ref AS child_owner,
                       requests.customer_ref AS parent_owner
                  FROM memory_facts
                  JOIN requests ON requests.id = memory_facts.source_id
                 WHERE memory_facts.source_type = 'request'
            """)).mappings()
            for row in rows:
                parent_owner = canonical_customer_ref(row["parent_owner"])
                if parent_owner and canonical_customer_ref(row["child_owner"]) is None:
                    connection.execute(text("""
                        UPDATE memory_facts SET customer_ref = :owner
                         WHERE id = :child_id
                    """), {"owner": parent_owner, "child_id": row["child_id"]})


def _create_owner_indexes(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        for table, columns in _OWNERSHIP_COLUMNS.items():
            if table not in tables or "customer_ref" not in columns:
                continue
            connection.execute(text(
                f"CREATE INDEX IF NOT EXISTS {_quote(f'ix_{table}_customer_ref')} "
                f"ON {_quote(table)} ({_quote('customer_ref')})"
            ))


def _create_offer_identity_index(engine: Engine) -> None:
    """Apply the offer identity invariant to an existing deployment safely.

    Fresh databases get the matching model-level unique constraint. For a
    legacy database we add a unique index only when existing rows are already
    unambiguous. We deliberately do not delete or silently choose between
    conflicting historical offers during startup.
    """
    tables = set(inspect(engine).get_table_names())
    if "offers" not in tables:
        return
    columns = {column["name"] for column in inspect(engine).get_columns("offers")}
    if not {"request_id", "business_id"}.issubset(columns):
        return
    identity_columns = {"request_id", "business_id"}
    if any(
        set(constraint.get("column_names") or ()) == identity_columns
        for constraint in inspect(engine).get_unique_constraints("offers")
    ) or any(
        index.get("unique")
        and set(index.get("column_names") or ()) == identity_columns
        for index in inspect(engine).get_indexes("offers")
    ):
        return
    with engine.begin() as connection:
        conflict = connection.execute(text("""
            SELECT request_id, business_id
              FROM offers
             GROUP BY request_id, business_id
            HAVING COUNT(*) > 1
             LIMIT 1
        """)).first()
        if conflict:
            warnings.warn(
                "Offer identity migration skipped: duplicate legacy "
                f"request/business rows exist ({conflict[0]}, {conflict[1]}).",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_offers_request_business "
            "ON offers (request_id, business_id)"
        ))


def _create_request_turn_identity_index(engine: Engine) -> None:
    """Make a chat-authorized request durable across retries/restarts."""
    tables = set(inspect(engine).get_table_names())
    if "requests" not in tables:
        return
    columns = {column["name"] for column in inspect(engine).get_columns("requests")}
    if not {"customer_ref", "source_turn_id"}.issubset(columns):
        return
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_requests_customer_source_turn "
            "ON requests (customer_ref, source_turn_id)"
        ))


def migrate_customer_ownership_schema(engine: Engine) -> None:
    """Upgrade an existing database without ever assigning legacy rows broadly."""
    _add_missing_columns(engine)
    _backfill_unambiguous_links(engine)
    _create_owner_indexes(engine)
    _create_offer_identity_index(engine)
    _create_request_turn_identity_index(engine)
