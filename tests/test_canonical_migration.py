"""Offline SQLite upgrades retain every save without importing Yuki Host code."""

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest

import engine
from scripts.migrate_canonical_players import PLUGIN_ID, main, migrate

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def player(account, eggs):
    return {
        "qq": account,
        "kun": None,
        "jie_cao": 500,
        "eggs": eggs,
        "divine_weapon": 2,
        "phantom_pills": 3,
        "chicken_soup": 4,
        "resurrection_pills": 5,
        "luck": 40,
        "today_train": 6,
        "signed_today": True,
        "last_sign_date": NOW.date().isoformat(),
    }


def legacy():
    kun = {"name": "原拍卖鲲", "weight": 1234.5, "attribute": "怒", "alive": True, "killer": None}
    return {
        "schema_version": 1,
        "scope_type": "group",
        "scope_id": "5000",
        "revision": 37,
        "updated_at": NOW.isoformat(),
        "state": {
            "gid": "5000",
            "kun_enabled": True,
            "game_enabled": True,
            "players": {"0": player("0", 90), "1": player("1", 11)},
            "last_boss_date": NOW.date().isoformat(),
            "anti_assault": ["0"],
            "anti_devour": [],
            "anti_attack": ["1"],
            "auction": {
                "seller": "0",
                "seller_name": "原卖家",
                "bidder": "1",
                "kun": kun,
                "start_price": 10,
                "current_bid": 20,
                "start_time": NOW.isoformat(),
            },
            "boss": {
                "name": "原BOSS",
                "weight": 0,
                "alive": False,
                "attributes": ["怒"],
                "killer": "1",
                "damage_rank": {"0": 33, "1": 77},
                "anti_assault": ["0"],
                "anti_devour": [],
                "anti_attack": [],
            },
            "death_list": [{"qq": "1", "reason": "原阵亡事实"}],
            "mini_game": {
                "type": "奥数比赛",
                "slots_used": 1,
                "max_slots": 5,
                "participants": ["1"],
                "rewards": {"1": 3},
                "question": "1+1",
                "answer": "2",
            },
            "duel_champion": {"uid": "1", "name": "原冠军", "consecutive": 2},
        },
    }


