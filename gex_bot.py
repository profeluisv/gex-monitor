# GEX MONITOR v17 - GitHub Actions + Telegram (SPX OI + Volumen)
import re, sys, time
from datetime import datetime, date, timedelta
from statistics import NormalDist
import numpy as np
import pandas as pd
import requests
from curl_cffi import requests as cffi_requests

TICKERS = ["SPX", "SPY", "QQQ", "GLD", "USO", "IBIT"]
TARGET_DTE = 1
TOKEN = "8624586813:AAE3Vvh-ic3WIddMJI-b4X2QF6Ii8AP2VD4"
CHAT_ID = "@gex_monitor"
ENVIAR_SOLO_EN_SESION = True

NYSE_TZ = "America/New_York"
MULT_CONTRATO = 100
PCT_MOV = 0.01
DATO_MAX_RETRASO_MIN = 45
IV_MAX = 3.0
CBOE_SYMBOL = {"SPX": "_SPX"}
CBOE_ROOTS = {"SPX": {"SPX", "SPXW"}}
OCC_REGEX = re.compile(r"^(?P<root>[A-Z]{1,6}\d?)(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
HEADERS = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
           "Origin": "https://www.cboe.com", "Referer": "https://www.cboe.com/"}
_SESSION = cffi_requests.Session(impersonate="chrome")
_INV = NormalDist().inv_cdf
_FER = {}

def log(msg):
    print(f"[{pd.Timestamp.now(tz=NYSE_TZ):%Y-%m-%d %H:%M:%S} NY] {msg}", flush=True)

def _nth(y, m, wd, n):
    if n > 0:
        d = date(y, m, 1)
        return d + timedelta(days=(wd - d.weekday()) % 7 + 7 * (n - 1))
    last = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return last - timedelta(days=(last.weekday() - wd) % 7)

def _obs(d):
    return d - timedelta(days=1) if d.weekday() == 5 else (d + timedelta(days=1) if d.weekday() == 6 else d)

def _gf(y):
    a, (b, c) = y % 19, divmod(y, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    x = h + l - 7 * m + 114
    return date(y, x // 31, (x % 31) + 1) - timedelta(days=2)

def _feriados(y):
    if y not in _FER:
        nyd = date(y, 1, 1)
        s = {_nth(y, 1, 0, 3), _nth(y, 2, 0, 3), _gf(y), _nth(y, 5, 0, -1),
             _obs(date(y, 7, 4)), _nth(y, 9, 0, 1), _nth(y, 11, 3, 4), _obs(date(y, 12, 25))}
        if nyd.weekday() == 6: s.add(date(y, 1, 2))
        elif nyd.weekday() != 5: s.add(nyd)
        if y >= 2022: s.add(_obs(date(y, 6, 19)))
        _FER[y] = s
    return _FER[y]

def _es_nyse(d): return d.weekday() < 5 and d not in _feriados(d.year)
def _cierre_hm(d): return (13, 0) if d in (_nth(d.year, 11, 3, 4) + timedelta(days=1), date(d.year, 7, 3), date(d.year, 12, 24)) else (16, 0)

def _valid_days(a, b):
    c, n = a, 0
    while c <= b:
        if _es_nyse(c): n += 1
        c += timedelta(days=1)
    return n

def _phi(x): return np.exp(-0.5 * x * x) / np.sqrt(2.0 * np.pi)
def _fm(x):
    a = abs(x)
    return f"{x / 1e6:+.1f}M" if a >= 1e6 else (f"{x / 1e3:+.0f}K" if a >= 1e3 else f"{x:+.0f}")
def _txt_ret(m): return f"{m:.0f} min" if m < 120 else (f"{m / 60:.1f} h" if m < 2880 else f"{m / 1440:.1f} días")
def _num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None

def descargar_cadena(ticker, reintentos=3):
    sim = CBOE_SYMBOL.get(ticker.upper(), ticker.upper())
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sim}.json"
    err = None
    for i in range(reintentos):
        try:
            r = _SESSION.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 403: raise ValueError(f"403 Forbidden ({r.url})")
            r.raise_for_status()
            p = r.json()
            p["data"]["current_price"]; p["data"]["options"]
            return p
        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))
    log(f"Error con {ticker}: {err}")
    return None

