"""UI-1b：ConfigService（lx/webui/service.py）测试。先写测试再实现。

覆盖（任务卡）：get_config 默认/存值/坏类型回退+warning/未知键忽略；revision 稳定且随内容变；
save 成功（写入、别的键保留、旧分节不动、save/reload 各调一次）、整体拒绝、冲突、save 抛异常回滚、
reload 抛异常降级、同键多次取最后一个、白名单可改、覆盖表有错拒绝/无错保存；
check_overrides 好坏各一；explain 三种 source、未知 scope、群聊 id；
about backups 排序与上限。数据全合成。
"""
import json
import os
import unittest

from lx.config import session_rules as sr
from lx.config import spec
from lx.contracts import Scope, SessionKey
from lx.webui.service import ConfigService


def _defaults(scope):
    return spec.default_tree(scope)


class _Harness:
    """内存 conf + 可编程的 save/reload 回调。"""

    def __init__(self, conf=None, save_exc=None, reload_exc=None, reload_notes=None):
        self.conf = {} if conf is None else conf
        self.save_calls = 0
        self.reload_calls = []
        self.save_exc = save_exc
        self.reload_exc = reload_exc
        self.reload_notes = ["加载提示A"] if reload_notes is None else reload_notes

    def save(self):
        self.save_calls += 1
        if self.save_exc is not None:
            raise self.save_exc

    def reload(self, conf):
        self.reload_calls.append(conf)
        if self.reload_exc is not None:
            raise self.reload_exc
        return list(self.reload_notes)

    def svc(self, **kw):
        kw.setdefault("plugin_version", "v9.9.9-test.1")
        return ConfigService(self.conf, self.save, self.reload, **kw)


def _rev_of(svc):
    return svc.get_config()["revision"]


class GetConfigTest(unittest.TestCase):
    def test_defaults_when_conf_empty(self):
        h = _Harness()
        got = h.svc().get_config()
        self.assertEqual(got["private"], _defaults(Scope.PRIVATE))
        self.assertEqual(got["group"], _defaults(Scope.GROUP))
        self.assertEqual(got["session_overrides"], "")
        self.assertEqual(got["warnings"], [])
        self.assertEqual(len(got["revision"]), 12)
        json.dumps(got)

    def test_stored_values_override_defaults(self):
        h = _Harness({"private": {"idle": {"base_minutes": 45}}})
        got = h.svc().get_config()
        self.assertEqual(got["private"]["idle"]["base_minutes"], 45)
        # 没写的项还是默认
        self.assertEqual(got["private"]["idle"],
                         {**_defaults(Scope.PRIVATE)["idle"], "base_minutes": 45})

    def test_bad_type_falls_back_with_warning(self):
        h = _Harness({"private": {"idle": {"base_minutes": "很多"}}})
        got = h.svc().get_config()
        self.assertEqual(got["private"]["idle"]["base_minutes"],
                         spec.field("idle.base_minutes").default(Scope.PRIVATE))
        self.assertEqual(len(got["warnings"]), 1)
        self.assertIn("基础间隔", got["warnings"][0])  # 带项标题
        json.dumps(got)

    def test_unknown_keys_ignored_silently(self):
        h = _Harness({"private": {"根本没有这个块": {"x": 1}, "idle": {"查无此项": 2}}})
        got = h.svc().get_config()
        self.assertEqual(got["private"], _defaults(Scope.PRIVATE))
        self.assertEqual(got["warnings"], [])

    def test_non_string_overrides_becomes_empty(self):
        h = _Harness({"session_overrides": {"不是": "字符串"}})
        got = h.svc().get_config()
        self.assertEqual(got["session_overrides"], "")

    def test_startup_warnings_prepended(self):
        h = _Harness()
        got = h.svc(startup_warnings=["启动提示1"]).get_config()
        self.assertEqual(got["warnings"], ["启动提示1"])

    def test_revision_stable_and_content_sensitive(self):
        h = _Harness()
        svc = h.svc()
        r1 = svc.get_config()["revision"]
        r2 = svc.get_config()["revision"]
        self.assertEqual(r1, r2)
        h.conf["private"] = {"idle": {"base_minutes": 44}}
        r3 = svc.get_config()["revision"]
        self.assertNotEqual(r1, r3)
        self.assertEqual(len(r3), 12)