def seed(path, mode="resolved"):
    people = {account: str(uuid4()) for account in ("0", "1", "2")}
    space, telegram_space = str(uuid4()), str(uuid4())
    value = legacy()
    if mode == "unknown":
        value["state"]["players"]["404"] = player("404", 999)
    elif mode == "collision":
        value["state"]["players"]["00"] = player("00", 999)
    encoded = json.dumps(value, ensure_ascii=False)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE persons(id TEXT PRIMARY KEY);
            CREATE TABLE spaces(id TEXT PRIMARY KEY);
            CREATE TABLE identity_bindings(
                person_id TEXT NOT NULL REFERENCES persons(id),
                platform TEXT NOT NULL, external_account_id TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE presences(platform TEXT NOT NULL, external_account_id TEXT NOT NULL);
            CREATE TABLE space_bindings(
                space_id TEXT NOT NULL REFERENCES spaces(id),
                platform TEXT NOT NULL, external_space_id TEXT NOT NULL
            );
            CREATE TABLE plugin_state(
                id INTEGER PRIMARY KEY, plugin_id TEXT NOT NULL, namespace TEXT NOT NULL,
                key TEXT NOT NULL, value_json TEXT NOT NULL, version INTEGER NOT NULL,
                canonical_person_id TEXT REFERENCES persons(id), updated_at TEXT NOT NULL,
                UNIQUE(plugin_id, namespace, key)
            );
        """)
        connection.executemany("INSERT INTO persons VALUES(?)", ((p,) for p in people.values()))
        connection.executemany("INSERT INTO spaces VALUES(?)", ((space,), (telegram_space,)))
        bindings = [(people[account], "qq", account, "active") for account in ("0", "1")]
        bindings.append((people["2"], "telegram", "0", "active"))
        if mode == "collision":
            bindings.append((people["0"], "qq", "00", "active"))
        elif mode == "transferred":
            bindings.append((people["2"], "qq", "0", "transferred"))
        connection.executemany("INSERT INTO identity_bindings VALUES(?,?,?,?)", bindings)
        connection.executemany(
            "INSERT INTO space_bindings VALUES(?,?,?)",
            ((space, "qq", "5000"), (telegram_space, "telegram", "5000")),
        )
        connection.execute(
            "INSERT INTO plugin_state VALUES(17,?,'state','group:5000',?,13,NULL,?)",
            (PLUGIN_ID, encoded, NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO plugin_state VALUES(18,'another-plugin','state','group:5000',"
            "'unchanged foreign plugin bytes',7,NULL,?)",
            (NOW.isoformat(),),
        )
    return space, telegram_space, people, value, encoded


@pytest.mark.parametrize("mode", ["resolved", "unknown", "collision", "transferred"])
def test_preserves_original_save_and_all_economic_references(tmp_path, mode):
    path = tmp_path / "kun.db"
    scope, _, people, old, encoded = seed(path, mode)
    with sqlite3.connect(path) as connection:
        result = migrate(connection, apply=True)
        row = connection.execute(
            "SELECT id,key,value_json,version,updated_at FROM plugin_state WHERE id=17"
        ).fetchone()
        assert row[:2] == (17, f"group:{scope}") and row[3:] == (13, NOW.isoformat())
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute(
            "SELECT value_json,version FROM plugin_state WHERE id=18"
        ).fetchone() == ("unchanged foreign plugin bytes", 7)
        if mode != "resolved":
            assert row[2] == encoded and result.moved_legacy_rows == 1
            assert result.converted_rows == 0 and result.diagnostics[0].row_id == 17
            assert result.diagnostics[0].category == (
                "independent_players_not_merged"
                if mode == "collision"
                else "legacy_player_unresolved"
            )
            return
        assert result.converted_rows == 1 and not result.diagnostics
        value = json.loads(row[2])
        engine.validate_envelope(value, scope_type="group", scope_id=scope)
        assert value["revision"] == 37 and value["updated_at"] == old["updated_at"]
        state = value["state"]
        assert value["schema_version"] == 2 and value["scope_id"] == scope and "gid" not in state
        assert state["players"][people["0"]]["eggs"] == 90
        assert state["players"][people["1"]]["eggs"] == 11
        assert (
            state["auction"]["seller"] == people["0"] and state["auction"]["bidder"] == people["1"]
        )
        assert state["auction"]["kun"] == old["state"]["auction"]["kun"]
        assert state["boss"]["damage_rank"] == {people["0"]: 33, people["1"]: 77}
        assert state["boss"]["killer"] == people["1"]
        assert state["mini_game"]["participants"] == [people["1"]]
        assert state["mini_game"]["rewards"] == {people["1"]: 3}
        assert state["duel_champion"]["person_id"] == people["1"]
        assert state["death_list"] == [{"person_id": people["1"], "reason": "原阵亡事实"}]
        before = connection.execute("SELECT * FROM plugin_state ORDER BY id").fetchall()
        assert migrate(connection, apply=True).converted_rows == 0
        assert connection.execute("SELECT * FROM plugin_state ORDER BY id").fetchall() == before


def test_read_only_plan_and_apply_backup_are_separate(tmp_path, capsys):
    path = tmp_path / "original.db"
    scope, _, _, _, encoded = seed(path)
    original_bytes = path.read_bytes()
    assert main([str(path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "read_only" and plan["converted_rows"] == 1
    assert path.read_bytes() == original_bytes
    backup = tmp_path / "consistent.db.bak"
    assert main([str(path), "--apply", "--backup", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "applied"
    with sqlite3.connect(backup) as connection:
        assert connection.execute(
            "SELECT key,value_json,version FROM plugin_state WHERE id=17"
        ).fetchone() == ("group:5000", encoded, 13)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT key FROM plugin_state WHERE id=17").fetchone() == (
            f"group:{scope}",
        )


def test_unresolved_scope_stays_exact_and_reports_only_row_identity(tmp_path, capsys):
    path = tmp_path / "unknown.db"
    _, _, _, _, encoded = seed(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM space_bindings WHERE platform='qq'")
    assert main([str(path), "--apply"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostics"] == [{"row_id": 17, "category": "legacy_scope_owner_unresolved"}]
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT key,value_json,version FROM plugin_state WHERE id=17"
        ).fetchone() == ("group:5000", encoded, 13)


def test_several_old_scopes_are_not_merged_or_deleted(tmp_path):
    path = tmp_path / "collision.db"
    scope, _, _, _, encoded = seed(path)
    second = legacy()
    second["scope_id"] = second["state"]["gid"] = "5001"
    second["state"]["players"]["0"]["eggs"] = 999
    second_bytes = json.dumps(second, ensure_ascii=False)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO space_bindings VALUES(?,'qq','5001')", (scope,))
        connection.execute(
            "INSERT INTO plugin_state VALUES(19,?,'state','group:5001',?,21,NULL,?)",
            (PLUGIN_ID, second_bytes, NOW.isoformat()),
        )
        connection.commit()
        result = migrate(connection, apply=True)
        assert result.converted_rows == 0 and result.moved_legacy_rows == 1
        assert {row.row_id for row in result.diagnostics} == {17, 19}
        assert connection.execute(
            "SELECT key,value_json,version FROM plugin_state WHERE id=17"
        ).fetchone() == (f"group:{scope}", encoded, 13)
        assert connection.execute(
            "SELECT key,value_json,version FROM plugin_state WHERE id=19"
        ).fetchone() == ("group:5001", second_bytes, 21)


def test_same_number_on_telegram_does_not_select_qq_save(tmp_path):
    path = tmp_path / "platform.db"
    qq_space, telegram_space, people, _, _ = seed(path)
    with sqlite3.connect(path) as connection:
        migrate(connection, apply=True)
        original = connection.execute("SELECT * FROM plugin_state ORDER BY id").fetchall()
        assert qq_space != telegram_space
        assert (
            connection.execute(
                "SELECT value_json FROM plugin_state "
                "WHERE plugin_id=? AND namespace='state' AND key=?",
                (PLUGIN_ID, f"group:{telegram_space}"),
            ).fetchone()
            is None
        )
        fresh = engine.default_envelope("group", telegram_space, NOW)
        assert not fresh["state"]["players"]
        assert people["2"] not in json.loads(original[0][4])["state"]["players"]
        assert connection.execute("SELECT * FROM plugin_state ORDER BY id").fetchall() == original