def horario_hoy(ahora):
    ahora = pd.Timestamp(ahora)
    if ahora.tzinfo is None: ahora = ahora.tz_localize(NYSE_TZ)
    dia = ahora.tz_convert(NYSE_TZ).date()
    if not _es_nyse(dia): return ahora, None, None
    ch, cm = _cierre_hm(dia)
    return ahora, pd.Timestamp(datetime(dia.year, dia.month, dia.day, 9, 30), tz=NYSE_TZ), pd.Timestamp(datetime(dia.year, dia.month, dia.day, ch, cm), tz=NYSE_TZ)

def estado_sesion(ahora):
    ahora, abre, cierra = horario_hoy(ahora)
    if abre is None or ahora >= cierra: return "cierre"
    return "pre" if ahora < abre else "sesion"

def hora_dato(payload):
    tr = [t for t in (o.get("last_trade_time") for o in payload["data"]["options"]) if isinstance(t, str) and t.strip()]
    cand = ([(max(tr), NYSE_TZ)] if tr else []) + [(payload.get("timestamp"), "UTC"), (payload["data"].get("last_trade_time"), "UTC")]
    for valor, zona in cand:
        try: t = pd.Timestamp(valor)
        except (ValueError, TypeError): continue
        if not pd.isna(t): return (t.tz_localize(zona) if t.tzinfo is None else t).tz_convert(NYSE_TZ)
    return None

def subyacente_implicito(spot, K, GRP, SG, BID, ASK):
    q = {(k, g, sg > 0): ((a + b) / 2.0, a - b) for k, g, sg, b, a in zip(K, GRP, SG, BID, ASK) if np.isfinite(b) and np.isfinite(a) and b > 0 and a >= b}
    est = [(abs(k - spot), sc + q[(k, g, False)][1], k + mc - q[(k, g, False)][0])
           for (k, g, call), (mc, sc) in q.items() if call and (k, g, False) in q]
    if len(est) < 3: return None
    cerca = sorted(sorted(est, key=lambda x: x[0])[:20], key=lambda x: x[1])[:5]
    return float(np.median([x[2] for x in cerca]))

def calibrar_t(s, iv, g, d, es_call):
    ok = np.isfinite(g) & np.isfinite(d) & (g > 0) & np.where(es_call, (d > 0) & (d < 1), (d < 0) & (d > -1))
    if ok.sum() < 3: return None
    iv, g, d, es_call = iv[ok], g[ok], d[ok], es_call[ok]
    top = g >= 0.5 * g.max()
    d1 = np.array([_INV(x if c else x + 1.0) for x, c in zip(d[top], es_call[top])])
    t = (_phi(d1) / (s * iv[top] * g[top])) ** 2
    t = t[np.isfinite(t) & (t > 0)]
    return float(np.median(t)) if len(t) else None

def iv_otm(K, GRP, SG, IV, s_ref):
    ps = {}
    for k, g, sg, iv in zip(K, GRP, SG, IV): ps.setdefault((k, g), {})[sg > 0] = iv
    return np.array([ps[(k, g)][True if k >= s_ref else False] if (True in ps[(k, g)] and False in ps[(k, g)]) else ps[(k, g)][sg > 0] for k, g, sg in zip(K, GRP, SG)])

