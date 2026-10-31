import json
import re
import string
import syslog
from functools import cache

from microsre.core import APP_NAME, _post, config, log, worker
from microsre.registry import KNOWN

_MD2_ESCAPE = re.compile(r"([_\*\[\]()~`>#+\-=|{}.!\\])")


def _post_json(url, payload):
    data = json.dumps(payload, check_circular=False, separators=(",", ":")).encode()
    response = _post(url, data, {"Content-Type": "application/json"}, "alert")
    if response is None and "api.telegram.org" in url:
        log.info("Are you sure you added bot to channel admins?")
    return response


def send_notification(webhook_url, text, **kwargs):
    if "api.telegram.org" in webhook_url:
        payload = {
            "chat_id": kwargs.get("chat_id") or "",
            "text": _MD2_ESCAPE.sub(r"\\\1", text[:4096]),
            "parse_mode": "MarkdownV2",
        }
    elif "hooks.slack.com" in webhook_url:
        payload = {
            "text": text,
            "username": kwargs.get("username") or APP_NAME,
            "icon_emoji": ":warning:",
        }
    elif "discord.com/api/webhooks" in webhook_url:
        payload = {
            "content": text[:2000],
            "username": kwargs.get("username") or APP_NAME,
            "allowed_mentions": {"parse": []},
        }
    else:
        payload = {
            "text": text,
            "username": kwargs.get("username") or APP_NAME,
        }

    return _post_json(webhook_url, payload)


def send_configured_notification(text):
    c = config()
    if webhook_url := c.get("webhook_url"):
        return send_notification(webhook_url, text, username=c.get("username"), chat_id=c.get("chat_id"))


class MetricsFormatter(string.Formatter):
    def get_field(self, field_name, args, kwargs):
        return kwargs.get(field_name, f"[undefined {field_name}]"), field_name


class MetricsValidator(string.Formatter):
    def get_field(self, field_name, args, kwargs):
        return kwargs[field_name], field_name


class AlertState:
    _seen: set[str] = set()
    formatter = MetricsFormatter()
    validator = MetricsValidator()

    def __init__(self, metric, min_threshold, max_threshold, duration, recovery_margin, template, dryrun=False):
        self.metric = metric
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.duration = duration
        self.recovery_margin = recovery_margin
        self.template_title = template["title"]
        self.template_body = template["body"]
        self._breaching_since = None
        self._alerting = False
        self._dryrun = dryrun

    def evaluate(self, metrics, now):
        try:
            value = metrics[self.metric]
        except KeyError:
            if self.metric not in self._seen:
                log.error(f"Alert defined on unknown metric {self.metric}")
                self._seen.add(self.metric)
            return

        if not isinstance(value, (int, float)):
            if self.metric not in self._seen:
                log.error(f"Alert defined on non-numeric metric {self.metric}: {value!r}")
                self._seen.add(self.metric)
            return

        trigger = (self.max_threshold is not None and value >= self.max_threshold) or (
            self.min_threshold is not None and value <= self.min_threshold
        )

        if trigger:
            if self._breaching_since is None:
                self._breaching_since = now
            if not self._alerting and now - self._breaching_since >= self.duration:
                self._alerting = True
                self._send_alert(self._context(metrics, value))
        else:
            self._breaching_since = None
            if self._alerting:
                recovered = (
                    not self.recovery_margin
                    or (self.max_threshold is not None and value < self.max_threshold * self.recovery_margin)
                    or (self.min_threshold is not None and value > self.min_threshold / self.recovery_margin)
                )
                if recovered:
                    self._send_recovery(self._context(metrics, value))
                    self._alerting = False

    @property
    def alerting(self):
        return self._alerting

    def _send_alert(self, context):
        if self._dryrun:
            return
        message = (
            self.formatter.vformat(self.template_title, (), context)
            + "\n\n"
            + self.formatter.vformat(self.template_body, (), context)
        )
        syslog.syslog(
            syslog.LOG_ALERT,
            f"{context['metric.name']} is {context['metric.value']} for {context['metric.duration']}s",
        )
        worker.submit(lambda: send_configured_notification(message))

    def _send_recovery(self, context):
        if self._dryrun:
            return
        message = "Recovered: " + self.formatter.vformat(self.template_title, (), context)
        syslog.syslog(syslog.LOG_ALERT, f"{context['metric.name']} has recovered")
        worker.submit(lambda: send_configured_notification(message))

    def _context(self, metrics, value):
        context = metrics.copy()
        context["metric.name"] = self.metric
        context["metric.value"] = value
        context["metric.min"] = self.min_threshold
        context["metric.max"] = self.max_threshold
        context["metric.duration"] = self.duration

        scope, dot, _ = self.metric.rpartition(".")
        scope += dot
        for key in [k for k in context if k.startswith(scope)]:
            context["scope." + key[len(scope) :]] = context[key]

        return context

    def validate(self, data):
        context = self._context(data, 11)
        return self.validator.vformat(self.template_title, (), context) + self.validator.vformat(
            self.template_body, (), context
        )


@cache
def configured_alerts(fail=False, dryrun=False):
    alerts = []
    c = config()

    templates = {t["name"]: t for t in c.get("templates", [])}

    for name, params in c.get("alerts", {}).items():
        try:
            unknown = set(params) - {"min", "max", "duration", "recovery_margin", "template"}
            if unknown:
                raise KeyError(f"Unknown alert rule keys {sorted(unknown)}")

            if params["template"] not in templates:
                raise KeyError(f"Template {params['template']!r} not defined for {name}")

            alerts.append(
                AlertState(
                    name,
                    params.get("min"),
                    params.get("max"),
                    params.get("duration", 0),
                    params.get("recovery_margin", 0),
                    templates[params["template"]],
                    dryrun=dryrun,
                )
            )
            try:
                alerts[-1].validate(_known_metrics())
            except KeyError as exc:
                raise KeyError(f"Template {params['template']!r} contains unknown field {exc}")

        except Exception as exc:
            if fail:
                raise ValueError(f"Invalid alert {name} {params}: {exc!r}") from exc
            log.error(f"Invalid alert {name!r} {params}", exc_info=True)

    return alerts


@cache
def _known_metrics():
    metrics = dict.fromkeys(KNOWN)

    for entry in config().get("logs", []):
        name = entry["name"]
        metrics.update(
            dict.fromkeys(
                (
                    f"logs.{name}.name",
                    f"logs.{name}.line_count",
                    f"logs.{name}.error_count",
                    f"logs.{name}.error_pct",
                    f"logs.{name}.errors",
                )
            )
        )
        if entry.get("heartbeat_pattern"):
            metrics.update(
                dict.fromkeys(
                    (
                        f"logs.{name}.heartbeat_last_seen",
                        f"logs.{name}.heartbeat_age_seconds",
                        f"logs.{name}.heartbeat_status",
                    )
                )
            )
    for entry in config().get("docker-logs", []):
        for name in entry.get("containers", []):
            metrics.update(
                dict.fromkeys(
                    (
                        f"docker-logs.{name}.name",
                        f"docker-logs.{name}.line_count",
                        f"docker-logs.{name}.error_count",
                        f"docker-logs.{name}.error_pct",
                        f"docker-logs.{name}.errors",
                    )
                )
            )
            if entry.get("heartbeat_pattern"):
                metrics.update(
                    dict.fromkeys(
                        (
                            f"docker-logs.{name}.heartbeat_last_seen",
                            f"docker-logs.{name}.heartbeat_age_seconds",
                            f"docker-logs.{name}.heartbeat_status",
                        )
                    )
                )

    return metrics