class SaveConfigTest(unittest.TestCase):
    def _save(self, h, changes, **body_kw):
        svc = h.svc()
        body = {"revision": _rev_of(svc), "changes": changes}
        body.update(body_kw)
        return svc.save_config(body)

    def test_save_success_writes_and_calls_back(self):
        h = _Harness({"private": {"idle": {"base_minutes": 30, "enabled": True}}})
        svc = h.svc()
        rev = _rev_of(svc)
        data = svc.save_config({"revision": rev, "changes": [
            {"scope": "private", "path": "idle.base_minutes", "value": 45}]})
        self.assertTrue(data["saved"])
        self.assertEqual(data["applied"], [{"scope": "private", "path": "idle.base_minutes"}])
        self.assertTrue(data["reloaded"])
        self.assertEqual(h.conf["private"]["idle"]["base_minutes"], 45)
        self.assertTrue(h.conf["private"]["idle"]["enabled"])  # 别的键保留
        self.assertEqual(h.save_calls, 1)
        self.assertEqual(len(h.reload_calls), 1)
        self.assertIs(h.reload_calls[0], h.conf)
        json.dumps(data)

    def test_save_keeps_old_sections(self):
        h = _Harness({"friend_settings": {"enable": True}, "schema_version": 2})
        data = self._save(h, [{"scope": "group", "path": "idle.base_minutes", "value": 50}])
        self.assertTrue(data["saved"])
        self.assertEqual(h.conf["friend_settings"], {"enable": True})
        self.assertEqual(h.conf["schema_version"], 2)

    def test_all_or_nothing_on_validation_error(self):
        h = _Harness()
        data = self._save(h, [
            {"scope": "private", "path": "idle.base_minutes", "value": 45},
            {"scope": "private", "path": "idle.base_minutes", "value": 0}])  # 小于 lo=1
        self.assertFalse(data["saved"])
        self.assertFalse(data.get("conflict", False))
        self.assertTrue(data["errors"])
        self.assertNotIn("private", h.conf)  # 一项都不写
        self.assertEqual(h.save_calls, 0)
        self.assertEqual(h.reload_calls, [])

    def test_unknown_path_rejected(self):
        h = _Harness()
        data = self._save(h, [{"scope": "private", "path": "idle.查无此项", "value": 1}])
        self.assertFalse(data["saved"])
        self.assertIn("idle.查无此项", data["errors"][0]["path"])

    def test_group_only_item_in_private_rejected(self):
        h = _Harness()
        data = self._save(h, [{"scope": "private", "path": "trigger.keyword_enabled", "value": True}])
        self.assertFalse(data["saved"])

    def test_revision_conflict_writes_nothing(self):
        h = _Harness({"private": {"idle": {"base_minutes": 30}}})
        svc = h.svc()
        data = svc.save_config({"revision": "deadbeef0000", "changes": [
            {"scope": "private", "path": "idle.base_minutes", "value": 45}]})
        self.assertFalse(data["saved"])
        self.assertTrue(data["conflict"])
        self.assertEqual(h.conf["private"]["idle"]["base_minutes"], 30)
        self.assertEqual(h.save_calls, 0)

    def test_revision_conflict_checked_before_item_validation(self):
        # 页面数据已过期（revision 对不上）时，不逐项报错，只报冲突、要求刷新。
        h = _Harness({"private": {"idle": {"base_minutes": 30}}})
        svc = h.svc()
        data = svc.save_config({"revision": "deadbeef0000", "changes": [
            {"scope": "private", "path": "no.such_item", "value": 1}]})
        self.assertFalse(data["saved"])
        self.assertTrue(data["conflict"])
        self.assertEqual([e["path"] for e in data["errors"]], ["revision"])

    def test_save_exception_rolls_back(self):
        before = {"private": {"idle": {"base_minutes": 30}}}
        h = _Harness(dict(json.loads(json.dumps(before))), save_exc=OSError("磁盘满了"))
        data = self._save(h, [{"scope": "private", "path": "idle.base_minutes", "value": 45}])
        self.assertFalse(data["saved"])
        self.assertEqual(h.conf, before)
        self.assertIn("保存到磁盘失败", data["errors"][0]["message"])

    def test_reload_exception_degrades(self):
        h = _Harness(reload_exc=RuntimeError("热加载炸了"))
        data = self._save(h, [{"scope": "private", "path": "idle.base_minutes", "value": 45}])
        self.assertTrue(data["saved"])
        self.assertFalse(data["reloaded"])
        self.assertIn("reload_note", data)
        # 保存已落盘：内存里是新值
        self.assertEqual(h.conf["private"]["idle"]["base_minutes"], 45)

    def test_duplicate_path_last_wins(self):
        h = _Harness()
        data = self._save(h, [
            {"scope": "private", "path": "idle.base_minutes", "value": 30},
            {"scope": "private", "path": "idle.base_minutes", "value": 40}])
        self.assertTrue(data["saved"])
        self.assertEqual(h.conf["private"]["idle"]["base_minutes"], 40)
        self.assertEqual(data["applied"], [{"scope": "private", "path": "idle.base_minutes"}])

    def test_whitelist_editable(self):
        h = _Harness()
        data = self._save(h, [
            {"scope": "group", "path": "whitelist.enabled", "value": False},
            {"scope": "group", "path": "whitelist.ids", "value": ["900100200"]}])
        self.assertTrue(data["saved"])
        self.assertFalse(h.conf["group"]["whitelist"]["enabled"])
        self.assertEqual(h.conf["group"]["whitelist"]["ids"], ["900100200"])

    def test_bad_overrides_rejected(self):
        h = _Harness()
        svc = h.svc()
        data = svc.save_config({"revision": _rev_of(svc), "changes": [],
                                "session_overrides": "{bad json"})
        self.assertFalse(data["saved"])
        self.assertTrue(all(e["path"] == "session_overrides" for e in data["errors"]))
        self.assertNotIn("session_overrides", h.conf)

    def test_good_overrides_saved(self):
        h = _Harness()
        text = '{"group:900100200": {"idle": {"base_minutes": 7}}}'
        data = self._save(h, [], session_overrides=text)
        self.assertTrue(data["saved"])
        self.assertEqual(h.conf["session_overrides"], text)

    def test_body_wrong_types_give_errors_not_raise(self):
        h = _Harness()
        svc = h.svc()
        for body in (None, "字符串", {"revision": "x"}, {"revision": "x", "changes": "不是列表"},
                     {"revision": _rev_of(svc), "changes": [{"scope": "火星"}]}):
            data = svc.save_config(body)
            self.assertIn("errors", data, body)
            json.dumps(data)