def _muros(ks, idx_k, es_c, gex_vec):
    gc, gneg = np.zeros(len(ks)), np.zeros(len(ks))
    np.add.at(gc, idx_k[es_c], gex_vec[es_c]); np.add.at(gneg, idx_k[~es_c], gex_vec[~es_c])
    gp = -gneg
    cw = pw = None
    if gc.max() > 0 and gp.max() > 0:
        ic, ip = int(np.argmax(gc)), int(np.argmax(gp))
        if ks[ic] > ks[ip]: cw, pw = float(ks[ic]), float(ks[ip])
        else:
            mejor = None
            mp = ks < ks[ic]
            if mp.any() and gp[mp].max() > 0:
                j = int(np.flatnonzero(mp)[np.argmax(gp[mp])])
                mejor = (gc[ic] + gp[j], float(ks[ic]), float(ks[j]))
            mc = ks > ks[ip]
            if mc.any() and gc[mc].max() > 0:
                j = int(np.flatnonzero(mc)[np.argmax(gc[mc])])
                cand = (gc[j] + gp[ip], float(ks[j]), float(ks[ip]))
                if mejor is None or cand[0] > mejor[0]: mejor = cand
            if mejor is not None: cw, pw = mejor[1], mejor[2]
    else:
        cw = float(ks[np.argmax(gc)]) if gc.max() > 0 else None
        pw = float(ks[np.argmax(gp)]) if gp.max() > 0 else None
    ncw = float(gc[int(np.searchsorted(ks, cw))] - gp[int(np.searchsorted(ks, cw))]) if cw is not None else None
    npw = float(gc[int(np.searchsorted(ks, pw))] - gp[int(np.searchsorted(ks, pw))]) if pw is not None else None
    return cw, pw, ncw, npw, float(gex_vec.sum())

