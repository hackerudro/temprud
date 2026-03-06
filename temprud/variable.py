# temprud — native query operators on variable history
# by hackerudro
#
# Variables usually only hold "now". Once you assign, the past is gone.
# Here, the variable keeps its history and you query it: .at("2 hours ago"),
# .max(), .since("yesterday"). Time is a dimension of state, not an afterthought.
#
# Optional: scikit-learn for ML anomaly detection. Otherwise we fall back to z-score.
#   pip install scikit-learn

import os
import json
import smtplib
import statistics
import threading
import subprocess
import sys
from datetime import datetime, timedelta
from collections import deque
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional, Callable

try:
    from sklearn.ensemble import IsolationForest
    import numpy as np
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False


# ---------------------------------------------------------------------------
#  Registry: all live temprud variables by name
# ---------------------------------------------------------------------------
_REGISTRY: dict = {}

def _register(name: str, instance) -> None:
    _REGISTRY[name] = instance

def get_all() -> dict:
    """Return every registered temprud variable (name -> instance)."""
    return _REGISTRY.copy()


# ---------------------------------------------------------------------------
#  Time strings → datetime. So you can say "6 hours ago" instead of timestamps.
# ---------------------------------------------------------------------------
def _parse_time_string(time_str: str) -> datetime:
    """
    Human-readable time → datetime.
    e.g. "6 hours ago", "30 minutes ago", "2 days ago", "2024-01-15 09:30:00"
    """
    now = datetime.now()
    s = time_str.strip().lower()

    if s in ("now", "today"):
        return now

    if "ago" in s:
        parts = s.replace(" ago", "").strip().split()
        if len(parts) == 2:
            amount = float(parts[0])
            unit = parts[1].rstrip("s")
            mapping = {
                "second": timedelta(seconds=amount),
                "sec":    timedelta(seconds=amount),
                "minute": timedelta(minutes=amount),
                "min":    timedelta(minutes=amount),
                "hour":   timedelta(hours=amount),
                "day":    timedelta(days=amount),
                "week":   timedelta(weeks=amount),
                "month":  timedelta(days=amount * 30),
                "year":   timedelta(days=amount * 365),
            }
            if unit in mapping:
                return now - mapping[unit]

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(time_str.strip(), fmt)
        except ValueError:
            continue

    raise ValueError(
        f"Cannot parse: '{time_str}'. "
        f"Try '6 hours ago', '30 minutes ago', or '2024-01-15 09:30:00'"
    )


# ---------------------------------------------------------------------------
#  Expiry — how long we keep history (by time or by entry count)
# ---------------------------------------------------------------------------
class TemprudExpiry:
    """
    How much history to keep. Old entries are dropped automatically.

    TemprudExpiry(days=30)         — keep last 30 days
    TemprudExpiry(hours=24)        — keep last 24 hours
    TemprudExpiry(max_entries=1000) — keep last 1000 updates, no time limit
    """
    def __init__(self,
                 seconds: float = 0,
                 minutes: float = 0,
                 hours:   float = 0,
                 days:    float = 0,
                 weeks:   float = 0,
                 max_entries: int = None):

        total_seconds = (
            seconds +
            minutes * 60 +
            hours   * 3600 +
            days    * 86400 +
            weeks   * 604800
        )
        self.max_age = timedelta(seconds=total_seconds) if total_seconds > 0 else None
        self.max_entries = max_entries

    def cutoff(self) -> Optional[datetime]:
        if self.max_age:
            return datetime.now() - self.max_age
        return None

    def describe(self) -> str:
        parts = []
        if self.max_age:
            total = self.max_age.total_seconds()
            if total >= 604800:
                parts.append(f"{total/604800:.1f} weeks")
            elif total >= 86400:
                parts.append(f"{total/86400:.1f} days")
            elif total >= 3600:
                parts.append(f"{total/3600:.1f} hours")
            elif total >= 60:
                parts.append(f"{total/60:.1f} minutes")
            else:
                parts.append(f"{total:.0f} seconds")
        if self.max_entries:
            parts.append(f"{self.max_entries} max entries")
        return ", ".join(parts) if parts else "no expiry"