class SessionRulesEntriesTest(unittest.TestCase):
    """SR-UI §2：get_config 带条目列表、revision 跟着变、save 带 / 不带 / []、
    整体拒绝、丢默认值进 warnings、YAML 不被表单保存动到、生效值对。"""

    @staticmethod
    def _entry(scope, session, block, items):
        return {"scope": scope, "session": session, "block": block, "items": items}

    @staticmethod
    def _row(scope, block, session, **values):
        row = {"__template_key": sr.template_key(Scope(scope), block), sr.SESSION_ITEM: session}
        for f in spec.block(block).fields_for(Scope(scope)):
            row[f.name] = f.default(Scope(scope))
        row.update(values)
        return row

    def test_get_config_empty_form(self):
        got = _Harness().svc().get_config()
        self.assertEqual(got["session_rules"], [])
        json.dumps(got)

    def test_get_config_returns_entries(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        h = _Harness({"session_rules": [self._row("group", "context", " 900100200 ", max_messages=d + 5)]})
        got = h.svc().get_config()
        self.assertEqual(got["session_rules"],
                         [self._entry("group", "900100200", "context", {"max_messages": d + 5})])
        self.assertEqual(got["warnings"], [])
        json.dumps(got)

    def test_get_config_reports_bad_rows_as_warnings(self):
        h = _Harness({"session_rules": ["坏行", {"__template_key": "nope.nope", "session": "1"}]})
        got = h.svc().get_config()
        self.assertEqual(got["session_rules"], [])
        self.assertEqual(len(got["warnings"]), 2, got["warnings"])
        self.assertTrue(all("会话覆盖表单第 " in w for w in got["warnings"]))

    def test_revision_follows_the_form(self):
        svc = _Harness().svc()
        r1 = svc.get_config()["revision"]
        svc = _Harness({"session_rules": [self._row("group", "context", "1",
                                                    max_messages=spec.field("context.max_messages").default(Scope.GROUP) + 1)]}).svc()
        self.assertNotEqual(r1, svc.get_config()["revision"])
        # 内容一样、写法不一样（项顺序 / 条目顺序 / session 前后空白）→ revision 一样
        rows_a = [self._row("group", "context", "1", max_messages=201, inject_chars=1000)]
        rows_b = [self._row("group", "context", " 1 ", inject_chars=1000, max_messages=201)]
        self.assertEqual(_Harness({"session_rules": rows_a}).svc().get_config()["revision"],
                         _Harness({"session_rules": rows_b}).svc().get_config()["revision"])

    def _save(self, h, changes=None, **body_kw):
        svc = h.svc()
        body = {"revision": _rev_of(svc), "changes": changes or []}
        body.update(body_kw)
        return svc, svc.save_config(body)

    def test_save_writes_sparse_rows(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        h = _Harness()
        _svc, data = self._save(h, session_rules=[
            self._entry("group", "900100200", "context", {"max_messages": d + 5})])
        self.assertTrue(data["saved"], data)
        self.assertEqual(h.conf["session_rules"],
                         [{"__template_key": "group.context", "session": "900100200",
                           "max_messages": d + 5}])
        self.assertIn({"scope": None, "path": "session_rules"}, data["applied"])
        self.assertEqual(h.save_calls, 1)
        self.assertEqual(len(h.reload_calls), 1)
        self.assertEqual(data["config"]["session_rules"],
                         [self._entry("group", "900100200", "context", {"max_messages": d + 5})])
        json.dumps(data)

    def test_absent_or_none_leaves_form_alone(self):
        rows = [self._row("group", "context", "1",
                          max_messages=spec.field("context.max_messages").default(Scope.GROUP) + 1)]
        for body_kw in ({}, {"session_rules": None}):
            h = _Harness({"session_rules": list(rows)})
            _svc, data = self._save(h, **body_kw)
            self.assertTrue(data["saved"], data)
            self.assertEqual(h.conf["session_rules"], rows)
            self.assertNotIn({"scope": None, "path": "session_rules"}, data["applied"])

    def test_empty_list_clears_the_form(self):
        rows = [self._row("group", "context", "1",
                          max_messages=spec.field("context.max_messages").default(Scope.GROUP) + 1)]
        h = _Harness({"session_rules": rows})
        _svc, data = self._save(h, session_rules=[])
        self.assertTrue(data["saved"], data)
        self.assertEqual(h.conf["session_rules"], [])
        self.assertIn({"scope": None, "path": "session_rules"}, data["applied"])

    def test_validation_error_rejects_everything(self):
        h = _Harness({"private": {"idle": {"base_minutes": 30}}})
        before = json.loads(json.dumps(h.conf))
        _svc, data = self._save(h, [{"scope": "private", "path": "idle.base_minutes", "value": 45}],
                                session_rules=[self._entry("group", "1", "context", {"max_messages": 0})])
        self.assertFalse(data["saved"])
        self.assertTrue(data["errors"])
        self.assertTrue(all(e["path"] == "session_rules" for e in data["errors"]), data["errors"])
        self.assertIn("第 1 条", data["errors"][0]["message"])
        self.assertEqual(h.conf, before)   # 表单错时别的改动也不写
        self.assertEqual(h.save_calls, 0)
        self.assertEqual(h.reload_calls, [])

    def test_error_message_names_session_and_block(self):
        h = _Harness()
        _svc, data = self._save(h, session_rules=[
            self._entry("group", "1", "context", {"max_messages": 5}),
            self._entry("group", "900100200", "context", {"max_messages": 0})])
        self.assertFalse(data["saved"])
        msg = data["errors"][0]["message"]
        self.assertIn("第 2 条", msg)
        self.assertIn("900100200", msg)
        self.assertIn(spec.block("context").description, msg)
        self.assertIn(spec.field("context.max_messages").description, msg)

    def test_not_a_list_rejected(self):
        h = _Harness()
        _svc, data = self._save(h, session_rules="不是列表")
        self.assertFalse(data["saved"])
        self.assertEqual(data["errors"][0]["path"], "session_rules")
        self.assertNotIn("session_rules", h.conf)

    def test_default_values_dropped_with_warning(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        h = _Harness()
        _svc, data = self._save(h, session_rules=[
            self._entry("private", "10001", "base", {"take_over": False}),
            self._entry("group", "900100200", "context", {"max_messages": d, "inject_chars": d + 7})])
        self.assertTrue(data["saved"], data)
        # take_over 的私聊默认就是 False → 整条没有了；max_messages 被丢，inject_chars 留下
        self.assertEqual(h.conf["session_rules"],
                         [{"__template_key": "group.context", "session": "900100200",
                           "inject_chars": d + 7}])
        self.assertEqual(len(data["config"]["session_rules"]), 1)
        # 加载提示（"加载提示A"）也在 warnings 里，这里只数"被丢掉"的那两句
        warns = [w for w in data["config"]["warnings"] if "和出厂默认一样" in w]
        self.assertEqual(len(warns), 2, data["config"]["warnings"])
        self.assertTrue(any(spec.field("context.max_messages").description in w for w in warns), warns)
        self.assertTrue(any("YAML" in w for w in warns), warns)

    def test_save_does_not_touch_yaml_overrides(self):
        text = 'group:900100200:\n  idle:\n    base_minutes: 12\n'
        h = _Harness({"session_overrides": text})
        d = spec.field("context.max_messages").default(Scope.GROUP)
        _svc, data = self._save(h, session_rules=[
            self._entry("group", "900100200", "context", {"max_messages": d + 1})])
        self.assertTrue(data["saved"], data)
        self.assertEqual(h.conf["session_overrides"], text)
        self.assertNotIn("session_overrides", [a["path"] for a in data["applied"]])

    def test_save_both_yaml_and_form(self):
        h = _Harness()
        d = spec.field("context.max_messages").default(Scope.GROUP)
        _svc, data = self._save(
            h, session_overrides="private:2:\n  base:\n    take_over: true\n",
            session_rules=[self._entry("group", "900100200", "context", {"max_messages": d + 1})])
        self.assertTrue(data["saved"], data)
        self.assertEqual(h.conf["session_overrides"], "private:2:\n  base:\n    take_over: true\n")
        self.assertEqual(sorted(a["path"] for a in data["applied"]),
                         ["session_overrides", "session_rules"])

    def test_effective_values_after_save(self):
        h = _Harness()
        svc, data = self._save(h, session_rules=[
            self._entry("group", "900100200", "context", {"max_messages": 80})])
        self.assertTrue(data["saved"], data)
        r = svc._temp_resolver()
        self.assertEqual(r.warnings, [])
        self.assertEqual(r.resolve(SessionKey(Scope.GROUP, "900100200", "")).get("context.max_messages"), 80)
        self.assertEqual(
            r.resolve(SessionKey(Scope.GROUP, "555", "")).get("context.max_messages"),
            spec.field("context.max_messages").default(Scope.GROUP))

    def test_form_beats_yaml_for_the_same_item(self):
        text = 'group:900100200:\n  context:\n    max_messages: 33\n'
        h = _Harness({"session_overrides": text})
        svc, data = self._save(h, session_rules=[
            self._entry("group", "900100200", "context", {"max_messages": 44})])
        self.assertTrue(data["saved"], data)
        r = svc._temp_resolver()
        self.assertEqual(r.resolve(SessionKey(Scope.GROUP, "900100200", "")).get("context.max_messages"), 44)

    # ---------------------------------------------------------------- SR-UI-2 会话号规整
    def test_get_config_returns_the_bare_number(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        h = _Harness({"session_rules": [
            self._row("group", "context", "napcat:GroupMessage:1919810", max_messages=d + 1)]})
        got = h.svc().get_config()
        self.assertEqual(got["session_rules"],
                         [self._entry("group", "1919810", "context", {"max_messages": d + 1})])
        self.assertEqual(got["warnings"], [])

    def test_three_writings_of_one_session_save_as_one_row(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        c = spec.field("context.inject_chars").default(Scope.GROUP)
        h = _Harness({"session_rules": [
            self._row("group", "context", "1919810", max_messages=d + 1)]})
        _svc, data = self._save(h, session_rules=[
            self._entry("group", "1919810", "context", {"max_messages": d + 1}),
            self._entry("group", "group:1919810", "context", {"inject_chars": c + 2}),
            self._entry("group", "napcat:GroupMessage:1919810", "context", {"max_messages": d + 1})])
        self.assertTrue(data["saved"], data)
        self.assertEqual(h.conf["session_rules"],
                         [{"__template_key": "group.context", "session": "1919810",
                           "max_messages": d + 1, "inject_chars": c + 2}])
        self.assertEqual(len(h.conf["session_rules"]), 1)
        self.assertEqual(data["config"]["session_rules"][0]["session"], "1919810")

    def test_scope_mismatch_is_rejected_and_conf_is_untouched(self):
        text = 'private:10001:\n  base:\n    take_over: true\n'
        h = _Harness({"session_rules": [], "session_overrides": text})
        before = json.dumps(h.conf, sort_keys=True, ensure_ascii=False)
        _svc, data = self._save(h, session_rules=[
            self._entry("private", "napcat:GroupMessage:1919810", "context", {"max_messages": 80})])
        self.assertFalse(data["saved"])
        self.assertEqual([e["path"] for e in data["errors"]], ["session_rules"])
        self.assertIn("第 1 条", data["errors"][0]["message"])
        self.assertIn("群聊", data["errors"][0]["message"])
        self.assertIn("不能放在私聊里", data["errors"][0]["message"])
        self.assertEqual(json.dumps(h.conf, sort_keys=True, ensure_ascii=False), before)
        self.assertEqual(h.save_calls, 0)
        self.assertEqual(h.reload_calls, [])

    def test_revision_ignores_the_writing_style_of_the_number(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        a = _Harness({"session_rules": [self._row("group", "context", "1919810", max_messages=d + 1)]})
        b = _Harness({"session_rules": [
            self._row("group", "context", "napcat:GroupMessage:1919810", max_messages=d + 1)]})
        self.assertEqual(a.svc().get_config()["revision"], b.svc().get_config()["revision"])

    def test_form_beats_yaml_when_the_session_is_written_differently(self):
        text = 'napcat:GroupMessage:1919810:\n  context:\n    max_messages: 33\n'
        h = _Harness({"session_overrides": text})
        svc, data = self._save(h, session_rules=[
            self._entry("group", "1919810", "context", {"max_messages": 44})])
        self.assertTrue(data["saved"], data)
        self.assertEqual(h.conf["session_rules"][0]["session"], "1919810")
        r = svc._temp_resolver()
        # 表单行是 group:1919810，YAML 那张表是完整标识：两张表键不同，resolver 会多一句提示（允许）
        self.assertTrue(any("写了不止一次" in w for w in r.warnings), r.warnings)
        self.assertEqual(r.resolve(SessionKey(Scope.GROUP, "1919810", "")).get("context.max_messages"), 44)



    def test_good_text(self):
        h = _Harness()
        got = h.svc().check_overrides('{"group:900100200": {"idle": {"base_minutes": 7}}}')
        self.assertTrue(got["ok"])
        self.assertEqual(got["errors"], [])
        self.assertEqual(got["sessions"],
                         [{"scope": "group", "id": "900100200", "keys": ["idle.base_minutes"]}])
        json.dumps(got)

    def test_bad_text(self):
        h = _Harness()
        got = h.svc().check_overrides("{bad json")
        self.assertFalse(got["ok"])
        self.assertTrue(got["errors"])
        json.dumps(got)


class ExplainTest(unittest.TestCase):
    def test_three_sources(self):
        h = _Harness({
            "private": {"idle": {"base_minutes": 45}, "whitelist": {"enabled": False}},
            "session_overrides": '{"private:10001": {"context": {"max_messages": 80}}}',
        })
        got = h.svc().explain("private", "10001")
        values = got["values"]
        default_mm = spec.field("context.max_messages").default(Scope.PRIVATE)
        self.assertNotEqual(80, default_mm)  # 前提：覆盖值确实不是默认
        self.assertEqual(values["context.max_messages"], {"value": 80, "source": "override"})
        self.assertEqual(values["idle.base_minutes"], {"value": 45, "source": "global"})
        self.assertEqual(values["idle.enabled"]["source"], "default")
        self.assertEqual(got["whitelist"], {"enabled": False, "listed": False})
        self.assertTrue(got["enabled"])
        self.assertFalse(got["takes_over"])
        json.dumps(got)

    def test_unknown_scope_and_empty_ident(self):
        h = _Harness()
        self.assertIn("error", h.svc().explain("火星", "10001"))
        self.assertIn("error", h.svc().explain("private", ""))
        self.assertIn("error", h.svc().explain("private", None))

    def test_group_isolated_ident(self):
        h = _Harness({"session_overrides": '{"group:900100200": {"idle": {"base_minutes": 7}}}'})
        got = h.svc().explain("group", "111111_900100200")
        self.assertEqual(got["values"]["idle.base_minutes"], {"value": 7, "source": "override"})


class AboutTest(unittest.TestCase):
    def test_backups_sorted_and_capped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for i in range(22):
                p = os.path.join(d, f"v0_202610{i:02d}-120000.json")
                with open(p, "w", encoding="utf-8") as f:
                    f.write("{}")
                os.utime(p, (1700000000 + i, 1700000000 + i))
            h = _Harness()
            got = h.svc(backups_dir=d).about()
            self.assertEqual(len(got["backups"]), 20)
            mtimes = [b["mtime"] for b in got["backups"]]
            self.assertEqual(mtimes, sorted(mtimes, reverse=True))
            self.assertEqual(got["backups"][0]["name"], "v0_20261021-120000.json")
            self.assertIn("size", got["backups"][0])
            json.dumps(got)

    def test_missing_backups_dir_is_empty(self):
        h = _Harness()
        got = h.svc(backups_dir=os.path.join("根本", "不存在", "目录")).about()
        self.assertEqual(got["backups"], [])

    def test_legacy_sections_present(self):
        self.assertTrue(_Harness({"friend_settings": {}}).svc().about()["legacy_sections_present"])
        self.assertFalse(_Harness({}).svc().about()["legacy_sections_present"])
        got = _Harness().svc().about()
        self.assertEqual(got["plugin_version"], "v9.9.9-test.1")
        self.assertEqual(got["schema_version"], spec.SCHEMA_VERSION)
        self.assertEqual(got["startup_warnings"], [])
        json.dumps(got)


class ReviewFixesTest(unittest.TestCase):
    """UI-7 审查后的修整：#2 白名单格式、#3 现算警告、#15 数值上限、#16 null 覆盖表。"""

    def _save(self, h, changes=None, **extra):
        svc = h.svc(**extra.pop("svc_kw", {}))
        body = {"revision": _rev_of(svc), "changes": changes or []}
        body.update(extra)
        return svc, svc.save_config(body)

    def test_whitelist_ids_with_separators_rejected(self):
        for bad in (["123, 456"], ["123，456"], ["123 456"], ["123;456"], ["a、b"]):
            h = _Harness({})
            _svc, data = self._save(h, [{"scope": "group", "path": "whitelist.ids", "value": bad}])
            self.assertFalse(data["saved"], bad)
            self.assertEqual(data["errors"][0]["path"], "whitelist.ids")
            self.assertEqual(h.save_calls, 0)
            self.assertNotIn("group", h.conf)

    def test_whitelist_ids_with_scope_prefix_rejected(self):
        h = _Harness({})
        _svc, data = self._save(h, [{"scope": "private", "path": "whitelist.ids",
                                     "value": ["group:123"]}])
        self.assertFalse(data["saved"])
        self.assertIn("前缀", data["errors"][0]["message"])

    def test_whitelist_ids_good_forms_saved(self):
        h = _Harness({})
        good = ["900100200", "aiocqhttp:GroupMessage:900100200", "123_900100200"]
        _svc, data = self._save(h, [{"scope": "group", "path": "whitelist.ids", "value": good}])
        self.assertTrue(data["saved"], data)
        self.assertEqual(h.conf["group"]["whitelist"]["ids"], good)

    def test_live_warnings_used_instead_of_startup_snapshot(self):
        state = {"w": ["现在的警告"]}
        h = _Harness({})
        svc = h.svc(startup_warnings=["启动时的旧警告"], live_warnings=lambda: list(state["w"]))
        self.assertEqual(svc.get_config()["warnings"], ["现在的警告"])
        state["w"] = []
        self.assertEqual(svc.get_config()["warnings"], [])
        # 关于页的"启动提示"仍是启动快照
        self.assertEqual(svc.about()["startup_warnings"], ["启动时的旧警告"])

    def test_live_warnings_failure_falls_back_to_startup(self):
        def boom():
            raise RuntimeError("合成")
        svc = _Harness({}).svc(startup_warnings=["启动提示"], live_warnings=boom)
        self.assertEqual(svc.get_config()["warnings"], ["启动提示"])

    def test_huge_numbers_rejected_even_without_upper_bound(self):
        h = _Harness({})
        for v in (10 ** 30, 1e30, 10 ** 400):
            _svc, data = self._save(h, [{"scope": "private", "path": "trigger.wait_seconds",
                                         "value": v}])
            self.assertFalse(data["saved"], v)
            self.assertIn("太大", data["errors"][0]["message"])

    def test_session_overrides_null_means_absent_not_clear(self):
        text = "group:900100200:\n  idle:\n    enabled: true\n"
        h = _Harness({"session_overrides": text})
        _svc, data = self._save(h, [], session_overrides=None)
        self.assertTrue(data["saved"], data)
        self.assertEqual(h.conf["session_overrides"], text)  # None 不清空
        _svc, data = self._save(h, [], session_overrides="")
        self.assertTrue(data["saved"], data)
        self.assertEqual(h.conf["session_overrides"], "")  # 要清空就传空字符串


if __name__ == "__main__":
    unittest.main()