def calcular_gex(ticker, target_dte=TARGET_DTE, payload=None, ahora=None):
    ticker = ticker.upper()
    payload = descargar_cadena(ticker) if payload is None else payload
    if payload is None: return None
    spot, options = _num(payload["data"]["current_price"]), payload["data"]["options"]
    if spot is None or spot <= 0 or not options: return None

    ahora = ahora if ahora is not None else pd.Timestamp.now(tz=NYSE_TZ)
    hoy = ahora.date()
    _, abre, _ = horario_hoy(ahora)
    ts_dato = hora_dato(payload)
    raices = CBOE_ROOTS.get(ticker, {ticker})
    exp_cache, parsed = {}, []

    for opt in options:
        m = OCC_REGEX.match(str(opt.get("option", "")))
        if not m or m.group("root") not in raices: continue
        try: exp = datetime.strptime(m.group("date"), "%y%m%d").date()
        except ValueError: continue
        if exp < hoy: continue
        if exp not in exp_cache: exp_cache[exp] = max(0, _valid_days(hoy, exp) - 1)
        iv, oi_r, vol_r = _num(opt.get("iv")), _num(opt.get("open_interest")), _num(opt.get("volume"))
        oi = 0.0 if (oi_r is None or oi_r < 0) else oi_r
        vol = 0.0 if (vol_r is None or vol_r < 0) else vol_r
        if iv is None or not (0 < iv <= IV_MAX) or (oi <= 0 and vol <= 0): continue
        g, d, b, a = _num(opt.get("gamma")), _num(opt.get("delta")), _num(opt.get("bid")), _num(opt.get("ask"))
        parsed.append({"exp": exp, "root": m.group("root"), "dte": exp_cache[exp], "call": m.group("cp") == "C",
                       "strike": int(m.group("strike")) / 1000.0, "iv": iv, "oi": oi, "vol": vol,
                       "g": np.nan if g is None else g, "d": np.nan if d is None else d,
                       "bid": np.nan if b is None else b, "ask": np.nan if a is None else a})
    if not parsed: return None

    dtes = sorted({p["dte"] for p in parsed})
    eleg = [x for x in dtes if x <= target_dte] or [min(dtes, key=lambda x: (abs(x - target_dte), x))]
    sel = [p for p in parsed if p["dte"] in eleg]
    if not any(p["call"] for p in sel) or all(p["call"] for p in sel): return None
    d0, d1 = min(p["dte"] for p in sel), max(p["dte"] for p in sel)
    rango_dte = f"{d0}" if d0 == d1 else f"{d0}-{d1}"

    K = np.array([p["strike"] for p in sel]); IV = np.array([p["iv"] for p in sel])
    OI = np.array([p["oi"] for p in sel]); VOL = np.array([p["vol"] for p in sel])
    SG = np.array([1.0 if p["call"] else -1.0 for p in sel]); DTE = np.array([p["dte"] for p in sel])
    GV = np.array([p["g"] for p in sel]); DV = np.array([p["d"] for p in sel])
    BID = np.array([p["bid"] for p in sel]); ASK = np.array([p["ask"] for p in sel])
    grps = sorted({(p["exp"].toordinal(), p["root"]) for p in sel})
    gid = {g: i for i, g in enumerate(grps)}
    GRP = np.array([gid[(p["exp"].toordinal(), p["root"])] for p in sel])

    s_ref = subyacente_implicito(spot, K, GRP, SG, BID, ASK)
    if s_ref is None or not np.isfinite(s_ref) or abs(s_ref / spot - 1) > 0.05: s_ref = spot
    IVU = iv_otm(K, GRP, SG, IV, s_ref)
    T_map = {}
    for gi in np.unique(GRP):
        mk = GRP == gi
        t = calibrar_t(s_ref, IVU[mk], GV[mk], DV[mk], SG[mk] > 0)
        T_map[gi] = t if (t is not None and 1e-6 < t < 0.1) else max(int(DTE[mk][0]), 0.5) / 252.0
    S_ = IVU * np.sqrt(np.array([T_map[gi] for gi in GRP]))
    if not (np.isfinite(S_).all() and (S_ > 0).all()): return None

    def gamma_modelo(precio):
        p = np.asarray(precio, dtype=float)
        d1n = np.log(p[:, None] / K[None, :]) / S_[None, :] + 0.5 * S_[None, :]
        return _phi(d1n) / (p[:, None] * S_[None, :])

    def curva(precios):
        p = np.asarray(precios, dtype=float)
        return (gamma_modelo(p) * (OI * SG * MULT_CONTRATO)[None, :]).sum(axis=1) * p ** 2 * PCT_MOV

    gex_base = gamma_modelo([s_ref])[0] * SG * (MULT_CONTRATO * s_ref ** 2 * PCT_MOV)
    ks = np.unique(K); idx_k = np.searchsorted(ks, K); es_c = SG > 0
    cw, pw, ncw, npw, net_gex = _muros(ks, idx_k, es_c, gex_base * OI)

    vol_data = None
    if ticker == "SPX" and VOL.sum() > 0:
        vcw, vpw, vncw, vnpw, vnet = _muros(ks, idx_k, es_c, gex_base * VOL)
        vol_data = {"cw": vcw, "pw": vpw, "ncw": vncw, "npw": vnpw, "net": vnet}

    flip = None
    if len(ks) > 1:
        v = curva(ks); a, b = v[:-1], v[1:]
        raices = []
        for i in np.where(((a < 0) & (b >= 0)) | ((a > 0) & (b <= 0)))[0]:
            lo, hi, flo = ks[i], ks[i + 1], v[i]
            for _ in range(60):
                mid = 0.5 * (lo + hi); fm = curva([mid])[0]
                if fm == 0: lo = hi = mid; break
                if fm * flo > 0: lo, flo = mid, fm
                else: hi = mid
            raices.append(0.5 * (lo + hi))
        if raices: flip = float(min(raices, key=lambda x: abs(x - spot)))

    retraso = (pd.Timestamp(ahora).tz_convert(NYSE_TZ) - ts_dato).total_seconds() / 60 if ts_dato is not None else None
    fresco = retraso is not None and retraso <= DATO_MAX_RETRASO_MIN and (abre is None or ts_dato >= abre)
    return {"ticker": ticker, "dte": rango_dte, "spot": spot, "cw": cw, "pw": pw, "ncw": ncw, "npw": npw,
            "flip": flip, "net_gex": net_gex, "vol_data": vol_data, "retraso": retraso, "fresco": fresco}

def _fmt_m(p, net, es_c):
    if p is None: return "N/A", False
    disp = net is not None and ((es_c and net < 0) or (not es_c and net > 0))
    return f"${p:.2f}" + (f" ({_fm(net)})" if net is not None else "") + (" ⚠️" if disp else ""), disp

