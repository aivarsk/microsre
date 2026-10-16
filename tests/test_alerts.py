import json
import unittest

from testutil import Receiver, isolate_xdg

from microsre.alert import AlertState, configured_alerts, send_notification

TEMPLATE = {"title": "{metric.name} on {hostname}", "body": "value {metric.value}"}


def setUpModule():
    isolate_xdg()


def make_alert(metric="foo", min=None, max=None, duration=60, margin=0, template=TEMPLATE):
    return AlertState(metric, min, max, duration, margin, template)


class TestTriggering(unittest.TestCase):
    def test_trigger_and_recover_on_max(self):
        alert = make_alert(max=5, duration=60)
        alert.evaluate({"foo": 4, "hostname": "h"}, now=1000)
        self.assertFalse(alert.alerting)
        alert.evaluate({"foo": 5, "hostname": "h"}, now=1000)  # breach starts
        self.assertFalse(alert.alerting)
        alert.evaluate({"foo": 5, "hostname": "h"}, now=1059)
        self.assertFalse(alert.alerting)
        alert.evaluate({"foo": 5, "hostname": "h"}, now=1060)
        self.assertTrue(alert.alerting)
        alert.evaluate({"foo": 4, "hostname": "h"}, now=1061)
        self.assertFalse(alert.alerting)

    def test_trigger_on_min(self):
        alert = make_alert(min=5, duration=60)
        alert.evaluate({"foo": 6}, now=0)
        self.assertFalse(alert.alerting)
        alert.evaluate({"foo": 5}, now=60)  # breach starts
        self.assertFalse(alert.alerting)
        alert.evaluate({"foo": 5}, now=120)
        self.assertTrue(alert.alerting)

    def test_breach_resets_after_recovery(self):
        alert = make_alert(max=5, duration=120)
        alert.evaluate({"foo": 5}, now=1000)
        alert.evaluate({"foo": 5}, now=1100)
        self.assertFalse(alert.alerting)
        alert.evaluate({"foo": 5}, now=1200)
        self.assertTrue(alert.alerting)

        alert.evaluate({"foo": 4}, now=1210)
        self.assertFalse(alert.alerting)

        alert.evaluate({"foo": 5}, now=1220)
        self.assertFalse(alert.alerting)
        alert.evaluate({"foo": 5}, now=1339)
        self.assertFalse(alert.alerting)
        alert.evaluate({"foo": 5}, now=1340)
        self.assertTrue(alert.alerting)

    def test_breach_clock_resets_when_value_clears(self):
        alert = make_alert(max=5, duration=120)
        alert.evaluate({"foo": 5}, now=1000)
        alert.evaluate({"foo": 5}, now=1100)
        alert.evaluate({"foo": 4}, now=1110)  # clears before the duration elapses
        alert.evaluate({"foo": 5}, now=1120)  # breach restarts
        alert.evaluate({"foo": 5}, now=1239)
        self.assertFalse(alert.alerting)
        alert.evaluate({"foo": 5}, now=1240)
        self.assertTrue(alert.alerting)

    def test_recovery_margin_hysteresis(self):
        alert = make_alert(max=50, margin=0.8, duration=60)
        alert.evaluate({"foo": 50}, now=0)
        alert.evaluate({"foo": 50}, now=60)
        self.assertTrue(alert.alerting)

        alert.evaluate({"foo": 41}, now=61)
        self.assertTrue(alert.alerting)

        alert.evaluate({"foo": 39}, now=62)
        self.assertFalse(alert.alerting)


class TestBadMetrics(unittest.TestCase):
    def test_unknown_metric_logs_once_and_stays_silent(self):
        alert = make_alert(metric="nope", max=5)
        with self.assertLogs("microsre", level="ERROR") as records:
            alert.evaluate({"foo": 9}, now=0)
            alert.evaluate({"foo": 9}, now=60)
        self.assertEqual(len(records.records), 1)
        self.assertFalse(alert.alerting)

    def test_non_numeric_metric_does_not_crash(self):
        alert = make_alert(metric="foo", max=5)
        with self.assertLogs("microsre", level="ERROR"):
            alert.evaluate({"foo": "a string"}, now=0)
        self.assertFalse(alert.alerting)


class TestTemplates(unittest.TestCase):
    def test_validate_accepts_known_fields(self):
        from microsre.alert import _known_metrics

        alert = make_alert(metric="mem.utilization_pct", template={"title": "{mem.utilization_pct}", "body": ""})
        self.assertIsInstance(alert.validate(_known_metrics()), str)

    def test_validate_rejects_unknown_fields(self):
        from microsre.alert import _known_metrics

        alert = make_alert(template={"title": "{does.not.exist}", "body": ""})
        with self.assertRaises(KeyError):
            alert.validate(_known_metrics())

    def test_undefined_field_renders_as_placeholder(self):
        alert = make_alert(template={"title": "{metric.name} {unknown.thing}", "body": ""})
        context = alert._context({"foo": 9, "hostname": "h"}, 9)
        rendered = AlertState.formatter.vformat(alert.template_title, (), context)
        self.assertEqual(rendered, "foo [undefined unknown.thing]")


class TestConfiguredAlerts(unittest.TestCase):
    def test_default_config_produces_valid_alerts(self):
        from microsre.alert import _known_metrics

        alerts = configured_alerts()
        self.assertTrue(alerts)
        for alert in alerts:
            alert.validate(_known_metrics())


class TestKnownMetrics(unittest.TestCase):
    def test_known_metrics_match_registry(self):
        from microsre import registry
        from microsre.alert import _known_metrics

        for name in (
            "mem.utilization_pct",
            "mem.pressure.full.avg60.previous",
            "cpu.loadavg.1min.previous",
            "net.tcp.listen_overflows",
            "disk.max.queue_depth",
            "oom.victims",
            "uptime",
        ):
            self.assertIn(name, _known_metrics())
        self.assertTrue(set(registry.KNOWN) <= set(_known_metrics()))


class TestNotification(unittest.TestCase):
    def setUp(self):
        self.receiver = Receiver()

    def tearDown(self):
        self.receiver.close()

    def test_generic_webhook_posts_json(self):
        send_notification(
            f"http://127.0.0.1:{self.receiver.port}/hook",
            "hello world",
            username="tester",
        )
        request = self.receiver.first_request()
        self.assertIsNotNone(request)
        payload = json.loads(request[2])
        self.assertEqual(payload["text"], "hello world")
        self.assertEqual(payload["username"], "tester")


if __name__ == "__main__":
    unittest.main()
