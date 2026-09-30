# microSRE

60-second Linux health checks. Know when your server needs you - without Prometheus, Grafana, or a metrics agent.

Built for people who fix problems, not dashboards.

## Install

```bash
curl -fsSL https://microsre.io/install.sh | bash
```

No root required. Root unlocks a few extra signals, including per-process network details.

## What it does

microSRE reads `/proc` directly and watches application logs you specify. It samples CPU, memory, disk I/O, network counters, and pressure signals, then alerts when thresholds are crossed.

Alerts go to Slack, Discord, or Telegram. No dashboards. No data-source sprawl. No lock-in.

## Why direct observation

- **Stability** - kernel ABI, not userland format strings
- **Speed** - no fork/exec, no parsing overhead
- **Portability** - works in minimal containers, rescue shells, and locked-down hosts
- **Completeness** - access fields that `ps` and `top` hide or misrepresent
- **Early warning** - monitor `/proc/pressure/mem`, the same pressure signal systemd uses for OOM decisions

Your logs already contain the errors; microSRE alerts on patterns and thresholds without code changes or an SDK.

## Fleet beta

The optional hosted view aggregates health checks across machines. Raw metrics and logs stay local; the hosted view receives only 60-second health summaries and alert state.

Join the private beta: https://microsre.io/#saas

## Links

- Website: https://microsre.io/
- Documentation: https://microsre.io/readme.html
- GitHub: https://github.com/aivarsk/microsre
