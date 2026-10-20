"""Exercise the plugin against the actual installed Host SDK, without network or chat sends."""

import asyncio
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from yuki import __version__
from yuki.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki.plugin_host.loader import PluginLoader
from yuki.plugin_host.manifest import load_manifest
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import CurrentMessage, PermissionLevel
from yuki_plugin_sdk.testing import FakePluginContext, run_plugin_contract_tests

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def plugin_root(tmp_path):
    target = tmp_path / "io.github.yuanyeyoutao.kun-game"
    target.mkdir()
    for name in ("plugin.toml", "plugin.py", "engine.py"):
        shutil.copyfile(ROOT / name, target / name)
    return target


def test_actual_sdk7_loads_and_completes_plugin_lifecycle(plugin_root):
    assert PLUGIN_API_VERSION == "7.0"
    report = asyncio.run(run_plugin_contract_tests(plugin_root))
    assert report.passed, report.error_category
    assert report.checks == ("manifest", "permissions", "entrypoint", "register", "start", "stop")


def test_registered_commands_keep_canonical_save_and_config_on_sdk7(plugin_root):
    asyncio.run(_commands(plugin_root))


async def _commands(plugin_root):
    manifest = load_manifest(plugin_root, yuki_version=__version__)
    registry = ExtensionRegistry()
    loader = PluginLoader()
    loaded = loader.load(plugin_root, manifest)
    context = FakePluginContext(manifest.id)
    person, space, target = str(uuid4()), str(uuid4()), str(uuid4())
    context.messages.current = CurrentMessage(
        message_id="fixture-only",
        sender_user_id="10001",
        scope_type="group",
        group_id="20001",
        platform="qq",
        person_id=person,
        space_id=space,
        text="*签到",
        mentions=(
            {"kind": "member", "person_id": target, "display_name": "玩家"},
            {"kind": "member", "person_id": target, "display_name": "玩家"},
            {"kind": "member", "account_id": str(uuid4()), "display_name": "未知账号"},
            {"kind": "yuki", "presence_id": str(uuid4()), "display_name": "Yuki"},
            {"kind": "all", "display_name": "全体成员"},
        ),
        received_at=datetime(2026, 10, 19, 12, tzinfo=UTC),
    )
    context.people.current_user_id = person
    context.people.people[person] = {"display_name": "玩家"}
    try:
        await loaded.instance.register(registry.registrar(manifest.id, manifest.permissions))
        await loaded.instance.start(context)
        commands = {
            item.registration.metadata.name: item.registration
            for item in registry.list(kind=ExtensionKind.COMMAND)
        }
        assert commands["play"].metadata.permission == PermissionLevel.USER
        assert commands["admin"].metadata.permission == PermissionLevel.SUPERUSER
        play = commands["play"]
        engine = sys.modules[type(loaded.instance).__module__]._engine
        execute_play = engine.execute_play
        selected_people = []

        def record_play(*args, **kwargs):
            selected_people.append(kwargs["mentioned_person_ids"])
            return execute_play(*args, **kwargs)

        engine.execute_play = record_play
        result = await play.handler(play.argument_model(text="签到"))
        assert result.ok and result.data["scope"] == f"group:{space}"
        assert selected_people == [(target,)]
        saved = await context.storage.get("state", f"group:{space}")
        assert person in saved["state"]["players"]
        assert "10001" not in saved["state"]["players"]
        assert await context.storage.get("state", "group:20001") is None
        admin = commands["admin"]
        configured = await admin.handler(admin.argument_model(text="配置 蛋价 7"))
        assert configured.ok
        assert await context.config.get("egg_price", scope_type="group", scope_id=space) == 7
        await loaded.instance.stop()
        await loaded.instance.start(context)
        again = await play.handler(play.argument_model(text="查阅属性"))
        assert again.ok and again.data["revision"] == result.data["revision"]
        assert context.messages.sent == []
    finally:
        await loaded.instance.stop()
        loader.unload(loaded)