# ---------------------------------------------------------------------------
#  Alerts — fire when something in history/current value matches a condition
# ---------------------------------------------------------------------------
class TemprudAlert:
    """
    Hook into value changes: anomaly, threshold, percent move, or your own condition.
    notify: "print" | "sound" | "email" | any callable(name, value, score, timestamp, message).
    """

    TYPE_ANOMALY   = "anomaly"
    TYPE_THRESHOLD = "threshold"
    TYPE_CHANGE    = "change"
    TYPE_CUSTOM    = "custom"

    def __init__(self,
                 alert_type:  str,
                 notify,
                 cooldown_seconds: int = 60,
                 label:       str = None,
                 above=None,
                 below=None,
                 percent:     float = None,
                 condition:   Callable = None,
                 email_to:    str = None,
                 email_from:  str = None,
                 email_pass:  str = None,
                 smtp_host:   str = "smtp.gmail.com",
                 smtp_port:   int = 587):

        self.alert_type       = alert_type
        self.notify           = notify
        self.cooldown_seconds = cooldown_seconds
        self.label            = label
        self.above            = above
        self.below            = below
        self.percent          = percent
        self.condition        = condition
        self.email_to         = email_to
        self.email_from       = email_from
        self.email_pass       = email_pass
        self.smtp_host        = smtp_host
        self.smtp_port        = smtp_port
        self._last_fired: Optional[datetime] = None

    @classmethod
    def on_anomaly(cls, notify="print", cooldown_seconds: int = 60, label: str = None):
        """Fire when the current value is flagged as anomalous (ML or z-score)."""
        return cls(cls.TYPE_ANOMALY, notify=notify,
                   cooldown_seconds=cooldown_seconds, label=label)

    @classmethod
    def on_threshold(cls, above=None, below=None, notify="print",
                     cooldown_seconds: int = 60, label: str = None):
        """Fire when value goes above or below a number."""
        return cls(cls.TYPE_THRESHOLD, notify=notify, above=above, below=below,
                   cooldown_seconds=cooldown_seconds, label=label)

    @classmethod
    def on_change(cls, percent: float, notify="print",
                  cooldown_seconds: int = 60, label: str = None):
        """Fire when value moves by more than X% in one step."""
        return cls(cls.TYPE_CHANGE, notify=notify, percent=percent,
                   cooldown_seconds=cooldown_seconds, label=label)

    @classmethod
    def on_custom(cls, condition: Callable, notify="print",
                  cooldown_seconds: int = 60, label: str = None):
        """Fire when condition(temprud_instance) is True. You get the full variable to query."""
        return cls(cls.TYPE_CUSTOM, notify=notify, condition=condition,
                   cooldown_seconds=cooldown_seconds, label=label)

    @classmethod
    def on_anomaly_email(cls, email_to: str, email_from: str, email_pass: str,
                         smtp_host: str = "smtp.gmail.com", smtp_port: int = 587,
                         cooldown_seconds: int = 300):
        """Email when an anomaly is detected."""
        return cls(cls.TYPE_ANOMALY, notify="email",
                   email_to=email_to, email_from=email_from,
                   email_pass=email_pass, smtp_host=smtp_host,
                   smtp_port=smtp_port, cooldown_seconds=cooldown_seconds)

    def _on_cooldown(self) -> bool:
        if self._last_fired is None:
            return False
        elapsed = (datetime.now() - self._last_fired).total_seconds()
        return elapsed < self.cooldown_seconds

    def check(self, temprud_instance, is_anomaly: bool) -> bool:
        """See if this alert should fire; if so, dispatch and return True."""
        if self._on_cooldown():
            return False

        fired = False
        value = temprud_instance.now

        if self.alert_type == self.TYPE_ANOMALY:
            fired = is_anomaly

        elif self.alert_type == self.TYPE_THRESHOLD:
            if self.above is not None and isinstance(value, (int, float)):
                fired = value > self.above
            if self.below is not None and isinstance(value, (int, float)):
                fired = fired or (value < self.below)

        elif self.alert_type == self.TYPE_CHANGE:
            values = temprud_instance._numeric_values()
            if len(values) >= 2 and self.percent is not None:
                prev = values[-2]
                if prev != 0:
                    pct = abs((value - prev) / prev) * 100
                    fired = pct >= self.percent

        elif self.alert_type == self.TYPE_CUSTOM:
            if self.condition:
                try:
                    fired = bool(self.condition(temprud_instance))
                except Exception:
                    fired = False

        if fired:
            self._last_fired = datetime.now()
            self._dispatch(temprud_instance, value, is_anomaly)

        return fired

    def _dispatch(self, temprud_instance, value, is_anomaly: bool) -> None:
        name  = temprud_instance.name
        score = temprud_instance.anomaly_score()
        ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        label = self.label or self.alert_type.upper()

        msg = (
            f"[TEMPRUD] [{label}] "
            f"'{name}' = {value} | anomaly_score={score} | {ts}"
        )

        if self.notify == "print":
            print(f"\n[!] {msg}\n")

        elif self.notify == "sound":
            self._play_sound()
            print(f"\n[*] {msg}\n")

        elif self.notify == "email":
            self._send_email(name, value, score, ts)

        elif callable(self.notify):
            try:
                self.notify(name=name, value=value, score=score,
                            timestamp=ts, message=msg)
            except Exception as e:
                print(f"[temprud] Alert callback error: {e}")

    def _play_sound(self) -> None:
        try:
            if sys.platform == "win32":
                import winsound
                winsound.Beep(1000, 500)
            elif sys.platform == "darwin":
                subprocess.run(["afplay", "/System/Library/Sounds/Ping.aiff"],
                               capture_output=True)
            else:
                print("\a", end="", flush=True)
        except Exception:
            print("\a", end="", flush=True)

    def _send_email(self, name, value, score, ts) -> None:
        def _send():
            try:
                subject = f"[temprud] Anomaly in '{name}'"
                body = (
                    f"temprud variable alert.\n\n"
                    f"Variable : {name}\n"
                    f"Value    : {value}\n"
                    f"Score    : {score}\n"
                    f"Time     : {ts}\n"
                )
                msg = MIMEMultipart()
                msg["From"]    = self.email_from
                msg["To"]      = self.email_to
                msg["Subject"] = subject
                msg.attach(MIMEText(body, "plain"))

                with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                    server.starttls()
                    server.login(self.email_from, self.email_pass)
                    server.send_message(msg)
                print(f"[temprud] Email sent to {self.email_to}")
            except Exception as e:
                print(f"[temprud] Email failed: {e}")

        threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------------------
