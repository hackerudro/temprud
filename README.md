# temprud

**Native query operators on variable history.**

In most languages, a variable is just “what it is right now.” You assign, the old value is gone. Want “what was `x` two hours ago?” or “when did `x` hit its max?” you bolt on logging, event stores, audit tables, or time series DBs. Time gets pushed outside the language.

temprud is a different take: the variable keeps its own history and exposes it through **query operators**. Time becomes a first-class dimension of program state.

---

## The idea

```python
from temprud import Temprud

price = Temprud(100, name="price")
price.set(150)
price.set(90)
```

You still do `price.set(value)`. But now you can ask:

| Question | Operator |
|----------|----------|
| What is it now? | `price.now` |
| What was it at some time? | `price.at("2 hours ago")` |
| All values in a range? | `price.between("yesterday", "now")`, `price.since("7 days")` |
| Max / min / average (full or window)? | `price.max()`, `price.min()`, `price.average()`, `price.average("3 days")` |
| How much did it change? | `price.delta()`, `price.percent_change()` |
| Direction? | `price.trend()` → `"increasing"` / `"decreasing"` / `"stable"` |
| Previous value? | `price.rollback(1)` |

No separate logging layer, the variable *is* the history.

---

## Install

```bash
pip install scikit-learn   # optional but recommended (ML anomaly detection)
```

Clone or drop the `temprud` package into your project, or from repo root:

```bash
pip install -e .
```

---

## Quick start

```python
from temprud import Temprud, TemprudExpiry, TemprudAlert

# Basic: history + queries
price = Temprud(100, name="price")
price.set(105)
price.set(98)
print(price.now)           # 98
print(price.max())        # 105
print(price.at("1 hour ago"))  # closest value to that time

# Optional: keep only last 30 days
price = Temprud(100, name="price", expire=TemprudExpiry(days=30))

# Optional: persist to JSON (survives restarts)
price = Temprud(100, name="price", persist="price.json")

# Optional: alerts (anomaly, threshold, % change, or custom)
price = Temprud(
    name="btc",
    expire=TemprudExpiry(days=30),
    persist="btc.json",
    alerts=[
        TemprudAlert.on_anomaly(notify="print"),
        TemprudAlert.on_threshold(above=70_000, notify="sound"),
    ]
)
price.set(60_000)
price.set(85_000)   # anomaly + threshold can fire
```

---

## Why this exists

Lots of domains are about *how things change over time*:

- **Finance** — prices, balances, trades  
- **Health** — vitals, dosages, lab results  
- **IoT** — sensor streams, device metrics  
- **Debugging** — “when did this variable go wrong?”

Usually we externalize that into logs, event sourcing, or time-series DBs. temprud explores keeping history inside the variable and querying it with a small set of operators. Same idea as “temporal variables” or “time-aware state” here it’s just one package and a direct API.

**Principle:** time is a queryable dimension of state. The variable isn’t only “current value”; it’s “current value + history you can query.”

---

## API at a glance

- **State:** `set(value)`, `now`, `first`, `history`, `count`
- **Time queries:** `at(time_str)`, `between(start, end)`, `since(time_str)`  
  Time strings: `"now"`, `"6 hours ago"`, `"2024-01-15 09:30:00"`, etc.
- **Aggregates:** `max(window?)`, `min(window?)`, `average(window?)`, `std_dev(window?)`, `delta(window?)`, `percent_change(window?)`, `trend(window?)`
- **History:** `rollback(steps)`, `was_above(threshold)`, `was_below(threshold)`
- **Anomaly:** `is_anomaly()`, `anomaly_score()`, `anomaly_history()`, `detection_method()`  
  Uses Isolation Forest when `scikit-learn` is installed, else z-score.
- **Relation:** `correlates_with(other_temprud, lag_seconds=0)`
- **Alerts:** `TemprudAlert.on_anomaly`, `on_threshold`, `on_change`, `on_custom`  
  `notify`: `"print"` \| `"sound"` \| `"email"` \| any callable.
- **Expiry:** `TemprudExpiry(days=30)`, `TemprudExpiry(max_entries=1000)`
- **Persistence:** `persist="path.json"`, or `save(path)` / `load(path)`
- **Registry:** `get_all()` → dict of all registered variables by name

---

## Run the demo

From repo root:

```bash
python -m examples.demo
```

or, after `pip install -e .`:

```bash
python examples/demo.py
```

---

## License

MIT.

**Author:** [hackerudro](https://github.com/hackerudro)
