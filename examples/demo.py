# temprud demo - run through the main features
# From repo root:  python -m examples.demo   or  pip install -e . && python examples/demo.py

import time
import random
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from temprud import Temprud, TemprudAlert, TemprudExpiry


def sep(title: str, w: int = 58) -> None:
    print(f"\n{'-' * w}\n  {title}\n{'-' * w}")


# --- 1. Basic: history + native operators ---
sep("1. Basic  - assign and query")

price = Temprud(100, name="price")
price.set(105)
price.set(98)
price.set(110)
price.set(107)

print(f"now      : {price.now}")
print(f"first    : {price.first}")
print(f"max()    : {price.max()}")
print(f"min()    : {price.min()}")
print(f"average(): {price.average()}")
print(f"delta()  : {price.delta()}")
print(f"% change : {price.percent_change()}%")
print(f"trend()  : {price.trend()}")


# --- 2. Expiry: only keep recent history ---
sep("2. Expiry  - keep last N time or N entries")

temp = Temprud(name="short_lived", expire=TemprudExpiry(seconds=5))
for i in range(5):
    temp.set(round(20 + random.uniform(-1, 1), 1))
    time.sleep(0.3)
print(f"After 5 values: count = {temp.count}")
print("Waiting 6s for expiry...")
time.sleep(6)
temp.set(21.0)
print(f"After expiry: count = {temp.count}, now = {temp.now}")

capped = Temprud(name="capped", expire=TemprudExpiry(max_entries=5))
for i in range(10):
    capped.set(i * 10)
print(f"max_entries=5, added 10: count = {capped.count}, history = {[v for _, v in capped.history]}")


# --- 3. Persist: survive restarts ---
sep("3. Persist  - save/load history")

SAVE_FILE = "demo_history.json"
saved_var = Temprud(initial_value=1000, name="persistent_price", persist=SAVE_FILE)
saved_var.set(1050)
saved_var.set(1100)
saved_var.set(980)
print(f"Saved {saved_var.count} entries to {SAVE_FILE}")

print("Simulating restart  - load from file...")
reloaded = Temprud(name="persistent_price_reloaded", persist=SAVE_FILE)
print(f"Loaded {reloaded.count} entries. Values: {[v for _, v in reloaded.history]}, now = {reloaded.now}")
if os.path.exists(SAVE_FILE):
    os.remove(SAVE_FILE)


# --- 4. Alerts: print ---
sep("4. Alerts (print)  - anomaly, threshold, percent change")

stock = Temprud(
    name="ACME_stock",
    alerts=[
        TemprudAlert.on_anomaly(notify="print", cooldown_seconds=5, label="ANOMALY"),
        TemprudAlert.on_threshold(above=500, notify="print", cooldown_seconds=5, label="HIGH"),
        TemprudAlert.on_change(percent=10, notify="print", cooldown_seconds=5, label="BIG_MOVE"),
    ]
)
print("Feeding 40 normal values (100–120)...")
for _ in range(40):
    stock.set(round(random.uniform(100, 120), 2))
print(f"Detection: {stock.detection_method()}\nInjecting triggers...\n")
stock.set(550)
time.sleep(0.5)
stock.set(5)
time.sleep(0.5)
current = stock.now
stock.set(round(current * 1.15, 2))


# --- 5. Alerts: sound ---
sep("5. Alerts (sound)  - beep on anomaly")

sensor = Temprud(
    name="temperature_sensor",
    alerts=[TemprudAlert.on_anomaly(notify="sound", cooldown_seconds=5, label="TEMP_SPIKE")]
)
for _ in range(40):
    sensor.set(round(random.uniform(22, 24), 1))
print("Normal 22–24°C. Injecting 95°C... (beep)")
sensor.set(95.0)
time.sleep(1)
print(f"anomaly_score = {sensor.anomaly_score()}")


# --- 6. Custom alert callback ---
sep("6. Custom alert  - your function")

def on_alert(name, value, score, timestamp, message):
    print(f"  [ALERT] {name} = {value} @ {timestamp} (score={score})")

orders = Temprud(name="daily_orders", alerts=[TemprudAlert.on_anomaly(notify=on_alert, cooldown_seconds=5)])
for _ in range(40):
    orders.set(random.randint(90, 110))
print("Normal 90–110. Injecting 3...")
orders.set(3)


# --- 7. ML vs statistical detection ---
sep("7. ML vs statistical anomaly detection")

ml_var = Temprud(name="ml_var", ml=True)
stat_var = Temprud(name="stat_var", ml=False)
for v in [random.uniform(50, 55) for _ in range(50)]:
    ml_var.set(round(v, 2))
    stat_var.set(round(v, 2))
print("50 values in [50,55]. Then 52 (normal) and 200 (anomaly):")
ml_var.set(52.0)
stat_var.set(52.0)
print(f"  52: ml is_anomaly={ml_var.is_anomaly()}, stat={stat_var.is_anomaly()}")
ml_var.set(200.0)
stat_var.set(200.0)
print(f"  200: ml is_anomaly={ml_var.is_anomaly()}, stat={stat_var.is_anomaly()}")


# --- 8. Full setup ---
sep("8. Full  - expiry + persist + alerts")

FULL_SAVE = "full_demo.json"
btc = Temprud(
    name="bitcoin_full",
    expire=TemprudExpiry(days=7),
    persist=FULL_SAVE,
    alerts=[
        TemprudAlert.on_anomaly(notify="print", cooldown_seconds=5, label="BTC_ANOMALY"),
        TemprudAlert.on_threshold(above=75000, notify="print", cooldown_seconds=5, label="ATH"),
    ]
)
for p in [60000 + random.uniform(-2000, 2000) for _ in range(50)]:
    btc.set(round(p, 2))
btc.set(20000)
time.sleep(0.3)
btc.set(90000)
time.sleep(0.3)
btc.summary()
if os.path.exists(FULL_SAVE):
    os.remove(FULL_SAVE)


# --- 9. Alert management ---
sep("9. Alert management")

mgmt = Temprud(name="managed")
mgmt.add_alert(TemprudAlert.on_anomaly(notify="print", label="A1"))
mgmt.add_alert(TemprudAlert.on_threshold(above=100, notify="print", label="A2"))
mgmt.list_alerts()
mgmt.remove_alerts()
mgmt.list_alerts()


sep("Done")
print("""
Covered: history, .now / .first, .at() / .since() / .between(), .max() / .min() / .average(),
expiry, persist, alerts (print/sound/custom), ML vs stat anomaly, full setup, alert add/remove.
""")
