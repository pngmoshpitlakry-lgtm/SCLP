#!/usr/bin/env python3
"""
Scanner memecoins Solana -> alertes Discord.

Modes :
  python scanner.py scan          scan + alertes instantanées (toutes les 5 min)
  python scanner.py digest 12     résumé des alertes des 12 dernières heures
  python scanner.py test          envoie un message de test sur Discord

Aucune dépendance externe (bibliothèque standard uniquement).
Sources : DexScreener, GeckoTerminal, RugCheck (API publiques, sans clé).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover
    PARIS = None

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# ---------------------------------------------------------------------------
# RÉGLAGES — c'est ici que tu ajustes la sensibilité du scanner
# ---------------------------------------------------------------------------
CFG = {
    # Filtres de base (un token en dessous est ignoré)
    "min_liquidity_usd": 15_000,
    "min_age_minutes": 10,
    "max_age_hours": 72,

    # Signal "Momentum" : ça monte fort, avec du volume, et plus d'achats que de ventes
    "mom_change_h1_pct": 40,
    "mom_volume_h1_usd": 40_000,
    "mom_buy_ratio": 0.52,

    # Signal "Pic de volume" : le volume des 5 dernières minutes explose vs la moyenne de l'heure
    "spike_volume_m5_usd": 8_000,
    "spike_factor": 3.0,
    "spike_min_change_m5_pct": 3,

    # Signal "Nouveau runner" : token jeune qui fait déjà du volume
    "new_max_age_hours": 6,
    "new_volume_h1_usd": 60_000,

    # Alerte "sortie" sur un token déjà alerté : chute depuis son plus haut
    "dump_from_peak_pct": 35,

    # Anti-spam
    "cooldown_hours": 4,
    "max_alerts_per_run": 6,
    "skip_hours_if_danger": 12,

    # Filtre RugCheck : ignorer les tokens avec un risque de niveau "danger"
    "skip_if_danger": True,
}

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"


# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------
def http(url, payload=None, timeout=20):
    headers = {"User-Agent": "memecoin-scanner/1.0", "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def get_json(url, retries=2):
    for attempt in range(retries + 1):
        try:
            return http(url)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"[warn] HTTP {e.code} sur {url}", file=sys.stderr)
            return None
        except Exception as e:  # réseau, JSON invalide, timeout...
            if attempt < retries:
                time.sleep(2)
                continue
            print(f"[warn] échec {url}: {e}", file=sys.stderr)
            return None
    return None


# ---------------------------------------------------------------------------
# Collecte
# ---------------------------------------------------------------------------
def candidate_mints():
    """Tokens Solana à examiner : nouveaux pools, trending, profils/boosts DexScreener."""
    mints = set()

    for url in (
        f"{DEX}/token-profiles/latest/v1",
        f"{DEX}/token-boosts/latest/v1",
        f"{DEX}/token-boosts/top/v1",
    ):
        data = get_json(url)
        if isinstance(data, list):
            for it in data:
                if it.get("chainId") == "solana" and it.get("tokenAddress"):
                    mints.add(it["tokenAddress"])

    for url in (
        f"{GECKO}/trending_pools?duration=1h",
        f"{GECKO}/new_pools",
        f"{GECKO}/pools?sort=h24_volume_usd_desc",
    ):
        data = get_json(url)
        for pool in (data or {}).get("data", []):
            try:
                tid = pool["relationships"]["base_token"]["data"]["id"]  # "solana_<mint>"
                mints.add(tid.split("_", 1)[1])
            except Exception:
                continue
        time.sleep(1)  # GeckoTerminal limite le débit

    return mints


def fetch_pairs(mints):
    """Retourne {mint: meilleure paire (liquidité max)} via DexScreener."""
    mints = list(mints)
    best = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = get_json(f"{DEX}/tokens/v1/solana/{','.join(chunk)}")
        if not isinstance(data, list):
            continue
        for p in data:
            base = (p.get("baseToken") or {}).get("address")
            if not base or base not in chunk:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            cur = best.get(base)
            if cur is None or liq > ((cur.get("liquidity") or {}).get("usd") or 0):
                best[base] = p
        time.sleep(0.4)
    return best


def rugcheck(mint):
    d = get_json(f"{RUGCHECK}/{mint}/report/summary", retries=1)
    if not isinstance(d, dict):
        return None
    risks = d.get("risks") or []
    return {
        "danger": [r.get("name", "?") for r in risks if r.get("level") == "danger"],
        "warn": [r.get("name", "?") for r in risks if r.get("level") == "warn"],
    }


# ---------------------------------------------------------------------------
# Signaux
# ---------------------------------------------------------------------------
def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def age_hours(pair, now_ms):
    created = pair.get("pairCreatedAt")
    if not created:
        return None
    return (now_ms - created) / 3_600_000


def evaluate(pair, now_ms, cfg=CFG):
    """Retourne (raisons, score) si le token mérite une alerte, sinon None."""
    liq = num((pair.get("liquidity") or {}).get("usd"))
    if liq < cfg["min_liquidity_usd"]:
        return None

    age = age_hours(pair, now_ms)
    if age is not None and (age * 60 < cfg["min_age_minutes"] or age > cfg["max_age_hours"]):
        return None

    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    tx1 = (pair.get("txns") or {}).get("h1") or {}
    buys, sells = num(tx1.get("buys")), num(tx1.get("sells"))
    ratio = buys / (buys + sells) if (buys + sells) else 0.0

    v1, v5 = num(vol.get("h1")), num(vol.get("m5"))
    c1, c5 = num(chg.get("h1")), num(chg.get("m5"))

    reasons = []
    if c1 >= cfg["mom_change_h1_pct"] and v1 >= cfg["mom_volume_h1_usd"] and ratio >= cfg["mom_buy_ratio"]:
        reasons.append("🚀 Momentum")
    if (v5 >= cfg["spike_volume_m5_usd"] and v1 > 0 and v5 * 12 >= cfg["spike_factor"] * v1
            and c5 >= cfg["spike_min_change_m5_pct"]):
        reasons.append("⚡ Pic de volume")
    if age is not None and age <= cfg["new_max_age_hours"] and v1 >= cfg["new_volume_h1_usd"]:
        reasons.append("🆕 Nouveau runner")

    if not reasons:
        return None
    return reasons, v1


# ---------------------------------------------------------------------------
# Mise en forme Discord
# ---------------------------------------------------------------------------
def usd(x):
    x = num(x)
    if x >= 1_000_000_000:
        return f"${x / 1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x / 1_000:.1f}K"
    return f"${x:.0f}"


def pct(x):
    return f"{num(x):+.0f}%"


def fmt_age(h):
    if h is None:
        return "?"
    if h < 1:
        return f"{int(h * 60)} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} j"


def paris_time(ts):
    t = time.gmtime(ts) if PARIS is None else None
    if PARIS is None:
        return time.strftime("%d/%m %H:%M UTC", t)
    from datetime import datetime
    return datetime.fromtimestamp(ts, PARIS).strftime("%d/%m %H:%M")


def links(pair):
    mint = pair["baseToken"]["address"]
    pa = pair.get("pairAddress", "")
    parts = [
        f"[Axiom](https://axiom.trade/meme/{pa})",
        f"[Pump.fun](https://pump.fun/coin/{mint})",
        f"[DexScreener]({pair.get('url') or 'https://dexscreener.com/solana/' + pa})",
        f"[RugCheck](https://rugcheck.xyz/tokens/{mint})",
    ]
    return " · ".join(parts)


def alert_embed(pair, reasons, rc, now_ms):
    bt = pair["baseToken"]
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    tx1 = (pair.get("txns") or {}).get("h1") or {}
    mcap = pair.get("marketCap") or pair.get("fdv")

    if rc is None:
        risk = "⚠️ RugCheck indisponible : non vérifié"
    elif rc["danger"]:
        risk = "🔴 " + ", ".join(rc["danger"][:4])
    elif rc["warn"]:
        risk = "🟠 " + ", ".join(rc["warn"][:4])
    else:
        risk = "🟢 Aucun risque signalé"

    fields = [
        {"name": "MCap", "value": usd(mcap), "inline": True},
        {"name": "Liquidité", "value": usd((pair.get("liquidity") or {}).get("usd")), "inline": True},
        {"name": "Âge", "value": fmt_age(age_hours(pair, now_ms)), "inline": True},
        {"name": "Variation", "value": f"5m {pct(chg.get('m5'))} · 1h {pct(chg.get('h1'))} · 6h {pct(chg.get('h6'))}", "inline": False},
        {"name": "Volume", "value": f"5m {usd(vol.get('m5'))} · 1h {usd(vol.get('h1'))}", "inline": True},
        {"name": "Txns 1h", "value": f"{int(num(tx1.get('buys')))} achats / {int(num(tx1.get('sells')))} ventes", "inline": True},
        {"name": "Sécurité", "value": risk, "inline": False},
        {"name": "Liens", "value": links(pair), "inline": False},
    ]
    return {
        "title": f"${bt.get('symbol', '?')} — {' + '.join(reasons)}",
        "description": f"{bt.get('name', '')}\n`{bt['address']}`",
        "color": 0x2ECC71,
        "fields": fields,
    }


def dump_embed(pair, rec, drop_pct):
    bt = pair["baseToken"]
    return {
        "title": f"📉 ${bt.get('symbol', '?')} — {drop_pct:.0f}% sous son plus haut",
        "description": (
            f"Alerté à {paris_time(rec['t'])} (Paris). Plus haut depuis l'alerte : "
            f"{rec['peak'] / rec['price0'] * 100 - 100:+.0f}% vs prix d'alerte. "
            f"Liquidité actuelle : {usd((pair.get('liquidity') or {}).get('usd'))}.\n"
            f"{links(pair)}"
        ),
        "color": 0xE74C3C,
    }


def send(embeds, content=None):
    if not embeds and not content:
        return
    if not WEBHOOK:
        print("[dry-run] DISCORD_WEBHOOK_URL absent. Contenu :")
        print(json.dumps({"content": content, "embeds": embeds}, ensure_ascii=False, indent=2))
        return
    chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)] or [[]]
    for n, chunk in enumerate(chunks):
        payload = {"embeds": chunk}
        if content and n == 0:
            payload["content"] = content
        for attempt in range(2):
            try:
                http(WEBHOOK, payload)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    time.sleep(3)
                    continue
                print(f"[erreur] Discord HTTP {e.code}: {e.read()[:200]}", file=sys.stderr)
                break
            except Exception as e:
                print(f"[erreur] Discord: {e}", file=sys.stderr)
                break
        time.sleep(1)


# ---------------------------------------------------------------------------
# État (persisté entre les exécutions)
# ---------------------------------------------------------------------------
def load_state():
    try:
        s = json.loads(STATE_FILE.read_text())
    except Exception:
        s = {}
    s.setdefault("alerts", {})
    s.setdefault("events", [])
    s.setdefault("skipped", {})
    return s


def save_state(s, now):
    s["alerts"] = {m: v for m, v in s["alerts"].items() if now - v["t"] < 48 * 3600}
    s["events"] = [e for e in s["events"] if now - e["t"] < 48 * 3600]
    s["skipped"] = {m: t for m, t in s["skipped"].items() if now - t < 24 * 3600}
    STATE_FILE.write_text(json.dumps(s))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def scan():
    state = load_state()
    now = time.time()
    now_ms = now * 1000

    mints = candidate_mints()
    tracked = {m for m, v in state["alerts"].items() if now - v["t"] < 24 * 3600}
    pairs = fetch_pairs(mints | tracked)
    print(f"[scan] {len(mints)} candidats, {len(tracked)} suivis, {len(pairs)} paires récupérées")

    candidates, dumps = [], []
    for mint, pair in pairs.items():
        price = num(pair.get("priceUsd"))
        rec = state["alerts"].get(mint)

        if rec:
            rec["peak"] = max(rec.get("peak", 0), price)
            if (not rec.get("dump_sent") and rec["peak"] > 0 and price > 0
                    and now - rec["t"] < 24 * 3600):
                drop = (1 - price / rec["peak"]) * 100
                if drop >= CFG["dump_from_peak_pct"]:
                    dumps.append(dump_embed(pair, rec, drop))
                    rec["dump_sent"] = True
            if now - rec["t"] < CFG["cooldown_hours"] * 3600:
                continue

        if now - state["skipped"].get(mint, 0) < CFG["skip_hours_if_danger"] * 3600:
            continue

        res = evaluate(pair, now_ms)
        if res:
            candidates.append((res[1], mint, pair, res[0]))

    candidates.sort(key=lambda x: x[0], reverse=True)

    embeds = []
    for _, mint, pair, reasons in candidates:
        if len(embeds) >= CFG["max_alerts_per_run"]:
            break
        rc = rugcheck(mint)
        time.sleep(0.5)
        if rc and rc["danger"] and CFG["skip_if_danger"]:
            state["skipped"][mint] = now
            print(f"[skip] {pair['baseToken'].get('symbol')} : {rc['danger']}")
            continue
        embeds.append(alert_embed(pair, reasons, rc, now_ms))
        price = num(pair.get("priceUsd"))
        state["alerts"][mint] = {
            "t": now, "symbol": pair["baseToken"].get("symbol", "?"), "price0": price,
            "peak": price, "dump_sent": False, "pair": pair.get("pairAddress", ""),
        }
        state["events"].append({
            "t": now, "mint": mint, "symbol": pair["baseToken"].get("symbol", "?"),
            "kind": " + ".join(reasons), "price0": price,
            "mcap": num(pair.get("marketCap") or pair.get("fdv")),
            "liq": num((pair.get("liquidity") or {}).get("usd")),
        })

    send(embeds + dumps)
    save_state(state, now)
    print(f"[scan] {len(embeds)} alertes, {len(dumps)} alertes de sortie")


def digest(hours):
    state = load_state()
    now = time.time()
    since = now - hours * 3600
    events = [e for e in state["events"] if e["t"] >= since]
    if not events:
        send([{
            "title": f"🗒️ Résumé des {hours} dernières heures",
            "description": "Aucun signal sur la période.",
            "color": 0x95A5A6,
        }])
        return

    first = {}
    for e in events:
        first.setdefault(e["mint"], e)
    pairs = fetch_pairs(set(first))

    rows = []
    for mint, e in first.items():
        pair = pairs.get(mint)
        price = num(pair.get("priceUsd")) if pair else 0
        perf = (price / e["price0"] - 1) * 100 if price and e["price0"] else None
        liq = num((pair.get("liquidity") or {}).get("usd")) if pair else 0
        rows.append((perf, e, liq, pair))
    rows.sort(key=lambda r: (r[0] is None, -(r[0] or 0)))

    lines = []
    for perf, e, liq, pair in rows[:15]:
        perf_txt = f"{perf:+.0f}%" if perf is not None else "n/a"
        mark = "🟢" if (perf or 0) > 0 else "🔴"
        link = f"[Axiom](https://axiom.trade/meme/{pair['pairAddress']})" if pair else ""
        lines.append(
            f"{mark} **${e['symbol']}** alerté {paris_time(e['t'])} à MCap {usd(e['mcap'])} "
            f"→ **{perf_txt}** depuis l'alerte · liq {usd(liq)} {link}"
        )

    winners = sum(1 for r in rows if (r[0] or 0) > 0)
    send([{
        "title": f"🗒️ Résumé des {hours} dernières heures",
        "description": (
            f"{len(rows)} tokens alertés, {winners} dans le vert depuis l'alerte.\n\n" + "\n".join(lines)
        )[:4000],
        "color": 0x3498DB,
    }])


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "scan":
        scan()
    elif mode == "digest":
        digest(int(sys.argv[2]) if len(sys.argv) > 2 else 12)
    elif mode == "test":
        send([{"title": "✅ Scanner connecté", "description": "Le webhook Discord fonctionne.", "color": 0x2ECC71}])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