#  Anomaly detector: Isolation Forest when sklearn is around, else z-score
# ---------------------------------------------------------------------------
class _MLAnomalyDetector:
    """
    Isolation Forest: learns "normal" from history, flags outliers.
    Retrains when enough new points show up.
    """
    def __init__(self, min_train: int = 30, retrain_every: int = 50,
                 contamination: float = 0.05):
        self.min_train      = min_train
        self.retrain_every  = retrain_every
        self.contamination  = contamination
        self._model         = None
        self._trained_on    = 0
        self._available     = ML_AVAILABLE

    def _features(self, values: list) -> list:
        window = 10
        features = []
        for i, v in enumerate(values):
            chunk = values[max(0, i - window):i + 1]
            mean_diff = v - (sum(chunk) / len(chunk))
            std = statistics.stdev(chunk) if len(chunk) > 1 else 0
            features.append([v, mean_diff, std])
        return features

    def train(self, values: list) -> None:
        if not self._available or len(values) < self.min_train:
            return
        features = self._features(values)
        self._model = IsolationForest(
            contamination=self.contamination,
            random_state=42,
            n_estimators=100
        )
        self._model.fit(features)
        self._trained_on = len(values)

    def should_retrain(self, n_values: int) -> bool:
        return n_values >= self.min_train and (
            self._model is None or
            (n_values - self._trained_on) >= self.retrain_every
        )

    def score(self, values: list) -> float:
        """Anomaly score for the latest value. 0 = normal, 1 = very anomalous."""
        if not self._available or self._model is None or len(values) < 3:
            return 0.0
        features = self._features(values)
        last_feature = [features[-1]]
        raw = self._model.decision_function(last_feature)[0]
        score = max(0.0, min(1.0, 0.5 - raw))
        return round(score, 4)

    def is_anomaly(self, values: list) -> bool:
        if not self._available or self._model is None or len(values) < 3:
            return False
        features = self._features(values)
        prediction = self._model.predict([features[-1]])[0]
        return prediction == -1