def _ico(x): return "🟢" if x > 0 else ("🔴" if x < 0 else "⚪")
def _reg(x): return "🟢 Gamma positivo" if x > 0 else ("🔴 Gamma negativo" if x < 0 else "⚪ Gamma neutro")

def formatear_mensaje(res_list, ahora_ny):
    lineas, hay_disp = ["<b>GEX Monitor v17</b>", f"Día: {ahora_ny:%Y-%m-%d}", f"Hora: {ahora_ny:%H:%M} NY", ""], False
    for r in res_list:
        cw, dc = _fmt_m(r["cw"], r["ncw"], True)
        pw, dp = _fmt_m(r["pw"], r["npw"], False)
        fl = f"${r['flip']:.2f}" if r["flip"] is not None else "N/A"
        hay_disp = hay_disp or dc or dp
        if r["ticker"] == "SPX":
            vd = r["vol_data"]
            if vd:
                vcw, dvc = _fmt_m(vd["cw"], vd["ncw"], True)
                vpw, dvp = _fmt_m(vd["pw"], vd["npw"], False)
                vnet = f"{_fm(vd['net'])} {_ico(vd['net'])}"
                hay_disp = hay_disp or dvc or dvp
            else:
                vcw = vpw = vnet = "N/A"
            b = (f"<b>Ticker: {r['ticker']}</b>\nDTE: {r['dte']}\nSpot: ${r['spot']:.2f}\n"
                 f"🟩 Call wall:\nOI: {cw}\nVolumen: {vcw}\n🟥 Put wall:\nOI: {pw}\nVolumen: {vpw}\n"
                 f"Flip: {fl}\nNet GEX:\nOI: {_fm(r['net_gex'])} {_ico(r['net_gex'])}\nVolumen: {vnet}\n")
        else:
            b = (f"<b>Ticker: {r['ticker']}</b>\nDTE: {r['dte']}\nSpot: ${r['spot']:.2f}\n"
                 f"Call wall: {cw}\nPut wall: {pw}\nFlip: {fl}\nNet GEX: {_fm(r['net_gex'])}\n{_reg(r['net_gex'])}\n")
        if not r["fresco"]:
            b += f"⚠️ Dato no actualizado (retraso {_txt_ret(r['retraso']) if r['retraso'] is not None else '?'})\n"
        lineas.append(b)
    if hay_disp: lineas.append("⚠️ Muro disputado: en ese strike domina el lado contrario.")
    return "\n".join(lineas).strip()

def enviar_telegram(msg, reintentos=3):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for parte in [msg[i:i + 4000] for i in range(0, len(msg), 4000)]:
        for i in range(reintentos):
            try:
                r = requests.post(url, json={"chat_id": CHAT_ID, "text": parte, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                log(f"Error Telegram ({i + 1}): {e}")
                if i == reintentos - 1: return False
                time.sleep(2 * (i + 1))
    return True

def main():
    forzar = "--forzar" in sys.argv
    ahora_ny = pd.Timestamp.now(tz=NYSE_TZ)
    estado = estado_sesion(ahora_ny)
    if ENVIAR_SOLO_EN_SESION and estado != "sesion" and not forzar:
        log(f"Sesión NY en estado '{estado}'. Se omite envío.")
        return
    resultados = []
    for i, t in enumerate(TICKERS):
        res = calcular_gex(t, TARGET_DTE, ahora=ahora_ny)
        if res is not None: resultados.append(res)
        if i < len(TICKERS) - 1: time.sleep(1.5)
    if not resultados:
        log("Sin resultados válidos de Cboe."); sys.exit(1)
    if not forzar:
        frescos = [r for r in resultados if r["fresco"]]
        if not frescos:
            log("Datos de Cboe no actualizados. Se omite envío."); return
        resultados = frescos
    msg = formatear_mensaje(resultados, ahora_ny)
    if msg and enviar_telegram(msg):
        log(f"Reporte enviado a {CHAT_ID} ({len(resultados)} tickers).")
    else:
        log("No se pudo enviar reporte a Telegram."); sys.exit(1)

if __name__ == "__main__":
    main()
