"""Fetch Korean and US market data and rebuild the JSON files in data/.

Korea: etoday.co.kr (KRX closing prices for every KOSPI/KOSDAQ stock).
US: stockanalysis.com (Nasdaq-100 constituents and QQQ/SPY/SOXX/EWY).

Run with no arguments. Use --kr-only or --us-only to run one side.
On the first run (no Korean rows yet) it backfills about 125 trading days.
"""
import datetime as dt
import json
import os
import re
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)

S = requests.Session()
S.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
})

# Real closes used to anchor the estimated index levels (first run only).
ANCHOR = {'코스피': ('2026-11-20', 4869.96), '코스닥': ('2026-11-20', 645.68)}
KEEP_DAYS = 260


def log(*a):
    print(*a, flush=True)


def get(url, tries=4, kind='text'):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, timeout=20)
            if r.status_code == 200:
                return r.json() if kind == 'json' else r.text
            last = 'HTTP %s' % r.status_code
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError('%s failed: %s' % (url, last))


def load(name, default):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def save(name, obj):
    p = os.path.join(DATA, name)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))


# ---------------------------------------------------------------- Korea

ET = 'https://www.etoday.co.kr/market/module/'
ROW = re.compile(r'<tr>(.*?)</tr>', re.S)
TD = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
TAG = re.compile(r'<[^>]+>')


def txt(h):
    return re.sub(r'\s+', ' ', TAG.sub(' ', h)).strip()


def num(s):
    s = s.replace(',', '').strip()
    return float(s) if s not in ('', '-') else None


def pct(s):
    s = s.replace('%', '').replace(' ', '')
    if s in ('', '-'):
        return 0.0
    return float(s)


def parse_cap(html):
    out = []
    for tr in ROW.findall(html):
        tds = TD.findall(tr)
        if len(tds) < 8:
            continue
        m = re.search(r'varIssCd=(\w+)', tds[1])
        if not m:
            continue
        sh = num(txt(tds[6]))
        out.append({'code': m.group(1), 'name': txt(tds[1]), 'shares': (sh or 0) * 1000,
                    'close': num(txt(tds[2])), 'pct': pct(txt(tds[4])), 'vol': num(txt(tds[5])) or 0.0})
    return out


def list_stamp(html):
    """(date, time) of the list page; time is '99:99' once it says 장종료 (closed)."""
    m = re.search(r'class="time_(live|end)">[^<]*</span>\s*(\d{4})\.(\d\d)\.(\d\d)(?:\s+(\d\d):(\d\d))?', html)
    if not m:
        return None, None
    kind, y, mo, d, hh, mm = m.groups()
    t = '99:99' if kind == 'end' else '%s:%s' % (hh or '00', mm or '00')
    return '%s-%s-%s' % (y, mo, d), t


def parse_day(html):
    out = []
    for tr in ROW.findall(html):
        tds = [txt(x) for x in TD.findall(tr)]
        if len(tds) < 6 or not re.match(r'^\d\d\.\d\d$', tds[0]):
            continue
        out.append((tds[0], num(tds[1]), pct(tds[3]), num(tds[5]) or 0.0))
    return out


def add_years(rows, today):
    """rows are newest first with MM.DD; give them ISO dates."""
    year = today.year
    prev_m = today.month
    res = []
    for md, c, p, a in rows:
        m, d = int(md[:2]), int(md[3:])
        if m > prev_m + 1:  # went back across New Year
            year -= 1
        prev_m = m
        res.append(('%04d-%02d-%02d' % (year, m, d), c, p, a))
    return res


LIST_STAMP = {}


def kr_list(sec):
    stocks, page = [], 1
    while True:
        batch = list(range(page, page + 10))
        with ThreadPoolExecutor(6) as ex:
            htmls = list(ex.map(lambda n: get(ET + 'mdl_capital_list?varMarketSec=%d&varPage=%d' % (sec, n)), batch))
        empty = False
        if page == 1 and htmls:
            LIST_STAMP[sec] = list_stamp(htmls[0])
        for h in htmls:
            rows = parse_cap(h)
            if not rows:
                empty = True
                break
            stocks += rows
        if empty or page > 400:
            break
        page += 10
    seen, uniq = set(), []
    for s in stocks:
        if s['code'] not in seen:
            seen.add(s['code'])
            uniq.append(s)
    return uniq