# ---------------------------------------------------------------------------
#  Temprud — variable with queryable history (native operators)
# ---------------------------------------------------------------------------
class Temprud:
    """
    A variable that keeps its history and exposes it via query operators.

    You don't log to a DB or build event sourcing. You assign with .set(value);
    the variable remembers, and you ask: .now, .at("2 hours ago"), .since("yesterday"),
    .max(), .min(), .average(), .trend(), .rollback(1), etc.

    Parameters:
        initial_value : optional starting value
        name         : id for this variable (alerts, registry)
        expire       : TemprudExpiry — how long to keep history
        alerts       : list of TemprudAlert — when to notify
        persist      : path to JSON file — history survives restarts
        ml           : use ML anomaly detection if sklearn available (default True)
        max_history  : cap on in-memory entries (default 100_000)

    Example:
        price = Temprud(100, name="price")
        price.set(150)
        price.set(90)
        price.now           # 90
        price.at("1 hour ago")
        price.max()         # 150
    """

    def __init__(self,
                 initial_value=None,
                 name:         str             = None,
                 expire:       "TemprudExpiry" = None,
                 alerts:       List["TemprudAlert"] = None,
                 persist:      str             = None,
                 ml:           bool            = True,
                 max_history:  int             = 100_000):

        self._name        = name or f"temprud_{id(self)}"
        self._expire      = expire
        self._alerts      = alerts or []
        self._persist     = persist
        self._max_history = max_history
        self._history     = deque(maxlen=max_history)

        self._ml_enabled  = ml and ML_AVAILABLE
        self._ml_detector = _MLAnomalyDetector() if self._ml_enabled else None

        self._stat_window      = 50
        self._stat_sensitivity = 2.5

        if self._persist:
            self._load()

        if initial_value is not None:
            self._record(initial_value)

        _register(self._name, self)

    def _record(self, value) -> None:
        entry = {"timestamp": datetime.now().isoformat(), "value": value}
        self._history.append(entry)
        self._apply_expiry()
        self._maybe_retrain()
        self._check_alerts()
        if self._persist:
            self._save()

    def _ts(self, entry) -> datetime:
        ts = entry["timestamp"]
        if isinstance(ts, datetime):
            return ts
        return datetime.fromisoformat(ts)

    def _apply_expiry(self) -> None:
        if not self._expire:
            return
        cutoff = self._expire.cutoff()
        if cutoff:
            while self._history and self._ts(self._history[0]) < cutoff:
                self._history.popleft()
        if self._expire.max_entries:
            while len(self._history) > self._expire.max_entries:
                self._history.popleft()

    def _maybe_retrain(self) -> None:
        if not self._ml_enabled:
            return
        values = self._numeric_values()
        if self._ml_detector.should_retrain(len(values)):
            self._ml_detector.train(values)

    def _check_alerts(self) -> None:
        if not self._alerts:
            return
        is_anom = self.is_anomaly()
        for alert in self._alerts:
            alert.check(self, is_anom)

    def _values(self) -> list:
        return [e["value"] for e in self._history]

    def _numeric_values(self) -> list:
        return [v for v in self._values() if isinstance(v, (int, float))]

    def _save(self) -> None:
        try:
            data = {
                "name": self._name,
                "saved_at": datetime.now().isoformat(),
                "entries": list(self._history)
            }
            with open(self._persist, "w") as f:
                json.dump(data, f, default=str)
        except Exception as e:
            print(f"[temprud] Save failed: {e}")

    def _load(self) -> None:
        if not os.path.exists(self._persist):
            return
        try:
            with open(self._persist, "r") as f:
                data = json.load(f)
            for entry in data.get("entries", []):
                self._history.append(entry)
            print(f"[temprud] Loaded {len(self._history)} entries from '{self._persist}'")
        except Exception as e:
            print(f"[temprud] Load failed: {e}")

    def save(self, filepath: str = None) -> None:
        path = filepath or self._persist
        if not path:
            raise ValueError("No filepath. Pass filepath or set persist= in constructor.")
        old = self._persist
        self._persist = path
        self._save()
        self._persist = old
        print(f"[temprud] Saved {len(self._history)} entries to '{path}'")

    def load(self, filepath: str) -> None:
        old = self._persist
        self._persist = filepath
        self._load()
        self._persist = old

    # --- Core API: assign and read current / first ---

    def set(self, value) -> None:
        """Update the variable. History is kept; alerts may fire."""
        self._record(value)

    @property
    def now(self):
        """Current value (last assigned)."""
        return self._history[-1]["value"] if self._history else None

    @property
    def first(self):
        """First value ever recorded."""
        return self._history[0]["value"] if self._history else None

    @property
    def name(self) -> str:
        return self._name

    @property
    def history(self) -> list:
        """Full history: list of (datetime, value) tuples."""
        return [(self._ts(e), e["value"]) for e in self._history]

    @property
    def count(self) -> int:
        return len(self._history)

    # --- Native query operators (time as a dimension) ---

    def at(self, time_str: str) -> dict:
        """
        Value closest to a given time. Native "what was x at T?".
        e.g. .at("6 hours ago"), .at("2024-01-15 09:30:00")
        """
        target = _parse_time_string(time_str)
        if not self._history:
            return None
        closest = min(self._history,
                      key=lambda e: abs((self._ts(e) - target).total_seconds()))
        diff = abs((self._ts(closest) - target).total_seconds())
        return {
            "value":          closest["value"],
            "actual_time":    self._ts(closest),
            "requested_time": target,
            "seconds_off":    round(diff, 2)
        }

    def between(self, start_str: str, end_str: str) -> list:
        """All (timestamp, value) between two times."""
        start = _parse_time_string(start_str)
        end   = _parse_time_string(end_str)
        return [(self._ts(e), e["value"])
                for e in self._history
                if start <= self._ts(e) <= end]

    def since(self, time_str: str) -> list:
        """All values since a time. Shorthand for .between(time_str, "now")."""
        return self.between(time_str, "now")

    def max(self, window: str = None):
        """Max over full history or over window (e.g. "7 days")."""
        v = self._window_values(window)
        return max(v) if v else None

    def min(self, window: str = None):
        v = self._window_values(window)
        return min(v) if v else None

    def _window_values(self, window: str = None) -> list:
        if window:
            return [v for _, v in self.since(window) if isinstance(v, (int, float))]
        return self._numeric_values()

    def average(self, window: str = None):
        v = self._window_values(window)
        return round(statistics.mean(v), 4) if v else None

    def std_dev(self, window: str = None):
        v = self._window_values(window)
        return round(statistics.stdev(v), 4) if len(v) >= 2 else 0

    def delta(self, window: str = None):
        """Latest minus earliest in window (or full history)."""
        v = self._window_values(window)
        return round(v[-1] - v[0], 4) if len(v) >= 2 else 0

    def percent_change(self, window: str = None):
        v = self._window_values(window)
        if len(v) < 2 or v[0] == 0:
            return 0
        return round(((v[-1] - v[0]) / abs(v[0])) * 100, 2)

    def trend(self, window: str = None) -> str:
        """'increasing' | 'decreasing' | 'stable' (or 'not enough data')."""
        v = self._window_values(window)
        if len(v) < 3:
            return "not enough data"
        mid = len(v) // 2
        a = statistics.mean(v[:mid])
        b = statistics.mean(v[mid:])
        thresh = abs(a) * 0.01
        if b > a + thresh:
            return "increasing"
        elif b < a - thresh:
            return "decreasing"
        return "stable"

    def rollback(self, steps: int = 1):
        """Value from N steps ago. .rollback(1) = previous value."""
        h = list(self._history)
        idx = -(steps + 1)
        if abs(idx) > len(h):
            return h[0]["value"] if h else None
        return h[idx]["value"]

    def was_above(self, threshold, window: str = None) -> bool:
        return any(v > threshold for v in self._window_values(window))

    def was_below(self, threshold, window: str = None) -> bool:
        return any(v < threshold for v in self._window_values(window))

    # --- Anomaly: ML (Isolation Forest) or z-score fallback ---

    def is_anomaly(self, sensitivity: float = None) -> bool:
        """True if current value is anomalous given history."""
        values = self._numeric_values()
        if self._ml_enabled and self._ml_detector._model is not None:
            return self._ml_detector.is_anomaly(values)
        return self._stat_anomaly(values, sensitivity)

    def _stat_anomaly(self, values: list, sensitivity: float = None) -> bool:
        sens = sensitivity or self._stat_sensitivity
        if len(values) < 10:
            return False
        window = min(self._stat_window, len(values) - 1)
        baseline = values[-window - 1:-1]
        if len(baseline) < 5:
            return False
        mean = statistics.mean(baseline)
        std = statistics.stdev(baseline)
        if std == 0:
            return values[-1] != mean
        return abs((values[-1] - mean) / std) > sens

    def anomaly_score(self) -> float:
        """0 = normal, higher = more anomalous. ML: 0–1; z-score fallback: raw z."""
        values = self._numeric_values()
        if self._ml_enabled and self._ml_detector._model is not None:
            return self._ml_detector.score(values)
        if len(values) < 10:
            return 0.0
        window = min(self._stat_window, len(values) - 1)
        baseline = values[-window - 1:-1]
        if len(baseline) < 5:
            return 0.0
        mean = statistics.mean(baseline)
        std = statistics.stdev(baseline)
        if std == 0:
            return 0.0
        return round(abs((values[-1] - mean) / std), 3)

    def anomaly_history(self) -> list:
        """Scan history for anomalous points. Returns (timestamp, value, score) list."""
        h = list(self._history)
        anomalies = []
        if len(h) < 15:
            return []
        for i in range(10, len(h)):
            baseline = [
                e["value"] for e in h[max(0, i - self._stat_window):i]
                if isinstance(e["value"], (int, float))
            ]
            if len(baseline) < 5:
                continue
            val = h[i]["value"]
            if not isinstance(val, (int, float)):
                continue
            mean = statistics.mean(baseline)
            std = statistics.stdev(baseline) if len(baseline) > 1 else 0
            if std == 0:
                continue
            score = abs((val - mean) / std)
            if score > self._stat_sensitivity:
                anomalies.append((self._ts(h[i]), val, round(score, 3)))
        return anomalies

    def detection_method(self) -> str:
        if self._ml_enabled and self._ml_detector._model is not None:
            return "ML (Isolation Forest)"
        elif self._ml_enabled:
            return "ML (training — need more data)"
        return "Statistical (z-score fallback)"

    # --- Cross-variable: correlate two temprud series ---

    def correlates_with(self, other: "Temprud", lag_seconds: int = 0) -> dict:
        """Pearson correlation with another variable (optional lag)."""
        a = self._numeric_values()
        b = other._numeric_values()
        if len(a) < 5 or len(b) < 5:
            return {"correlation": None, "strength": "unknown",
                    "interpretation": "Not enough data."}
        n = min(len(a), len(b))
        a, b = a[-n:], b[-n:]
        if lag_seconds > 0 and n > lag_seconds:
            lag_steps = max(1, lag_seconds // 10)
            a = a[lag_steps:]
            b = b[:len(a)]
        corr = self._pearson(a, b)
        if corr is None:
            return {"correlation": None, "strength": "unknown",
                    "interpretation": "Could not calculate (constant values)."}
        ac = abs(corr)
        strength  = "strong" if ac >= 0.8 else "moderate" if ac >= 0.5 else "weak" if ac >= 0.2 else "none"
        direction = "positive" if corr > 0 else "negative"
        lag_text = ""
        if lag_seconds > 0:
            if lag_seconds < 60:      lag_text = f" {lag_seconds} seconds later"
            elif lag_seconds < 3600:  lag_text = f" {lag_seconds//60} minutes later"
            elif lag_seconds < 86400: lag_text = f" {lag_seconds//3600} hours later"
            else:                     lag_text = f" {lag_seconds//86400} days later"
        if strength == "none":
            interp = f"'{self._name}' and '{other._name}' are not related."
        elif direction == "positive":
            interp = (f"'{self._name}' and '{other._name}' {strength} positive. "
                      f"When one goes up, the other tends up{lag_text}. (r={round(corr, 3)})")
        else:
            interp = (f"'{self._name}' and '{other._name}' {strength} negative. "
                      f"When one goes up, the other tends down{lag_text}. (r={round(corr, 3)})")
        return {"correlation": round(corr, 4), "strength": strength,
                "direction": direction, "interpretation": interp}

    def _pearson(self, a, b):
        n = len(a)
        if n < 2:
            return None
        ma, mb = sum(a)/n, sum(b)/n
        num  = sum((a[i]-ma)*(b[i]-mb) for i in range(n))
        da   = sum((x-ma)**2 for x in a)**0.5
        db   = sum((x-mb)**2 for x in b)**0.5
        if da == 0 or db == 0:
            return None
        return num / (da * db)

    # --- Alert management ---

    def add_alert(self, alert: "TemprudAlert") -> None:
        self._alerts.append(alert)

    def remove_alerts(self) -> None:
        self._alerts = []

    def list_alerts(self) -> None:
        if not self._alerts:
            print(f"[{self._name}] No alerts.")
            return
        print(f"[{self._name}] {len(self._alerts)} alert(s):")
        for i, a in enumerate(self._alerts):
            notify = a.notify if isinstance(a.notify, str) else "custom"
            print(f"   {i+1}. {a.alert_type} | notify={notify} | cooldown={a.cooldown_seconds}s")

    # --- Summary ---

    def summary(self) -> None:
        v = self._numeric_values()
        print("=" * 55)
        print(f"  temprud variable: '{self._name}'")
        print("=" * 55)
        print(f"  now               : {self.now}")
        print(f"  first             : {self.first}")
        print(f"  count             : {self.count}")
        print(f"  expiry             : {self._expire.describe() if self._expire else 'none'}")
        print(f"  alerts             : {len(self._alerts)}")
        print(f"  persist            : {self._persist or 'none'}")
        print(f"  detection         : {self.detection_method()}")
        if v:
            print(f"  min / max         : {min(v)} / {max(v)}")
            print(f"  average           : {self.average()}")
            print(f"  std_dev           : {self.std_dev()}")
            print(f"  delta             : {self.delta()}")
            print(f"  % change          : {self.percent_change()}%")
            print(f"  trend             : {self.trend()}")
            print(f"  is_anomaly (now)  : {self.is_anomaly()}")
            print(f"  anomaly_score     : {self.anomaly_score()}")
        if self._history:
            print(f"  first at          : {self._ts(self._history[0]).strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  last at           : {self._ts(self._history[-1]).strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 55)

    def __repr__(self) -> str:
        return (f"Temprud(name='{self._name}', now={self.now}, "
                f"count={self.count}, detection={self.detection_method()})")

    def __len__(self) -> int:
        return self.count
