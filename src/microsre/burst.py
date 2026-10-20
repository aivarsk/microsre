import collections

from microsre.metrics import calculate_burst_metrics
from microsre.procfs import collect_burst_procfs

_GAP_SECONDS = 2.0


class BurstSampler:
    def __init__(self, interval, read=collect_burst_procfs):
        self._read = read
        self._samples = collections.deque(maxlen=2 * interval + 10)
        self._dropped = 0
        self._previous = None
        self._previous_ts = None

    def _tick(self, now):
        try:
            current = self._read()
        except OSError:
            return
        if self._previous is not None and now - self._previous_ts <= _GAP_SECONDS:
            if len(self._samples) == self._samples.maxlen:
                self._dropped += 1
            self._samples.append(calculate_burst_metrics(self._previous, current, max(now - self._previous_ts, 1e-6)))
        self._previous, self._previous_ts = current, now

    def drain(self):
        maxima = {}
        hottest_core = None
        count = 0
        while self._samples:
            count += 1
            sample = self._samples.popleft()
            for key, value in sample.items():
                if key == "cpu.max_utilization_core":
                    continue
                if key not in maxima or value > maxima[key]:
                    maxima[key] = value
                    if key == "cpu.max_utilization_pct":
                        hottest_core = sample.get("cpu.max_utilization_core")
        if hottest_core is not None:
            maxima["cpu.max_utilization_core"] = hottest_core
        drained = {"burst.samples": count, "burst.dropped": self._dropped}
        self._dropped = 0
        drained.update({f"burst.{key}": value for key, value in maxima.items()})
        return drained
