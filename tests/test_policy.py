"""运行策略（报告开关 / 时间 / 巡检间隔）与 cron 生成的测试。

这层的意义：客户会在网页上随手关掉「午报」、把巡检改成每 30 分钟。
如果开关只影响 UI 而没影响真正的定时任务，那就是假配置——所以这里逐条锁定
「配置 → 计划 → cron 行」的映射关系。
"""
import unittest

from cs2analyst.config import AppConfig
from cs2analyst.policy import (
    alerts_interval_minutes, clamp_interval, describe_plan, next_run_hint,
    parse_hhmm, report, report_enabled, should_run,
)
from tools.install_cron import build_lines


def cfg_with(reports=None, alerts=None, schedule=None) -> AppConfig:
    cfg = AppConfig(
        reports=reports or {},
        alerts=alerts if alerts is not None else {},
        schedule=schedule or {},
    )
    return cfg


class TestParseTime(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_hhmm("17:30", 0, 0), (17, 30))
        self.assertEqual(parse_hhmm("9:05", 0, 0), (9, 5))
        self.assertEqual(parse_hhmm("12：10", 0, 0), (12, 10))   # 全角冒号

    def test_invalid_falls_back(self):
        for bad in ("", "abc", "25:00", "12:99", None, "12"):
            self.assertEqual(parse_hhmm(bad, 7, 45), (7, 45), f"{bad!r} 应该回退默认值")


class TestReportConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = cfg_with()
        daily = report(cfg, "daily")
        self.assertTrue(daily["enabled"])
        self.assertEqual((daily["hour"], daily["minute"]), (17, 30))
        weekly = report(cfg, "weekly")
        self.assertEqual(weekly["weekday"], 6)
        self.assertEqual((weekly["hour"], weekly["minute"]), (20, 0))

    def test_flat_keys(self):
        cfg = cfg_with(reports={"noon_enabled": False, "noon_time": "11:45"})
        noon = report(cfg, "noon")
        self.assertFalse(noon["enabled"])
        self.assertEqual((noon["hour"], noon["minute"]), (11, 45))

    def test_nested_legacy_wins(self):
        """老配置里的嵌套写法仍然生效（不强行迁移用户的文件）。"""
        cfg = cfg_with(reports={"daily": {"enabled": False, "hour": 21, "minute": 5}})
        daily = report(cfg, "daily")
        self.assertFalse(daily["enabled"])
        self.assertEqual((daily["hour"], daily["minute"]), (21, 5))

    def test_daily_time_falls_back_to_schedule(self):
        cfg = cfg_with(schedule={"hour": 8, "minute": 15})
        daily = report(cfg, "daily")
        self.assertEqual((daily["hour"], daily["minute"]), (8, 15))

    def test_weekly_day_wraps(self):
        cfg = cfg_with(reports={"weekly_day": 9})
        self.assertEqual(report(cfg, "weekly")["weekday"], 2)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            report(cfg_with(), "monthly")


class TestInterval(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(clamp_interval(1), 15)       # 低于下限 → 15
        self.assertEqual(clamp_interval(5000), 1440)  # 高于上限 → 一天
        self.assertEqual(clamp_interval("abc"), 60)
        self.assertEqual(clamp_interval(30), 30)

    def test_from_config(self):
        self.assertEqual(alerts_interval_minutes(cfg_with(alerts={"interval_minutes": 30})), 30)
        self.assertEqual(alerts_interval_minutes(cfg_with()), 60)


class TestShouldRun(unittest.TestCase):
    def test_disabled_report_skips(self):
        cfg = cfg_with(reports={"noon_enabled": False})
        ok, reason = should_run(cfg, "noon")
        self.assertFalse(ok)
        self.assertIn("关闭", reason)
        self.assertTrue(should_run(cfg, "daily")[0])

    def test_disabled_alerts_skips(self):
        cfg = cfg_with(alerts={"enabled": False})
        ok, reason = should_run(cfg, "alerts")
        self.assertFalse(ok)
        self.assertIn("关闭", reason)

    def test_unknown_task(self):
        ok, reason = should_run(cfg_with(), "monthly")
        self.assertFalse(ok)
        self.assertIn("未知", reason)


class TestDescribe(unittest.TestCase):
    def test_lists_all_four(self):
        lines = describe_plan(cfg_with())
        joined = " | ".join(lines)
        for word in ("日报", "午报", "周报", "盘中巡检"):
            self.assertIn(word, joined)
        self.assertIn("每 60 分钟", joined)

    def test_shows_off(self):
        cfg = cfg_with(reports={"weekly_enabled": False}, alerts={"enabled": False})
        joined = " | ".join(describe_plan(cfg))
        self.assertIn("周报：关", joined)
        self.assertIn("盘中巡检：关", joined)

    def test_next_run_hint(self):
        from datetime import datetime

        cfg = cfg_with(reports={"daily_time": "23:59"})
        hint = next_run_hint(cfg, "daily", datetime(2026, 10, 29, 8, 0))
        self.assertIn("今天", hint)
        hint2 = next_run_hint(cfg, "daily", datetime(2026, 10, 29, 23, 59))
        self.assertIn("明天", hint2)
        self.assertEqual(next_run_hint(cfg_with(reports={"daily_enabled": False}), "daily"), "已关闭")


class TestCronLines(unittest.TestCase):
    def test_generates_four_lines(self):
        lines = build_lines(cfg_with())
        self.assertEqual(len(lines), 4)
        self.assertTrue(any("--task daily" in ln for ln in lines))
        self.assertTrue(any("--task noon" in ln for ln in lines))
        self.assertTrue(any("--task weekly" in ln for ln in lines))
        self.assertTrue(any("--task alerts" in ln for ln in lines))

    def test_disabled_report_not_scheduled(self):
        lines = build_lines(cfg_with(reports={"noon_enabled": False}))
        self.assertFalse(any("--task noon" in ln for ln in lines))
        self.assertEqual(len(lines), 3)

    def test_weekly_dow_conversion(self):
        """config 用 0=周一，cron 用 0=周日：周日应写成 0。"""
        lines = build_lines(cfg_with(reports={"weekly_day": 6, "weekly_time": "20:00"}))
        weekly = [ln for ln in lines if "--task weekly" in ln][0]
        self.assertTrue(weekly.startswith("0 20 * * 0"), weekly)
        # 周一 → cron 1
        lines = build_lines(cfg_with(reports={"weekly_day": 0}))
        weekly = [ln for ln in lines if "--task weekly" in ln][0]
        self.assertTrue(weekly.startswith("0 20 * * 1"), weekly)

    def test_interval_mapping(self):
        lines = build_lines(cfg_with(alerts={"interval_minutes": 30}))
        alerts = [ln for ln in lines if "--task alerts" in ln][0]
        self.assertTrue(alerts.startswith("*/30 9-23 * * *"), alerts)

    def test_all_disabled(self):
        cfg = cfg_with(reports={"daily_enabled": False, "noon_enabled": False,
                                "weekly_enabled": False},
                       alerts={"enabled": False})
        self.assertEqual(build_lines(cfg), [])

    def test_lines_use_python_and_cd(self):
        line = build_lines(cfg_with())[0]
        self.assertIn("cd ", line)
        self.assertIn("tools/run_scheduled.py", line)


if __name__ == "__main__":
    unittest.main()
