"""Fixed-memory latency histograms; quantiles are labelled upper bounds."""

import math


class LatencyHistogram:
    BOUNDS_MS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000)

    def __init__(self):
        self.buckets = [0] * (len(self.BOUNDS_MS) + 1)
        self.count = 0
        self.maximum = 0.0

    def record(self, milliseconds):
        if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)) or not math.isfinite(milliseconds) or milliseconds < 0:
            raise ValueError("Finite nonnegative latency required")
        index = next((i for i, bound in enumerate(self.BOUNDS_MS) if milliseconds <= bound), len(self.BOUNDS_MS))
        self.buckets[index] += 1
        self.count += 1
        self.maximum = max(self.maximum, milliseconds)

    def summary(self):
        def percentile(fraction):
            if not self.count:
                return None
            target, cumulative = math.ceil(self.count * fraction), 0
            for index, number in enumerate(self.buckets):
                cumulative += number
                if cumulative >= target:
                    return self.BOUNDS_MS[index] if index < len(self.BOUNDS_MS) else None
        return {"method": "fixed-bucket-upper-bound", "count": self.count, "max_ms": self.maximum,
                "p95_upper_ms": percentile(.95), "p99_upper_ms": percentile(.99),
                "overflow_above_60000_ms": self.buckets[-1]}