def kr_history(code, pages):
    rows = []
    for n in range(1, pages + 1):
        part = parse_day(get(ET + 'mdl_stock_day?varIssCd=%s&varPage=%d' % (code, n)))
        if not part:
            break
        rows += part
    return add_years(rows, NOW.date())


def kr_compute(stocks, hist, dates_wanted):
    """Return (day rows, movers, active) for the dates in dates_wanted."""
    ser = {}
    for s in stocks:
        h = hist.get(s['code'])
        if not h:
            continue
        h = sorted(h)  # oldest first
        ser[s['code']] = (s, [x[0] for x in h], h)
    days, movers, active = [], [], []
    for mk in ('코스피', '코스닥'):
        mine = {c: v for c, v in ser.items() if v[0]['market'] == mk}
        for d in dates_wanted:
            rets, valid, amts = [], [], []
            ab = nn = hi = lo = 0
            for c, (s, ds, h) in mine.items():
                try:
                    i = ds.index(d)
                except ValueError:
                    continue
                _, close, p, amt = h[i]
                if close is None:
                    continue
                rets.append(p)
                amts.append((amt, s, p))
                if i >= 1 and h[i - 1][1]:
                    prev = h[i - 1][1]
                    if abs((close / prev - 1) * 100 - p) <= 0.6:
                        valid.append((s, close, prev, p))
                if i >= 19:
                    w = [x[1] for x in h[i - 19:i + 1]]
                    if None not in w:
                        nn += 1
                        if close > sum(w) / 20:
                            ab += 1
                if i >= 20:
                    pw = [x[1] for x in h[i - 20:i]]
                    if None not in pw:
                        if close > max(pw):
                            hi += 1
                        if close < min(pw):
                            lo += 1
            if not valid:
                continue
            pc = sum(v[2] * v[0]['shares'] for v in valid)
            cc = sum(v[1] * v[0]['shares'] for v in valid)
            tot = sum(a[0] for a in amts)
            amts.sort(key=lambda a: -a[0])
            days.append({
                'date': d, 'market': mk, 'cap_ret': round((cc / pc - 1) * 100, 2),
                'ew_ret': round(sum(rets) / len(rets), 2), 'med_ret': round(st.median(rets), 2),
                'up': sum(r > 0 for r in rets), 'down': sum(r < 0 for r in rets), 'flat': sum(r == 0 for r in rets),
                'above_ma20': round(ab / nn * 100, 1) if nn >= 100 else None,
                'new_hi20': hi if nn >= 100 else None, 'new_lo20': lo if nn >= 100 else None,
                'amt_eok': round(tot / 100), 'amt_usd_b': None, 'etf_ret': None,
                'top10_share': round(sum(a[0] for a in amts[:10]) / tot * 100, 1) if tot else None,
                'limit_up': sum(r >= 29.5 for r in rets), 'limit_down': sum(r <= -29.5 for r in rets),
                'index_est': None,
            })
            con = sorted(((v[0], v[3], (v[1] - v[2]) * v[0]['shares']) for v in valid), key=lambda x: x[2])
            for side, arr in (('up', con[::-1][:5]), ('down', con[:5])):
                for k, (s, p, c) in enumerate(arr):
                    movers.append({'date': d, 'market': mk, 'side': side, 'rank': k + 1, 'code': s['code'],
                                   'name': s['name'], 'chg': p, 'contrib_pp': round(c / pc * 100, 3)})
            for a, s, p in amts[:10]:
                active.append({'date': d, 'market': mk, 'code': s['code'], 'name': s['name'],
                               'amt_eok': round(a / 100), 'amt_usd_m': None, 'chg': p})
    return days, movers, active


