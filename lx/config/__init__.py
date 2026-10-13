"""配置：规格表（唯一来源）、四层解析器、旧配置迁移、覆盖表解析。

- spec.py       配置规格表：所有新配置项（键、类型、私聊 / 群聊默认、大白话 hint）
- defaults.py   默认值（从规格表取）
- resolver.py   ConfigResolver / ResolvedConfig（全局私聊 / 全局群聊 / 分私聊 / 分群聊，稀疏逐字段覆盖）
- migrate.py    旧配置 → 新配置的纯函数迁移 + 旧键去向表
- overrides.py  parse_overrides_text：把 JSON / YAML 文本解析成覆盖表 dict
- legacy_schema.json  旧 _conf_schema.json 的原样副本（迁移与 schema 生成要用）
- generated/    生成物（_conf_schema.v2.json），由 tools/schema/gen_schema.py 生成
"""
