"""K-1 第一部分测试：lx/prompt/template.py 模板渲染。

约定：触发源返回原样模板 + meta["vars"]，由提示词装配统一渲染。
render_template 同时兼容双大括号（新：prompt.* 模板）与单大括号（旧：followup 模板）。
测试用合成文本。
"""
import unittest
from datetime import datetime, timezone, timedelta

from lx.prompt.template import format_current_time, render_template


class RenderDoubleBraceTest(unittest.TestCase):
    def test_double_replaced(self):
        self.assertEqual(
            render_template("现在{{current_time}}，未回复{{unanswered_count}}次。",
                            {"current_time": "今晚", "unanswered_count": 3}),
            "现在今晚，未回复3次。")

    def test_double_with_spaces(self):
        self.assertEqual(render_template("A{{ name }}B", {"name": "x"}), "AxB")

    def test_unknown_double_kept(self):
        self.assertEqual(render_template("A{{nope}}B", {}), "A{{nope}}B")
        self.assertEqual(render_template("A{{ nope }}B", {"other": 1}), "A{{ nope }}B")

    def test_value_strified(self):
        self.assertEqual(render_template("N={{n}}", {"n": 42}), "N=42")
        self.assertEqual(render_template("F={{f}}", {"f": 2.0}), "F=2.0")
        self.assertEqual(render_template("B={{b}}", {"b": None}), "B=None")


class RenderSingleBraceTest(unittest.TestCase):
    def test_single_replaced_when_known(self):
        self.assertEqual(
            render_template("过去 {elapsed}，一直没人说话。", {"elapsed": "30分钟"}),
            "过去 30分钟，一直没人说话。")

    def test_unknown_single_kept(self):
        self.assertEqual(render_template("x {nope} y", {}), "x {nope} y")
        self.assertEqual(render_template("x {nope} y", {"other": 1}), "x {nope} y")

    def test_json_braces_untouched(self):
        tpl = '{"a": 1, "b": "{x}"}'
        self.assertEqual(render_template(tpl, {"x": "v"}), '{"a": 1, "b": "v"}')
        tpl2 = '{"a": 1}'
        self.assertEqual(render_template(tpl2, {"a": "v"}), '{"a": 1}')

    def test_double_leftover_not_eaten_by_single_pass(self):
        # 双大括号里没有的变量：原样保留，不能被单大括号替换啃掉一层
        self.assertEqual(render_template("A{{unknown}}B", {"unknown}B": 1}), "A{{unknown}}B")
        self.assertEqual(render_template("A{{unknown}}B", {}), "A{{unknown}}B")

    def test_replacement_value_with_braces_not_rescanned(self):
        self.assertEqual(render_template("A{n}B", {"n": "{m}"}), "A{m}B")


class RenderRobustnessTest(unittest.TestCase):
    def test_non_string_template_is_empty(self):
        self.assertEqual(render_template(None, {"a": 1}), "")
        self.assertEqual(render_template(123, {"a": 1}), "")
        self.assertEqual(render_template(["x"], {"a": 1}), "")

    def test_never_raises(self):
        class BadStr:
            def __str__(self):
                raise RuntimeError("合成失败")

        for tpl, variables in (
            ("", None),
            ("{a}", None),
            ("{{a}}", "not-a-mapping"),
            ("{a}{{b}}", {"a": BadStr(), "b": BadStr()}),
            ("{{ }}", {}),
            ("{}{}", {}),
        ):
            try:
                render_template(tpl, variables)  # type: ignore[arg-type]
            except Exception as e:  # noqa: BLE001
                self.fail(f"render_template 抛了 {e!r}（{tpl!r}）")

    def test_empty_template(self):
        self.assertEqual(render_template("", {"a": 1}), "")


class FormatCurrentTimeTest(unittest.TestCase):
    def test_card_example(self):
        dt = datetime(2026, 10, 13, 21, 30)
        self.assertEqual(format_current_time(dt), "2026年10月13日 21:30 星期二")

    def test_weekday_names(self):
        # 2026-10-12 是星期一，2026-10-18 是星期日
        self.assertIn("星期一", format_current_time(datetime(2026, 10, 12, 8, 0)))
        self.assertIn("星期日", format_current_time(datetime(2026, 10, 18, 8, 0)))
        self.assertIn("星期三", format_current_time(datetime(2026, 10, 14, 8, 0)))

    def test_aware_datetime(self):
        tz8 = timezone(timedelta(hours=8))
        dt = datetime(2026, 10, 13, 21, 30, tzinfo=tz8)
        self.assertEqual(format_current_time(dt), "2026年10月13日 21:30 星期二")

    def test_never_raises(self):
        try:
            format_current_time(None)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001
            self.fail(f"format_current_time 抛了 {e!r}")


if __name__ == "__main__":
    unittest.main()
