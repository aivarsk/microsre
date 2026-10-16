# microSRE

Know when your server needs you. Linux health checks at your cadence - no Prometheus, Grafana, or metrics agent.

Built for people who fix problems, not dashboards.

## Install

```bash
curl -fsSL https://microsre.io/install.sh | bash
```

No root required. Root (or the docker group) adds container log scanning.

## What it does

microSRE reads `/proc` directly and watches application logs you specify. It samples CPU, memory, disk I/O, network counters, and pressure signals, then alerts when thresholds are crossed.

Alerts go to Slack, Discord, or Telegram. No dashboards. No data-source sprawl. No lock-in.

## Why direct observation

- **Stability** - kernel ABI, not userland format strings
- **Speed** - no fork/exec, no parsing overhead
- **Portability** - works in minimal containers, rescue shells, and locked-down hosts
- **Completeness** - access fields that `ps` and `top` hide or misrepresent
- **Early warning** - monitor `/proc/pressure/memory`, the same pressure signal systemd-oomd watches when it decides which cgroup to kill

Your logs already contain the errors; microSRE alerts on patterns and thresholds without code changes or an SDK.

## Fleet beta

The optional hosted view aggregates health checks across machines. Raw metrics and logs stay local; the hosted view receives only periodic health summaries and alert state.

Join the private beta: https://microsre.io/#saas

## Links

- Website: https://microsre.io/
- Documentation: https://microsre.io/readme.html
- GitHub: https://github.com/aivarsk/microsre
