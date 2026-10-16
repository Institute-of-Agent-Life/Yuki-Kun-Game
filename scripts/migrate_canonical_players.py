"""Offline migration of this plugin's QQ-era saves to their recorded owners.

Run while Yuki is stopped. Only this plugin's ``plugin_state`` rows are written;
identity tables are read as historical ownership evidence. The default is a
read-only plan. ``--apply`` first creates a consistent SQLite backup.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

PLUGIN_ID = "io.github.yuanyeyoutao.kun-game"


@dataclass(frozen=True)
class Diagnostic:
    row_id: int
    category: str


@dataclass(frozen=True)
class MigrationResult:
    converted_rows: int
    moved_legacy_rows: int
    diagnostics: tuple[Diagnostic, ...]


def _person(connection: sqlite3.Connection, account: str) -> str | None:
    # A transferred account may have multiple historical owners. The save has
    # no per-player ownership timestamp, so the latest binding cannot choose.
    owners = {
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT b.person_id FROM identity_bindings b
            WHERE b.platform='qq' AND b.external_account_id=:account
              AND NOT EXISTS (SELECT 1 FROM presences p WHERE p.platform='qq'
                              AND p.external_account_id=b.external_account_id)
            """,
            {"account": account},
        )
    }
    return next(iter(owners)) if len(owners) == 1 else None


