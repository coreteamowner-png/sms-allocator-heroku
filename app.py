import os, re, csv, io, sys, time, unicodedata
from urllib.parse import unquote
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_sqlalchemy import SQLAlchemy
import requests
from bs4 import BeautifulSoup
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static', template_folder='templates')

# Config
DATABASE_URL = os.getenv('DATABASE_URL') or 'sqlite:///local.db'
UPSTREAM_BASE = os.getenv('UPSTREAM_BASE') or 'http://mysmsportal.com'
LOGIN_FORM_RAW = os.getenv('LOGIN_FORM_RAW','')  # set in Heroku config vars
PHPSESSID_OVERRIDE = os.getenv('PHPSESSID_OVERRIDE','')  # optional
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Models
class Allocation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_external_id = db.Column(db.String, nullable=False)
    range_code = db.Column(db.String, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String, nullable=False)
    response = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

with app.app_context():
    db.create_all()

# ----- helper parsing / scraping utilities -----
BASE = UPSTREAM_BASE
LOGIN_PATH = "/index.php?login=1"
ALL_PATH   = "/index.php?opt=shw_all_v2"
TODAY_PATH = "/index.php?opt=shw_sts_today"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": BASE,
    "Referer": BASE + "/index.php?opt=shw_all_v2",
}

def parse_form_encoded(raw):
    parts = [p for p in raw.split("&") if "=" in p]
    return {k: unquote(v) for k, v in (p.split("=", 1) for p in parts)}

def do_login(sess):
    if not LOGIN_FORM_RAW:
        return None
    data = parse_form_encoded(LOGIN_FORM_RAW)
    hdr = dict(HEADERS)
    hdr["Referer"] = BASE + "/index.php?opt=shw_allo"
    return sess.post(BASE + LOGIN_PATH, data=data, headers=hdr, allow_redirects=True, timeout=15)

def get_clients_page(sess):
    hdr = dict(HEADERS); hdr["Referer"] = BASE + "/index.php?login=1"
    return sess.get(BASE + ALL_PATH, headers=hdr, timeout=15)

def post_open_client(sess, selidd):
    hdr = dict(HEADERS)
    hdr.update({"Content-Type": "application/x-www-form-urlencoded", "Referer": BASE + "/index.php?opt=shw_all_v2"})
    data = {"selidd": str(selidd), "selected2": "1"}
    return sess.post(BASE + ALL_PATH, data=data, headers=hdr, timeout=20)

def allocate_remote(sess, selidd, selrng, count):
    payload = {
        "quantity": str(count),
        "selidd": str(selidd),
        "selrng": selrng,
        "allocate": "1"
    }
    return sess.post(BASE + ALL_PATH, data=payload, headers=HEADERS, timeout=20)

# parse ranges table (reused)
def num_from_text(txt: str) -> int:
    s = (txt or "").strip().replace(",", "")
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else 0

def parse_all_ranges_with_stats_and_value(html):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        rng_text = tds[0].get_text(" ", strip=True)
        if not rng_text:
            continue
        up = rng_text.strip().upper()
        if up in ("RANGE", "S/N"):
            continue
        all_num = num_from_text(tds[1].get_text(" ", strip=True))
        free    = num_from_text(tds[2].get_text(" ", strip=True))
        alloc   = num_from_text(tds[3].get_text(" ", strip=True))
        selrng_val = ""
        hidden = tr.find("input", attrs={"name": "selrng"})
        if hidden and hidden.get("value"):
            selrng_val = hidden["value"].strip()
        else:
            frm = tr.find("form")
            if frm:
                inp = frm.find("input", attrs={"name": "selrng"})
                if inp and inp.get("value"):
                    selrng_val = inp["value"].strip()
        rows.append({
            "text": rng_text,
            "all": all_num, "free": free, "allocated": alloc,
            "selrng": selrng_val,
            "allocatable": bool(selrng_val)
        })
    return rows