def update_kr(state):
    days = state['mkt_days']
    kr_old = sorted({r['date'] for r in days if r['market'] != '나스닥100'})
    boot = not kr_old
    pages = 13 if boot else 3
    log('KR: %s, %d pages per stock' % ('backfill' if boot else 'daily', pages))
    stocks = []
    for sec, mk in ((1, '코스피'), (2, '코스닥')):
        lst = kr_list(sec)
        for s in lst:
            s['market'] = mk
        stocks += lst
        log('KR list', mk, len(lst))
    if len(stocks) < 1500:
        raise RuntimeError('stock list too short: %d' % len(stocks))
    hist, fails = {}, 0
    def job(s):
        try:
            return s['code'], kr_history(s['code'], pages)
        except Exception as e:  # noqa: BLE001
            return s['code'], e
    with ThreadPoolExecutor(8) as ex:
        for i, (code, h) in enumerate(ex.map(job, stocks)):
            if isinstance(h, Exception):
                fails += 1
            else:
                hist[code] = h
            if i % 500 == 0:
                log('KR history', i, '/', len(stocks))
    log('KR history done, failed', fails)
    if fails > len(stocks) * 0.05:
        raise RuntimeError('too many failed stocks: %d' % fails)
    # The per-stock daily table is posted late (often the next day). The list page
    # already has today's close, so add today from it after 15:30 if missing.
    stamps = [LIST_STAMP.get(1), LIST_STAMP.get(2)]
    today = NOW.date().isoformat()
    if all(st_ and st_[0] == today and st_[1] >= '15:30' for st_ in stamps):
        added = 0
        for s in stocks:
            h = hist.get(s['code'])
            if not h or s.get('close') is None or any(x[0] == today for x in h):
                continue
            h.insert(0, (today, s['close'], s['pct'], s['close'] * s['vol'] / 1e6))
            added += 1
        log('KR today from list page:', added)
    all_dates = sorted({x[0] for h in hist.values() for x in h})
    if boot:
        wanted = all_dates[1:]
    else:
        # recompute the last stored day too, so a list-page day gets replaced
        # by the official daily table once it is posted
        wanted = [d for d in all_dates if d >= kr_old[-1]]
    if not wanted:
        log('KR: no new trading day')
        return False
    new_days, new_mv, new_ac = kr_compute(stocks, hist, wanted)
    # chain the estimated index
    for mk in ('코스피', '코스닥'):
        rows = sorted([r for r in new_days if r['market'] == mk], key=lambda r: r['date'])
        if not rows:
            continue
        if boot:
            ad, av = ANCHOR[mk]
            idx = next((i for i, r in enumerate(rows) if r['date'] == ad), len(rows) - 1)
            rows[idx]['index_est'] = av
            for i in range(idx + 1, len(rows)):
                rows[i]['index_est'] = round(rows[i - 1]['index_est'] * (1 + rows[i]['cap_ret'] / 100), 2)
            for i in range(idx - 1, -1, -1):
                rows[i]['index_est'] = round(rows[i + 1]['index_est'] / (1 + rows[i + 1]['cap_ret'] / 100), 2)
        else:
            base = [r for r in days if r['market'] == mk and r['date'] < rows[0]['date']]
            if not base:
                continue
            lv = max(base, key=lambda r: r['date'])['index_est']
            for r in rows:
                lv = round(lv * (1 + r['cap_ret'] / 100), 2)
                r['index_est'] = lv
    ws = set(wanted)
    state['mkt_days'] = [r for r in days if not (r['market'] != '나스닥100' and r['date'] in ws)] + new_days
    state['movers'] = [r for r in state['movers'] if not (r['market'] != '나스닥100' and r['date'] in ws)] + new_mv
    state['active'] = [r for r in state['active'] if not (r['market'] != '나스닥100' and r['date'] in ws)] + new_ac
    log('KR added', sorted(ws)[0], '...', sorted(ws)[-1])
    return True


# ---------------------------------------------------------------- US

SA = 'https://stockanalysis.com'