def _owner(connection: sqlite3.Connection, scope_type: str, account: str) -> str | None:
    if scope_type == "private":
        return _person(connection, account)
    owners = {
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT space_id FROM space_bindings
            WHERE platform='qq' AND external_space_id=:account
            """,
            {"account": account},
        )
    }
    return next(iter(owners)) if len(owners) == 1 else None


def _convert(envelope, *, scope_type, old_scope_id, owner, connection):
    value = deepcopy(envelope)
    if (
        value["schema_version"] != 1
        or value["scope_type"] != scope_type
        or value["scope_id"] != old_scope_id
    ):
        raise ValueError("legacy_scope_mismatch")
    state = value["state"]
    identities = {}

    def person(account):
        if account not in identities:
            identities[account] = _person(connection, account)
        if identities[account] is None:
            raise ValueError("legacy_player_unresolved")
        return identities[account]

    def killer(account):
        return account if account in (None, "BOSS") else person(account)

    players = {}
    for account, player in state["players"].items():
        if player["qq"] != account:
            raise ValueError("legacy_player_key_mismatch")
        owner_id = person(account)
        if owner_id in players:
            raise ValueError("independent_players_not_merged")
        player["person_id"] = owner_id
        del player["qq"]
        if player["kun"] is not None:
            player["kun"]["killer"] = killer(player["kun"].get("killer"))
        players[owner_id] = player
    state["players"] = players
    for name in ("anti_assault", "anti_devour", "anti_attack"):
        state[name] = [person(account) for account in state[name]]
    for entry in state["death_list"]:
        entry["person_id"] = person(entry.pop("qq"))
    auction = state["auction"]
    if auction is not None:
        auction["seller"] = person(auction["seller"])
        auction["bidder"] = person(auction["bidder"]) if auction["bidder"] is not None else None
        auction["kun"]["killer"] = killer(auction["kun"].get("killer"))
    boss = state["boss"]
    if boss is not None:
        boss["killer"] = killer(boss.get("killer"))
        boss["damage_rank"] = {
            person(account): damage for account, damage in boss["damage_rank"].items()
        }
        for name in ("anti_assault", "anti_devour", "anti_attack"):
            boss[name] = [person(account) for account in boss[name]]
    game = state["mini_game"]
    if game is not None:
        game["participants"] = [person(account) for account in game["participants"]]
        game["rewards"] = {person(account): reward for account, reward in game["rewards"].items()}
    champion = state["duel_champion"]
    if champion is not None:
        champion["person_id"] = person(champion.pop("uid"))
    del state["gid"]
    value["schema_version"] = 2
    value["scope_id"] = owner
    return value


def migrate(connection: sqlite3.Connection, *, apply: bool = False) -> MigrationResult:
    """Prepare the exact legacy rows, then apply only their short SQL updates.

    Unresolved players keep their original v1 JSON at the resolved canonical
    scope. That existing save remains visibly unreadable rather than becoming
    an empty game. Several independent saves are never economically merged.
    No Host Python package or Alembic version is required.
    """

    rows = [
        dict(zip(("id", "key", "value_json", "canonical_person_id"), row, strict=True))
        for row in connection.execute(
            """
            SELECT id,key,value_json,canonical_person_id FROM plugin_state
            WHERE plugin_id=:plugin AND namespace='state' ORDER BY id
            """,
            {"plugin": PLUGIN_ID},
        )
    ]
    groups = defaultdict(list)
    keys = {row["key"] for row in rows}
    diagnostics = []
    for row in rows:
        scope_type, separator, account = row["key"].partition(":")
        if not separator or scope_type not in ("group", "private"):
            continue
        try:
            envelope = json.loads(row["value_json"])
            if envelope["schema_version"] == 2:
                continue
        except (ValueError, KeyError, TypeError):
            diagnostics.append(Diagnostic(row["id"], "invalid_legacy_state"))
            continue
        owner = _owner(connection, scope_type, account)
        if owner is None:
            diagnostics.append(Diagnostic(row["id"], "legacy_scope_owner_unresolved"))
            continue
        if scope_type == "private" and row["canonical_person_id"] not in (None, owner):
            diagnostics.append(Diagnostic(row["id"], "private_owner_mismatch"))
            continue
        groups[(scope_type, owner)].append((row, account, envelope))

    prepared = []
    converted, moved_legacy = 0, 0
    for (scope_type, owner), originals in groups.items():
        key = f"{scope_type}:{owner}"
        if key in keys:
            diagnostics.extend(
                Diagnostic(row["id"], "canonical_scope_already_exists") for row, _, _ in originals
            )
            continue
        row, account, envelope = originals[0]
        payload = row["value_json"]
        if len(originals) == 1:
            try:
                payload = json.dumps(
                    _convert(
                        envelope,
                        scope_type=scope_type,
                        old_scope_id=account,
                        owner=owner,
                        connection=connection,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                converted += 1
            except ValueError as exc:
                diagnostics.append(Diagnostic(row["id"], str(exc)))
                moved_legacy += 1
            except (KeyError, TypeError, AttributeError):
                diagnostics.append(Diagnostic(row["id"], "invalid_legacy_state"))
                moved_legacy += 1
        else:
            diagnostics.extend(
                Diagnostic(item["id"], "independent_scopes_not_merged") for item, _, _ in originals
            )
            moved_legacy += 1
        prepared.append(
            {
                "id": row["id"],
                "key": key,
                "value": payload,
                "person": owner if scope_type == "private" else row["canonical_person_id"],
                "plugin": PLUGIN_ID,
            }
        )
    if apply and prepared:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                UPDATE plugin_state SET key=:key,value_json=:value,canonical_person_id=:person
                WHERE id=:id AND plugin_id=:plugin AND namespace='state'
                """,
                prepared,
            )
    return MigrationResult(converted, moved_legacy, tuple(diagnostics))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Stopped Yuki's SQLite database")
    parser.add_argument("--apply", action="store_true", help="Back up, then write the migration")
    parser.add_argument("--backup", type=Path, help="New backup path (default: alongside database)")
    args = parser.parse_args(argv)
    mode = "rw" if args.apply else "ro"
    backup = None
    with sqlite3.connect(f"{args.database.resolve().as_uri()}?mode={mode}", uri=True) as connection:
        if args.apply:
            backup = args.backup or args.database.with_name(
                args.database.name + datetime.now(UTC).strftime(".kun-v1-%Y%m%dT%H%M%S%fZ.bak")
            )
            with backup.open("xb"):
                pass
            with sqlite3.connect(backup) as destination:
                connection.backup(destination)
        result = migrate(connection, apply=args.apply)
    output = asdict(result)
    output["mode"] = "applied" if args.apply else "read_only"
    output["backup"] = str(backup) if backup is not None else None
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 2 if result.diagnostics else 0


if __name__ == "__main__":
    raise SystemExit(main())