# ---------------- Today-stats parsing helpers (adapted from your script) ----------------
def safe_int_from_text(txt):
    if txt is None:
        return 0
    s = str(txt).strip().replace(",", "")
    s = s.replace("\xa0", " ")
    m = re.search(r"-?\d+", s)
    if not m:
        return 0
    try:
        return int(m.group(0))
    except:
        return 0

def norm_status(s: str):
    if not s:
        return None
    t = unicodedata.normalize("NFKC", s)
    t = t.replace("\xa0", " ")
    t = re.sub(r"[\u2010-\u2015\u2212\u2012\u2013]+", "-", t)
    t = re.sub(r"[^\w\s\-]", " ", t)
    t = re.sub(r"[-_]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip().upper()
    if "NOT" in t and "PAID" in t:
        return "NOT TO BE PAID"
    if "TO" in t and "BE" in t and "PAID" in t:
        return "TO BE PAID"
    return None

def find_table_and_headers(soup):
    for tbl in soup.find_all("table"):
        headers = []
        thead = tbl.find("thead")
        if thead:
            headers = [th.get_text(" ", strip=True).upper() for th in thead.find_all(["th","td"])]
        else:
            first = tbl.find("tr")
            if first:
                headers = [td.get_text(" ", strip=True).upper() for td in first.find_all(["th","td"])]
        joined = " ".join(headers)
        if "CLIENT" in joined and "STATUS" in joined and ("MESSAGES" in joined or "NUMBER" in joined):
            return tbl, headers
    for tbl in soup.find_all("table"):
        txt = tbl.get_text(" ", strip=True).upper()
        if "CLIENT" in txt and "STATUS" in txt:
            thead = tbl.find("thead")
            if thead:
                headers = [th.get_text(" ", strip=True).upper() for th in thead.find_all(["th","td"])]
            else:
                first = tbl.find("tr")
                headers = [td.get_text(" ", strip=True).upper() for td in first.find_all(["th","td"])] if first else []
            return tbl, headers
    return None, []

def get_col_indices_from_headers(headers):
    col_msg = col_client = col_status = None
    for i, h in enumerate(headers):
        hh = (h or "").upper()
        if "MESSAGE" in hh and col_msg is None:
            col_msg = i
        if "CLIENT" in hh and col_client is None:
            col_client = i
        if "STATUS" in hh and col_status is None:
            col_status = i
    return col_msg, col_client, col_status

def compute_counts_from_table(tbl, headers):
    col_msg, col_client, col_status = get_col_indices_from_headers(headers)
    counts = {}
    rows = tbl.find_all("tr")
    for tr in rows:
        cells = tr.find_all(["td", "th"])
        if not cells or len(cells) < 2:
            continue
        header_like = " ".join([c.get_text(" ", strip=True).upper() for c in cells[:min(6, len(cells))]])
        if ("CLIENT" in header_like and "STATUS" in header_like) or ("NUMBER" in header_like and "SENDER" in header_like):
            continue
        client = ""
        if col_client is not None and col_client < len(cells):
            client = cells[col_client].get_text(" ", strip=True)
        else:
            for c in cells:
                t = c.get_text(" ", strip=True)
                if t and re.search(r"[A-Za-z]", t) and not re.match(r"^\+?\d+$", t):
                    client = t
                    break
        if not client:
            continue
        client = client.strip()
        msg_val = 0
        if col_msg is not None and col_msg < len(cells):
            msg_val = safe_int_from_text(cells[col_msg].get_text(" ", strip=True))
        else:
            for c in cells:
                n = safe_int_from_text(c.get_text(" ", strip=True))
                if n >= 0 and n < 1_000_000:
                    msg_val = n
                    break
        status_raw = ""
        if col_status is not None and col_status < len(cells):
            status_raw = cells[col_status].get_text(" ", strip=True)
        else:
            for c in cells:
                t = c.get_text(" ", strip=True).upper()
                if "PAID" in t or "NOT" in t:
                    status_raw = c.get_text(" ", strip=True)
                    break
            if not status_raw:
                status_raw = cells[-1].get_text(" ", strip=True)
        status_key = norm_status(status_raw)
        if status_key not in ("TO BE PAID", "NOT TO BE PAID"):
            continue
        if client not in counts:
            counts[client] = {"TO BE PAID": 0, "NOT TO BE PAID": 0}
        counts[client][status_key] += msg_val
    return counts

def fetch_today_html(sess):
    r = sess.get(BASE + TODAY_PATH, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text

# ---------------- routes (existing) ----------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/csv')
def csv_page():
    return render_template('csv.html')

@app.route('/today')
def today_page():
    return render_template('today.html')

@app.route('/api/clients', methods=['GET'])
def api_clients():
    sess = requests.Session()
    if PHPSESSID_OVERRIDE:
        domain = BASE.replace('http://','').replace('https://','').split('/')[0]
        sess.cookies.set("PHPSESSID", PHPSESSID_OVERRIDE, domain=domain)
    else:
        try:
            do_login(sess)
        except Exception as e:
            return jsonify({'error':'login failed','msg':str(e)}), 500
    try:
        r = get_clients_page(sess)
        soup = BeautifulSoup(r.text, "lxml")
        out=[]
        for opt in soup.select("select[name=selidd] option"):
            val = (opt.get("value") or "").strip()
            if val:
                out.append({'name': opt.get_text(" ", strip=True), 'external_id': val})
        seen=set(); uniq=[]
        for o in out:
            if o['external_id'] not in seen:
                seen.add(o['external_id']); uniq.append(o)
        return jsonify(uniq)
    except Exception as e:
        return jsonify({'error':'fetch clients failed','msg':str(e)}), 500

@app.route('/api/ranges/<selidd>', methods=['GET'])
def api_ranges(selidd):
    sess = requests.Session()
    if PHPSESSID_OVERRIDE:
        domain = BASE.replace('http://','').replace('https://','').split('/')[0]
        sess.cookies.set("PHPSESSID", PHPSESSID_OVERRIDE, domain=domain)
    else:
        try:
            do_login(sess)
        except Exception as e:
            return jsonify({'error':'login failed','msg':str(e)}), 500
    try:
        r = post_open_client(sess, selidd)
        rows = parse_all_ranges_with_stats_and_value(r.text)
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error':'fetch ranges failed','msg':str(e)}), 500

@app.route('/api/allocate', methods=['POST'])
def api_allocate():
    data = request.json or {}
    selidd = data.get('selidd')
    selrng = data.get('selrng')
    qty = int(data.get('quantity',0) or 0)
    if not selidd or not selrng or qty <= 0:
        return jsonify({'error':'missing params'}), 400

    sess = requests.Session()
    if PHPSESSID_OVERRIDE:
        domain = BASE.replace('http://','').replace('https://','').split('/')[0]
        sess.cookies.set("PHPSESSID", PHPSESSID_OVERRIDE, domain=domain)
    else:
        try:
            do_login(sess)
        except Exception as e:
            return jsonify({'error':'login failed','msg':str(e)}), 500

    try:
        resp = allocate_remote(sess, selidd, selrng, qty)
        status = 'success' if resp.status_code == 200 else f'http_{resp.status_code}'
        a = Allocation(client_external_id=selidd, range_code=selrng, quantity=qty, status=status, response=(resp.text[:200] if resp.text else ''))
        db.session.add(a)
        db.session.commit()
        return jsonify({'status':status, 'alloc_id': a.id})
    except Exception as e:
        a = Allocation(client_external_id=selidd, range_code=selrng, quantity=qty, status='error', response=str(e))
        db.session.add(a)
        db.session.commit()
        return jsonify({'error':'allocate failed','msg':str(e)}), 500

@app.route('/api/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({'error':'no_file'}), 400
    f = request.files['file']
    filename = secure_filename(f.filename)
    if not filename.lower().endswith('.csv'):
        return jsonify({'error':'invalid_file_type'}), 400
    try:
        stream = io.StringIO(f.stream.read().decode('utf-8', errors='ignore'))
        reader = csv.DictReader(stream)
    except Exception as e:
        return jsonify({'error':'csv_parse_error','msg':str(e)}), 400

    results = []
    for row in reader:
        client = (row.get('client_external_id') or row.get('selidd') or '').strip()
        selrng = (row.get('selrng') or '').strip()
        qty_raw = (row.get('quantity') or row.get('qty') or '0').strip()
        try:
            qty = int(float(qty_raw))
        except:
            qty = 0
        if not client or not selrng or qty <= 0:
            results.append({'client': client, 'range': selrng, 'qty': qty, 'status': 'skipped', 'msg':'invalid row'})
            continue

        sess = requests.Session()
        if PHPSESSID_OVERRIDE:
            domain = BASE.replace('http://','').replace('https://','').split('/')[0]
            sess.cookies.set("PHPSESSID", PHPSESSID_OVERRIDE, domain=domain)
        else:
            try:
                do_login(sess)
            except Exception as e:
                results.append({'client': client, 'range': selrng, 'qty': qty, 'status': 'login_failed', 'msg': str(e)})
                continue
        try:
            resp = allocate_remote(sess, client, selrng, qty)
            status = 'success' if resp.status_code == 200 else f'http_{resp.status_code}'
            a = Allocation(client_external_id=client, range_code=selrng, quantity=qty, status=status, response=(resp.text[:200] if resp.text else ''))
            db.session.add(a); db.session.commit()
            results.append({'client': client, 'range': selrng, 'qty': qty, 'status': status})
        except Exception as e:
            a = Allocation(client_external_id=client, range_code=selrng, quantity=qty, status='error', response=str(e))
            db.session.add(a); db.session.commit()
            results.append({'client': client, 'range': selrng, 'qty': qty, 'status': 'error', 'msg': str(e)})
    return jsonify({'results': results, 'processed': len(results)})

@app.route('/api/history', methods=['GET'])
def api_history():
    rows = Allocation.query.order_by(Allocation.created_at.desc()).limit(200).all()
    out = []
    for r in rows:
        out.append({
            'id': r.id,
            'client_external_id': r.client_external_id,
            'range_code': r.range_code,
            'quantity': r.quantity,
            'status': r.status,
            'created_at': r.created_at.isoformat()
        })
    return jsonify(out)

# ===== NEW: today stats JSON endpoint =====
@app.route('/api/today', methods=['GET'])
def api_today():
    sess = requests.Session()
    if PHPSESSID_OVERRIDE:
        domain = BASE.replace('http://','').replace('https://','').split('/')[0]
        sess.cookies.set("PHPSESSID", PHPSESSID_OVERRIDE, domain=domain)
    else:
        try:
            do_login(sess)
        except Exception as e:
            return jsonify({'error':'login failed','msg':str(e)}), 500
    try:
        html = fetch_today_html(sess)
        soup = BeautifulSoup(html, "lxml")
        tbl, headers = find_table_and_headers(soup)
        if not tbl:
            return jsonify({'error':'no_table_found'}), 500
        counts = compute_counts_from_table(tbl, headers)
        # build totals and respond with JSON
        totals = {}
        grand = {"TO BE PAID":0,"NOT TO BE PAID":0}
        for c,vals in counts.items():
            totals[c] = vals
            grand["TO BE PAID"] += vals.get("TO BE PAID",0)
            grand["NOT TO BE PAID"] += vals.get("NOT TO BE PAID",0)
        return jsonify({'counts': totals, 'grand': grand})
    except Exception as e:
        return jsonify({'error':'fetch/parse_failed','msg':str(e)}), 500

@app.route('/static/<path:p>')
def static_files(p):
    return send_from_directory('static', p)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',5000)))