def unflat(arr):
    memo = {}

    def h(i):
        if isinstance(i, int) and i < 0:
            return None
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in ('Date', 'Set', 'Map', 'BigInt', 'RegExp', 'Object'):
                memo[i] = v[1] if len(v) > 1 else None
                return memo[i]
            out = []
            memo[i] = out
            out.extend(h(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            memo[i] = out
            for k, x in v.items():
                out[k] = h(x)
            return out
        memo[i] = v
        return v
    return h(0)


def sa_nodes(path):
    j = get(SA + path + '__data.json', kind='json')
    return [unflat(n['data']) if n and n.get('data') else None for n in j['nodes']]


def sa_history(kind, sym):
    nodes = sa_nodes('/%s/%s/history/' % (kind, sym.lower()))
    for n in nodes:
        if isinstance(n, dict) and isinstance(n.get('data'), dict) and isinstance(n['data'].get('data'), list):
            rows = [(r['t'], r['c'], r.get('v') or 0) for r in n['data']['data'] if r.get('c') is not None]
            return sorted(rows)
    raise RuntimeError('no history for ' + sym)


SUFFIX = re.compile(r',? (Inc\.?|Corporation|Corp\.?|Company|Co\.|Holdings|Ltd\.?|plc|N\.V\.|Incorporated|Group|Limited)$')


def short(n):
    n = SUFFIX.sub('', n)
    n = SUFFIX.sub('', n)
    return n.replace(' Class A', '').replace(' Class C', '').strip(' ,')


def update_us(state):
    nodes = sa_nodes('/list/nasdaq-100-stocks/')
    lst = next(n['stockData'] for n in nodes if isinstance(n, dict) and isinstance(n.get('stockData'), list))
    lst = [x for x in lst if x.get('price') and x.get('marketCap')]
    log('US list', len(lst))
    if len(lst) < 90:
        raise RuntimeError('Nasdaq-100 list too short')
    syms = [('stocks', x['s']) for x in lst] + [('etf', e) for e in ('QQQ', 'SPY', 'SOXX', 'EWY')]
    with ThreadPoolExecutor(5) as ex:
        res = list(ex.map(lambda a: (a[1], sa_history(*a)), syms))
    H = dict(res)
    info = {x['s']: x for x in lst}
    stocks = [s for s in info if s in H]
    sh = {s: info[s]['marketCap'] / info[s]['price'] for s in stocks}
    C = {s: {t: c for t, c, v in H[s]} for s in stocks}
    V = {s: {t: c * v for t, c, v in H[s]} for s in stocks}
    dates = [r[0] for r in H['QQQ']]

    def ret(sym, t, p):
        d = {a: c for a, c, v in H[sym]}
        return round((d[t] / d[p] - 1) * 100, 2) if t in d and p in d else None
    qc = {t: c for t, c, v in H['QQQ']}
    days, movers, active, ref = [], [], [], []
    for i in range(1, len(dates)):
        t, p = dates[i], dates[i - 1]
        rs, con, dv = [], [], []
        pcap = ccap = 0
        for s in stocks:
            if t in C[s] and p in C[s]:
                r = (C[s][t] / C[s][p] - 1) * 100
                rs.append(r)
                pcap += C[s][p] * sh[s]
                ccap += C[s][t] * sh[s]
                con.append((s, r, (C[s][t] - C[s][p]) * sh[s]))
            if t in V[s]:
                dv.append((s, V[s][t], (C[s][t] / C[s][p] - 1) * 100 if p in C[s] else None))
        if not rs:
            continue
        ab = hi = lo = nn = 0
        if i >= 20:
            for s in stocks:
                w = [C[s].get(d) for d in dates[i - 19:i + 1]]
                if None in w:
                    continue
                nn += 1
                if w[-1] > sum(w) / 20:
                    ab += 1
                pw = [C[s].get(d) for d in dates[i - 20:i]]
                if None in pw:
                    continue
                if w[-1] > max(pw):
                    hi += 1
                if w[-1] < min(pw):
                    lo += 1
        tot = sum(x[1] for x in dv)
        dv.sort(key=lambda x: -x[1])
        days.append({
            'date': t, 'market': '나스닥100', 'cap_ret': round((ccap / pcap - 1) * 100, 2), 'etf_ret': ret('QQQ', t, p),
            'ew_ret': round(sum(rs) / len(rs), 2), 'med_ret': round(st.median(rs), 2),
            'up': sum(r > 0 for r in rs), 'down': sum(r < 0 for r in rs), 'flat': sum(r == 0 for r in rs),
            'above_ma20': round(ab / nn * 100, 1) if nn else None, 'new_hi20': hi if nn else None, 'new_lo20': lo if nn else None,
            'amt_eok': None, 'amt_usd_b': round(tot / 1e9, 1), 'top10_share': round(sum(x[1] for x in dv[:10]) / tot * 100, 1) if tot else None,
            'limit_up': None, 'limit_down': None, 'index_est': qc[t],
        })
        con.sort(key=lambda x: x[2])
        for side, arr in (('up', con[::-1][:5]), ('down', con[:5])):
            for k, (s, r, c) in enumerate(arr):
                movers.append({'date': t, 'market': '나스닥100', 'side': side, 'rank': k + 1, 'code': s,
                               'name': short(info[s]['n']), 'chg': round(r, 2), 'contrib_pp': round(c / pcap * 100, 3)})
        for s, v, r in dv[:10]:
            active.append({'date': t, 'market': '나스닥100', 'code': s, 'name': short(info[s]['n']),
                           'amt_eok': None, 'amt_usd_m': round(v / 1e6), 'chg': None if r is None else round(r, 2)})
        ref.append({'date': t, 'qqq': ret('QQQ', t, p), 'spy': ret('SPY', t, p), 'soxx': ret('SOXX', t, p), 'ewy': ret('EWY', t, p)})
    state['mkt_days'] = [r for r in state['mkt_days'] if r['market'] != '나스닥100'] + days
    state['movers'] = [r for r in state['movers'] if r['market'] != '나스닥100'] + movers
    state['active'] = [r for r in state['active'] if r['market'] != '나스닥100'] + active
    state['us_ref'] = ref
    log('US rows', len(days), 'last', dates[-1])
    return True


def trim(state):
    for key in ('mkt_days', 'movers', 'active', 'us_ref'):
        rows = state[key]
        ds = sorted({r['date'] for r in rows})
        if len(ds) > KEEP_DAYS:
            cut = ds[-KEEP_DAYS]
            state[key] = [r for r in rows if r['date'] >= cut]
        state[key].sort(key=lambda r: (r['date'], r.get('market', ''), r.get('side', ''), r.get('rank', 0)))


def main():
    os.makedirs(DATA, exist_ok=True)
    state = {k: load(k + '.json', []) for k in ('mkt_days', 'movers', 'active', 'us_ref')}
    meta = load('meta.json', {})
    args = set(sys.argv[1:])
    ok = {}
    if '--us-only' not in args:
        try:
            ok['kr'] = update_kr(state)
        except Exception as e:  # noqa: BLE001
            log('KR FAILED:', repr(e))
            ok['kr'] = 'error: ' + str(e)[:200]
    if '--kr-only' not in args:
        try:
            ok['us'] = update_us(state)
        except Exception as e:  # noqa: BLE001
            log('US FAILED:', repr(e))
            ok['us'] = 'error: ' + str(e)[:200]
    trim(state)
    for k, v in state.items():
        save(k + '.json', v)
    if not os.path.exists(os.path.join(DATA, 'notes.json')):
        save('notes.json', [])
    meta['updated_at'] = NOW.isoformat(timespec='minutes')
    meta['last_run'] = ok
    kr = [r['date'] for r in state['mkt_days'] if r['market'] != '나스닥100']
    meta['kr_last'] = max(kr) if kr else None
    meta['us_last'] = state['us_ref'][-1]['date'] if state['us_ref'] else None
    save('meta.json', meta)
    log('done', ok)
    if all(isinstance(v, str) for v in ok.values()):
        sys.exit(1)


if __name__ == '__main__':
    main()
