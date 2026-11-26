#!/usr/bin/env python3
"""One slow seat: a coin whose funding has been crowded for days,
the 4h is already against that crowd, and price is back near the 4h average.
Does not change pullback, quiet, Snap, or the jump."""
import os
import shutil
import py_compile

TARGET = "main.py"
BACKUP = "main.py.bak_crowd"


def fail(msg):
    shutil.copy2(BACKUP, TARGET)
    print(msg)
    print(f"restored from {BACKUP} — bot file was not changed")


def put(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise RuntimeError(f"{label} count={n}")
    return text.replace(old, new, 1)


CONFIG = """
    # Crowd: one seat. Days of crowded funding, 4h already against it, price near the 4h average.
    ENABLE_CROWD = True
    CROWD_RISK = 0.0010
    CROWD_MAX_POSITIONS = 1
    CROWD_INTERVAL_HOURS = 1.0
    CROWD_UNIVERSE = 20
    CROWD_MIN_QUOTE_VOL = 8000000.0
    CROWD_FUND_DAYS = 7
    CROWD_FUND_SUM_LONG = 0.0040
    CROWD_FUND_SUM_SHORT = -0.0030
    CROWD_NEAR_PCT = 0.015
    CROWD_STOP_ATR = 1.20
    CROWD_STOP_MIN_PCT = 0.008
    CROWD_STOP_MAX_PCT = 0.025
"""

SCAN = '''
def crowd_open_count() -> int:
    n = 0
    for trades in active_trades.values():
        for tr in trades.values():
            if tr.get("crowd") and tr.get("entry_price"):
                n += 1
    return n


async def crowd_funding_sum(symbol: str):
    days = float(getattr(config, "CROWD_FUND_DAYS", 7))
    try:
        rows = await client.futures_funding_rate(symbol=symbol, limit=100)
    except Exception:
        return None
    cut = int(time.time() * 1000) - int(days * 86400 * 1000)
    rates = []
    for row in rows or []:
        try:
            ts = int(row.get("fundingTime") or 0)
            rate = float(row.get("fundingRate") or 0)
        except Exception:
            continue
        if ts >= cut:
            rates.append((ts, rate))
    if len(rates) < 6:
        return None
    rates.sort()
    vals = [rate for _, rate in rates]
    return sum(vals), vals[-3:]


async def crowd_scan_once() -> None:
    if not getattr(config, "ENABLE_CROWD", True):
        return
    if bot_paused or is_lane_in_cooldown("crowd"):
        return
    if crowd_open_count() >= int(getattr(config, "CROWD_MAX_POSITIONS", 1)):
        logger.info("📡 Crowd | seat full")
        return
    try:
        tickers = await client.futures_ticker()
    except Exception as e:
        logger.warning(f"Crowd ticker: {e}")
        return
    liquid = []
    for t in tickers or []:
        sym = t.get("symbol") or ""
        if not sym.endswith("USDT") or sym.endswith("BUSDT"):
            continue
        if sym in TRADFI_BLACKLIST or sym in _IMPULSE_STOCKS or sym in temp_notional_blacklist:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
        except Exception:
            continue
        if qv < float(getattr(config, "CROWD_MIN_QUOTE_VOL", 8000000.0)):
            continue
        liquid.append((qv, sym))
    liquid.sort(reverse=True)
    liquid = liquid[:int(getattr(config, "CROWD_UNIVERSE", 20))]
    near = float(getattr(config, "CROWD_NEAR_PCT", 0.015))
    long_min = float(getattr(config, "CROWD_FUND_SUM_LONG", 0.004))
    short_max = float(getattr(config, "CROWD_FUND_SUM_SHORT", -0.003))
    best = None
    rejected = {"fund": 0, "far": 0, "side": 0, "bars": 0, "busy": 0}
    for _, sym in liquid:
        if any(sym in trades for trades in active_trades.values()):
            rejected["busy"] += 1
            continue
        packed = await crowd_funding_sum(sym)
        if not packed:
            rejected["fund"] += 1
            continue
        total, last3 = packed
        if total >= long_min and all(x > 0 for x in last3):
            side = "SELL"
        elif total <= short_max and all(x < 0 for x in last3):
            side = "BUY"
        else:
            rejected["fund"] += 1
            continue
        try:
            k = await get_futures_klines_cached(symbol=sym, interval="4h", limit=40, closed_only=True)
        except Exception:
            k = None
        if not k or len(k) < 22:
            rejected["bars"] += 1
            continue
        c = np.array([float(x[4]) for x in k], dtype=float)
        h = np.array([float(x[2]) for x in k], dtype=float)
        l = np.array([float(x[3]) for x in k], dtype=float)
        ema = float(talib.EMA(c, 21)[-1])
        px = float(c[-1])
        if px <= 0 or ema <= 0:
            rejected["bars"] += 1
            continue
        dist = (px - ema) / px
        if abs(dist) > near:
            rejected["far"] += 1
            continue
        if side == "SELL" and dist >= 0:
            rejected["side"] += 1
            continue
        if side == "BUY" and dist <= 0:
            rejected["side"] += 1
            continue
        atr = float(talib.ATR(h, l, c, 14)[-1]) if len(c) >= 15 else 0.0
        if best is None or abs(total) > abs(best["fund"]):
            best = {"symbol": sym, "side": side, "fund": total, "px": px, "atr": atr, "dist": dist}
    if not best:
        logger.info(
            f"📡 Crowd | no trade | scanned={len(liquid)} fund={rejected['fund']} "
            f"far={rejected['far']} side={rejected['side']} bars={rejected['bars']} busy={rejected['busy']}"
        )
        return
    sym = best["symbol"]
    side = best["side"]
    px = float(best["px"])
    try:
        book = await client.futures_order_book(symbol=sym, limit=5)
        bid = float(book["bids"][0][0])
        ask = float(book["asks"][0][0])
        mid = (bid + ask) / 2.0
        if mid <= 0 or (ask - bid) / mid > 0.0015:
            logger.info(f"📡 Crowd skip {sym} | wide book")
            return
        px = mid
    except Exception as e:
        logger.info(f"📡 Crowd book {sym} | {e}")
        return
    dist_px = float(best["atr"]) * float(getattr(config, "CROWD_STOP_ATR", 1.20)) if best["atr"] else 0.0
    dist_px = min(max(dist_px, px * float(getattr(config, "CROWD_STOP_MIN_PCT", 0.008))),
                  px * float(getattr(config, "CROWD_STOP_MAX_PCT", 0.025)))
    _momentum_stop[sym] = dist_px
    mrs = strategy_by_name("MomentumRS")
    if mrs is None:
        return
    quality = {
        "tier": "B",
        "score": 70,
        "crowd": True,
        "momentum_rs": True,
        "daily_carry": False,
        "impulse": False,
        "snap": False,
        "quiet_mover": False,
        "risk_multiplier": 1.0,
    }
    logger.info(
        f"🎯 CROWD {side} {sym} | fund7={best['fund']*100:+.2f}% "
        f"dist={best['dist']*100:+.2f}% stop={dist_px/px*100:.2f}%"
    )
    await place_full_entry_order(
        mrs, sym, side, px, dist_px, "B", "MomentumRS",
        0.70, quality, None, None, 1.0,
    )


async def crowd_loop():
    """One look an hour. Its seat is not the pullback seat."""
    await asyncio.sleep(120)
    while bot_running:
        try:
            await crowd_scan_once()
        except Exception as e:
            logger.warning(f"Crowd: {e}")
        hours = float(getattr(config, "CROWD_INTERVAL_HOURS", 1.0))
        await asyncio.sleep(max(3600.0, hours * 3600.0))


'''


def main():
    if not os.path.exists(TARGET):
        print(f"STOPPED missing {TARGET}")
        return
    shutil.copy2(TARGET, BACKUP)
    text = open(TARGET, encoding="utf-8").read().replace("\r\n", "\n").replace("\r", "\n")
    if "ENABLE_CROWD" in text or "def crowd_scan_once" in text:
        fail("STOPPED crowd lane is already in this file")
        return
    try:
        text = put(
            text,
            "    DAILY_CARRY_REVIEW_HOURS = 20.0\n",
            "    DAILY_CARRY_REVIEW_HOURS = 20.0\n" + CONFIG,
            "config",
        )
        text = put(
            text,
            '    lane_entry = bool(qrep.get("daily_carry") or qrep.get("impulse") or qrep.get("momentum_rs") or qrep.get("quiet_mover"))\n',
            '    lane_entry = bool(qrep.get("daily_carry") or qrep.get("impulse") or qrep.get("momentum_rs") or qrep.get("quiet_mover") or qrep.get("crowd"))\n',
            "lane",
        )
        text = put(
            text,
            '                elif (quality_report or {}).get("snap"):\n',
            '''                elif (quality_report or {}).get("crowd"):
                    risk = float(getattr(config, "CROWD_RISK", 0.0010))
                elif (quality_report or {}).get("snap"):
''',
            "risk",
        )
        text = put(
            text,
            '''            "daily_carry": bool((quality_report or {}).get("daily_carry")),
        }
        occupied_symbols[symbol] = {"strategy": strategy_name, "side": side, "ts": now}
''',
            '''            "daily_carry": bool((quality_report or {}).get("daily_carry")),
            "crowd": bool((quality_report or {}).get("crowd")),
        }
        occupied_symbols[symbol] = {"strategy": strategy_name, "side": side, "ts": now}
''',
            "reserve",
        )
        text = put(
            text,
            '''                "daily_carry": bool((quality_report or {}).get("daily_carry")),
                "snap": bool((quality_report or {}).get("snap")),
''',
            '''                "daily_carry": bool((quality_report or {}).get("daily_carry")),
                "snap": bool((quality_report or {}).get("snap")),
                "crowd": bool((quality_report or {}).get("crowd")),
''',
            "record",
        )
        text = put(
            text,
            '''            elif (quality_report or {}).get("snap"):
                trade_label = "Snap"
            else:
                trade_label = strategy_name
''',
            '''            elif (quality_report or {}).get("snap"):
                trade_label = "Snap"
            elif (quality_report or {}).get("crowd"):
                trade_label = "Crowd"
            else:
                trade_label = strategy_name
''',
            "label",
        )
        text = put(
            text,
            '            if getattr(config, "ONE_LANE", False) and not (quality_report or {}).get("quiet_mover") and not (quality_report or {}).get("daily_carry"):\n',
            '            if getattr(config, "ONE_LANE", False) and not (quality_report or {}).get("quiet_mover") and not (quality_report or {}).get("daily_carry") and not (quality_report or {}).get("crowd"):\n',
            "seat",
        )
        text = put(
            text,
            "async def daily_carry_loop():\n",
            SCAN + "async def daily_carry_loop():\n",
            "scan",
        )
        text = put(
            text,
            "    asyncio.create_task(daily_carry_loop())\n",
            "    asyncio.create_task(daily_carry_loop())\n    asyncio.create_task(crowd_loop())\n",
            "task",
        )
    except RuntimeError as e:
        fail(f"STOPPED {e}")
        return
    open(TARGET, "w", encoding="utf-8").write(text)
    try:
        py_compile.compile(TARGET, doraise=True)
    except Exception as e:
        fail(f"STOPPED compile: {e}")
        return
    print("DONE pieces=9")
    print(f"backup={BACKUP}")


if __name__ == "__main__":
    main()
