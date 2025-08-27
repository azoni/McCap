import json, asyncio, time
from dataclasses import asdict
from typing import List
from .config import REM_FILE, PAY_FILE, ALERTS_FILE
from .logging_setup import log
from .models import Reminder, Invoice, AlertEvent

# In-memory
reminders: List[Reminder] = []
invoices:  List[Invoice]  = []
alert_events: List[AlertEvent] = []

# Locks
REM_LOCK    = asyncio.Lock()
PAY_LOCK    = asyncio.Lock()
ALERTS_LOCK = asyncio.Lock()

# ---- Reminders ----
async def save_reminders():
    async with REM_LOCK:
        with open(REM_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in reminders], f, ensure_ascii=False, indent=2)

async def load_reminders():
    try:
        with open(REM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        reminders.clear()
        for it in data: reminders.append(Reminder(**it))
        log.info(f"Loaded {len(reminders)} reminder(s)")
    except FileNotFoundError:
        log.info("No reminders file found; starting fresh.")
    except Exception:
        log.exception("Failed to load reminders.json")

# ---- Invoices ----
async def save_invoices():
    async with PAY_LOCK:
        with open(PAY_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(i) for i in invoices], f, ensure_ascii=False, indent=2)

async def load_invoices():
    try:
        with open(PAY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        invoices.clear()
        for it in data: invoices.append(Invoice(**it))
        cutoff = time.time() - 7*24*3600
        invoices[:] = [i for i in invoices if not (i.status in ("paid","expired") and i.created_ts < cutoff)]
        log.info(f"Loaded {len(invoices)} payment record(s)")
    except FileNotFoundError:
        log.info("No payments file found; starting fresh.")
    except Exception:
        log.exception("Failed to load payments.json")

# ---- Alerts ----
async def save_alerts():
    async with ALERTS_LOCK:
        with open(ALERTS_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(a) for a in alert_events], f, ensure_ascii=False, indent=2)

async def load_alerts():
    try:
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        alert_events.clear()
        for it in data: alert_events.append(AlertEvent(**it))
        alert_events[:] = sorted(alert_events, key=lambda x: x.ts, reverse=True)[:1000]
        log.info(f"Loaded {len(alert_events)} alert event(s)")
    except FileNotFoundError:
        log.info("No alerts file found; starting fresh.")
    except Exception:
        log.exception("Failed to load alerts.json")
