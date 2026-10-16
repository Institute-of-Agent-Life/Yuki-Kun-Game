# 旧 QQ 存档的离线迁移

新版插件以内部 Person / Space 编号保存玩家和场景。旧版 `schema_version=1`
存档以 QQ 账号、群号保存。迁移脚本随独立插件提供，不再占用 Yuki 的 Alembic 迁移链，
也不导入 Yuki 源码；运行只需要 Python 标准库。

迁移前停止 Yuki，确认使用的是实际部署的 SQLite 数据库。先查看计划：

```powershell
python scripts/migrate_canonical_players.py C:/path/to/qq_ai_bot.db
```

此命令以只读方式打开数据库。确认记录后应用：

```powershell
python scripts/migrate_canonical_players.py C:/path/to/qq_ai_bot.db --apply --backup C:/backup/qq_ai_bot-before-kun.db
```

`--apply` 在写入前使用 SQLite backup API 生成一致备份，能包含尚在 WAL 中的已提交数据。
省略 `--backup` 时，在数据库旁生成带 UTC 时间的 `.kun-v1-*.bak` 文件；指定路径必须是
尚不存在的文件。保管好备份，它包含完整 Yuki 数据，而不仅是游戏存档。

输出的 `mode` 区分只读计划与实际应用；`converted_rows` 表示完整转换为 v2 的行数，
`moved_legacy_rows` 表示原封不动保留 v1 JSON、只将可证明的场景归到内部编号的行数。
只读模式下这两个数均是待执行计划。`diagnostics` 仅含原数据库行 ID 与固定问题类别，
不会输出存档正文、QQ 账号、玩家姓名或经济数据。返回码 0 表示计划中没有未解析项，
2 表示仍有未解析原件；实际应用时，其他可迁移记录仍会完成。

## 数据范围

脚本只读取以下既有表：

- `identity_bindings`、`presences`：确认旧 QQ 玩家对应的真实 Person，排除把机器人当玩家。
- `space_bindings`：确认旧 QQ 群对应的真实 Space。
- `plugin_state`：读取 `plugin_id=io.github.yuanyeyoutao.kun-game`、`namespace=state` 的存档。

写入也只涉及这些养鲲存档的 `key`、`value_json` 和私聊 `canonical_person_id`。
其他插件、配置、账本、通知、游戏修订号、KV version 与更新时间均不改动。
脚本不修改 Host 的 `alembic_version`，不重建身份，不调用模型或平台接口。

完整转换覆盖玩家字典、玩家 `person_id`、击杀者引用、阵亡列表、拍卖双方、BOSS 伤害排行、
小游戏参与者与奖励、擂台冠军。金币、道具、鲲、战斗结果与所有原业务数值保留。
完成后的存档仍由正常引擎校验；重复运行跳过已转换的 v2 存档。

## 无法直接转换的原件

脚本使用旧 QQ 的真实 Binding / SpaceBinding，不按今天同号的 Telegram 账号推断归属。
一个旧账号存在多个历史 Person 时，存档缺少逐玩家归属时间，脚本不能选择其中一人。
两个独立玩家记录映射到同一个 Person 时，也不能自动合并其经济数据。

已确认场景、但玩家归属未知或冲突的存档，会将完整原 v1 JSON 放在该场景的内部 key 下。
插件会明确报告旧身份未解析，不能将它当成新游戏建立空档。同一 Space 对应多个独立旧
场景时，保留所有原 JSON，其中一份原 v1 记录占据内部 key，其他原记录保留原 key；
不选择、合并或丢弃任何经济记录。

场景本身无法确认、私聊已有 owner 与原 Binding 不一致，或内部 key 已有独立存档时，
原行保持不变并列出诊断。先依据实际存档和原身份材料处理这些具体记录，再重跑脚本；
不要通过清空存档、猜并账号或改一份版本号绕过真实归属问题。

备份用于保留迁移前原件。若升级后已有新消息、回执或游戏操作，不得用整个旧数据库
覆盖新事实；应保留当前数据库，对已确认的问题做定向修复。只有仍处于停机迁移窗口、
且没有后续新事实时，才适合恢复完整的一致备份。

## 本地验证范围

`tests/test_canonical_migration.py` 使用独立真实 SQLite 表与纯游戏引擎，覆盖完整迁移、
未知玩家、历史换绑、玩家碰撞、多旧场景、只读计划、一致备份、重复执行、其他插件不变，
以及同数字 QQ / Telegram 的独立存档选择。测试不依赖 Yuki Host 源码，也不声称验证了
实际 Bot 加载、线上平台发送或真实生产数据。
