import json
import os
import uuid
import numpy as np
import warnings
warnings.filterwarnings("ignore")

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, date
from collections import Counter

app = Flask(__name__)
app.secret_key = "local_test_key"

BASE_DIR         = os.path.abspath(os.path.dirname(__file__))
DATA_DIR         = os.path.join(BASE_DIR, "data")
INVENTORY_FILE   = os.path.join(DATA_DIR, "inventory.json")
SALES_FILE       = os.path.join(DATA_DIR, "sales.json")
ARCHIVE_FILE     = os.path.join(DATA_DIR, "sales_archive.json")
CLOSED_DAYS_FILE = os.path.join(DATA_DIR, "closed_days.json")
UPLOAD_FOLDER    = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
USERNAME = "admin"
PASSWORD = "passw0rd"
SALES_ANCHOR = date(2025, 4, 28)

# ─── Helpers ─────────────────────────────────────────────────────────────────

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def load_json(filepath):
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(filepath):
        with open(filepath, "w") as f:
            json.dump([], f, indent=4)
    with open(filepath, "r") as f:
        try:
            return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

def save_json(filepath, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=4)

def load_closed_days():
    raw = load_json(CLOSED_DAYS_FILE)
    return set(raw) if isinstance(raw, list) else set()

def save_closed_days(day_set):
    save_json(CLOSED_DAYS_FILE, sorted(day_set))

def get_current_week_monday():
    today = date.today()
    return today - timedelta(days=today.weekday())

def auto_archive_old_sales():
    monday = get_current_week_monday()
    all_sales = load_json(SALES_FILE)
    current_week, to_archive = [], []
    for sale in all_sales:
        date_str = sale.get("datetime", "")[:10]
        try:
            sale_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            current_week.append(sale); continue
        (current_week if sale_date >= monday else to_archive).append(sale)
    if to_archive:
        archived = load_json(ARCHIVE_FILE)
        archived.extend(to_archive)
        save_json(ARCHIVE_FILE, archived)
        save_json(SALES_FILE, current_week)
    return current_week, len(to_archive)

def get_category_from_sale(sale):
    category = sale.get("category", "")
    if category and category != "Other":
        return category
    p_name = ""
    product = sale.get("product", {})
    if isinstance(product, dict):
        p_name = product.get("name", "").lower()
    elif isinstance(product, str):
        p_name = product.lower()
    if "jacket" in p_name or "hoodie" in p_name:
        return "Jackets"
    elif "short" in p_name:
        return "Shorts"
    elif "shirt" in p_name or "jersey" in p_name:
        return "T-shirts"
    return "Other"

def get_all_weeks():
    anchor_monday  = SALES_ANCHOR - timedelta(days=SALES_ANCHOR.weekday())
    current_monday = get_current_week_monday()
    weeks, ws = [], anchor_monday
    while ws <= current_monday:
        we = ws + timedelta(days=6)
        weeks.append({
            "start": ws.strftime("%Y-%m-%d"),
            "end":   we.strftime("%Y-%m-%d"),
            "label": f"{ws.strftime('%b %d')} – {we.strftime('%b %d, %Y')}"
        })
        ws += timedelta(days=7)
    return list(reversed(weeks))

# ─── Philippine Seasonal Data ─────────────────────────────────────────────────

PH_HOLIDAYS_FIXED = {
    (1, 1):  0.15,
    (4, 9):  0.60,
    (5, 1):  0.55,
    (6, 12): 0.65,
    (11,30): 0.60,
    (12,25): 0.10,
    (12,30): 0.60,
    (2, 17): 0.70,
    (8, 21): 0.65,
    (11, 1): 0.50,
    (11, 2): 0.55,
    (12, 8): 0.65,
    (12,24): 0.20,
    (12,31): 0.30,
}
HOLY_WEEK_MONTHS_DAYS = [(4,2),(4,3),(4,4),(4,5)]
PH_MONTHLY_MULT = {
    1:1.35,2:1.05,3:1.15,4:0.85,5:1.10,6:1.25,
    7:0.90,8:0.90,9:1.00,10:1.10,11:1.75,12:2.40,
}
PH_DOW_MULT = {0:0.85,1:0.85,2:0.90,3:0.95,4:1.10,5:1.65,6:1.50}

def ph_day_factor(d):
    dow_m = PH_DOW_MULT.get(d.weekday(), 1.0)
    mon_m = PH_MONTHLY_MULT.get(d.month, 1.0)
    hol_m = PH_HOLIDAYS_FIXED.get((d.month, d.day), None)
    if hol_m is None and (d.month, d.day) in HOLY_WEEK_MONTHS_DAYS:
        hol_m = 0.25
    return dow_m * mon_m * (hol_m if hol_m is not None else 1.0)

# ─── SARIMA Engine ────────────────────────────────────────────────────────────

def build_daily_series(all_sales, categories_list):
    if not all_sales:
        return {}, date.today()
    dated = []
    for s in all_sales:
        try:
            sd  = datetime.strptime(s.get("datetime","")[:10],"%Y-%m-%d").date()
            cat = get_category_from_sale(s)
            qty = int(s.get("quantity",0))
            if cat in categories_list and qty > 0:
                dated.append((sd, cat, qty))
        except ValueError:
            continue
    if not dated:
        return {}, date.today()
    min_date, max_date = min(d for d,_,_ in dated), max(d for d,_,_ in dated)
    series = {}
    cur = min_date
    while cur <= max_date:
        series[cur] = {cat: 0 for cat in categories_list}
        cur += timedelta(days=1)
    for sd, cat, qty in dated:
        series[sd][cat] += qty
    return series, max_date

def sarima_forecast(ts_values, steps, seasonal_period=7):
    try:
        from statsmodels.tsa.statespace.sarimax import SARIMAX
        arr = np.array(ts_values, dtype=float)
        if arr.sum() == 0:
            return [0] * steps
        zero_pct = (arr == 0).sum() / len(arr)
        if zero_pct > 0.60:
            nz_idx = np.where(arr > 0)[0]
            if len(nz_idx) == 0:
                return [0] * steps
            arr_i = arr.copy()
            for i in range(len(arr)):
                if arr[i] == 0:
                    pv = nz_idx[nz_idx < i]
                    nv = nz_idx[nz_idx > i]
                    if len(pv) and len(nv):
                        pi, ni = pv[-1], nv[0]
                        arr_i[i] = arr[pi] + (arr[ni]-arr[pi])*(i-pi)/(ni-pi)
                    elif len(pv):
                        arr_i[i] = arr[pv[-1]] * 0.5
                    elif len(nv):
                        arr_i[i] = arr[nv[0]] * 0.5
            arr = arr_i
        arr = np.log1p(arr)
        # Use only the most recent 365 data points to keep memory usage low
        # on constrained hosting environments (e.g. Render free tier 512 MB RAM)
        if len(arr) > 365:
            arr = arr[-365:]
        if len(arr) >= seasonal_period * 2:
            try:
                m = SARIMAX(arr, order=(1,1,1), seasonal_order=(1,1,1,seasonal_period),
                            enforce_stationarity=False, enforce_invertibility=False)
                raw_fc = m.fit(disp=False, maxiter=100, method='lbfgs',
                               optim_score=None, low_memory=True).forecast(steps=steps)
            except Exception:
                m = SARIMAX(arr, order=(1,1,1), enforce_stationarity=False, enforce_invertibility=False)
                raw_fc = m.fit(disp=False, maxiter=75).forecast(steps=steps)
        else:
            m = SARIMAX(arr, order=(1,1,1), enforce_stationarity=False, enforce_invertibility=False)
            raw_fc = m.fit(disp=False, maxiter=75).forecast(steps=steps)
        fc = np.expm1(raw_fc)
        return [max(0, round(float(v))) for v in fc]
    except Exception:
        window = ts_values[-7:] if len(ts_values) >= 7 else ts_values
        avg = float(np.mean(window)) if window else 0.0
        return [max(0, round(avg))] * steps

def apply_ph_seasonal_adjustment(base_forecast, start_date, closed_days_set=None):
    if not base_forecast:
        return base_forecast
    closed_days_set = closed_days_set or set()
    factors = []
    for i in range(len(base_forecast)):
        d = start_date + timedelta(days=i)
        factors.append(0.0 if d.isoformat() in closed_days_set else ph_day_factor(d))
    raw_sum = sum(base_forecast)
    if raw_sum == 0:
        return base_forecast
    weighted = [b * f for b, f in zip(base_forecast, factors)]
    w_sum = sum(weighted)
    if w_sum == 0:
        return [0] * len(base_forecast)
    scale = raw_sum / w_sum
    return [0 if factors[i] == 0.0 else max(0, round(weighted[i] * scale)) for i in range(len(base_forecast))]

def run_sarima_for_category(daily_series, category, forecast_start, steps, closed_days_set=None):
    sorted_dates = sorted(daily_series.keys())
    ts = [daily_series[d].get(category, 0) for d in sorted_dates]
    if not ts or sum(ts) == 0:
        return [0] * steps
    raw_fc = sarima_forecast(ts, steps)
    return apply_ph_seasonal_adjustment(raw_fc, forecast_start, closed_days_set)

def duration_to_steps(duration):
    if duration.endswith("m"):
        return int(duration[:-1]) * 30
    elif duration.endswith("d"):
        return int(duration[:-1])
    try:
        return int(duration)
    except ValueError:
        return 7

# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route("/api/inventory")
def api_inventory():
    inventory = load_json(INVENTORY_FILE)
    active = [i for i in inventory
              if sum(int(i.get(k,0)) for k in ['stock_s','stock_m','stock_l','stock_xl']) > 0]
    return jsonify(active)

@app.route("/api/closed_days", methods=["GET"])
def api_get_closed_days():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"closed_days": sorted(load_closed_days())})

@app.route("/api/set_closed_day", methods=["POST"])
def api_set_closed_day():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    date_str = data.get("date", "").strip()
    is_closed = bool(data.get("closed", True))
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400
    days = load_closed_days()
    days.add(date_str) if is_closed else days.discard(date_str)
    save_closed_days(days)
    return jsonify({"success": True, "date": date_str, "closed": is_closed,
                    "closed_days": sorted(days)})

@app.route("/api/sarima_forecast")
def api_sarima_forecast():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        import traceback
        duration = request.args.get("duration", "7d")
        steps    = duration_to_steps(duration)
        all_sales = load_json(ARCHIVE_FILE) + load_json(SALES_FILE)
        categories_list = ["Jackets", "T-shirts", "Shorts"]
        daily_series, _ = build_daily_series(all_sales, categories_list)
        forecast_start  = date.today() + timedelta(days=1)
        closed_days     = load_closed_days()
        result = {cat: run_sarima_for_category(daily_series, cat, forecast_start, steps, closed_days)
                  for cat in categories_list}
        labels = [(forecast_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(steps)]
        return jsonify({"labels": labels, "forecast": result, "steps": steps,
                        "duration": duration, "closed_days": sorted(closed_days)})
    except MemoryError:
        return jsonify({"error": "Not enough memory to run forecast. Try a shorter duration (e.g. 1 Week or 2 Weeks)."}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Forecast failed: {str(e)}"}), 500

# ─── Core Routes ──────────────────────────────────────────────────────────────

@app.route("/checkout", methods=["POST"])
def checkout():
    if "user" not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    data            = request.get_json()
    cart_items      = data.get('items', [])
    payment_method  = data.get('payment_method', 'Cash')
    buyer_name      = data.get('customer_name', 'Walk-in Customer')
    amount_tendered = float(data.get('amount_tendered', 0))
    if not cart_items:
        return jsonify({"success": False, "message": "Cart is empty"}), 400
    inventory     = load_json(INVENTORY_FILE)
    inventory_map = {str(item.get('id')): item for item in inventory if 'id' in item}
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sales_to_save = []
    for ci in cart_items:
        iid = str(ci.get('id')); qty = int(ci.get('quantity',0))
        sz  = ci.get('size','S').lower(); sk = f"stock_{sz}"
        if iid in inventory_map:
            prod = inventory_map[iid]
            cur  = int(prod.get(sk,0)); price = float(prod.get("price",0.0))
            if cur >= qty:
                sales_to_save.append({
                    "sale_id":str(uuid.uuid4()),"datetime":now_str,
                    "product":{"name":f"{prod['name']} ({sz.upper()})"},
                    "product_id":prod.get("id"),"category":prod.get("category","Other"),
                    "quantity":qty,"unit_price":price,"total_price":price*qty,
                    "payment_method":payment_method,"buyer_name":buyer_name,
                    "amount_tendered":amount_tendered,
                })
                prod[sk] = cur - qty
            else:
                return jsonify({"success":False,"message":f"Low stock for {prod['name']} size {sz.upper()}"}),400
        else:
            return jsonify({"success":False,"message":f"Item ID {iid} not found"}),400
    if sales_to_save:
        all_s = load_json(SALES_FILE); all_s.extend(sales_to_save)
        save_json(SALES_FILE, all_s); save_json(INVENTORY_FILE, inventory)
        return jsonify({"success":True,"message":"Sale recorded successfully!"})
    return jsonify({"success":False,"message":"Transaction failed"}),400

@app.route("/")
def home(): return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if request.form["username"]==USERNAME and request.form["password"]==PASSWORD:
            session["user"] = request.form["username"]
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if "user" not in session: return redirect(url_for("login"))
    inv = load_json(INVENTORY_FILE); sl = load_json(SALES_FILE)
    sc  = Counter()
    for s in sl: sc[s.get("product",{}).get("name","Unknown")] += int(s.get("quantity",0))
    lsi = [i for i in inv if any(int(i.get(k,0))<3 for k in ['stock_s','stock_m','stock_l','stock_xl'])]
    return render_template("dashboard.html",
        total_items=len(inv), items=inv,
        units_sold=sum(int(s.get("quantity",0)) for s in sl),
        sold_items_list=[{"name":n,"quantity":q} for n,q in sc.items()],
        low_stock=len(lsi), low_stock_items=lsi, user=session["user"])

@app.route("/inventory", methods=["GET","POST"])
def inventory():
    if "user" not in session: return redirect(url_for("login"))
    items = load_json(INVENTORY_FILE)
    if request.method == "POST":
        for item in items:
            oid = str(item.get("id"))
            if request.form.get(f"name_{oid}"):
                item["name"]     = request.form.get(f"name_{oid}", item["name"])
                item["category"] = request.form.get(f"category_{oid}", item.get("category","Other"))
                item["price"]    = float(request.form.get(f"price_{oid}", 0) or 0)
                item["stock_s"]  = int(request.form.get(f"stock_s_{oid}", 0) or 0)
                item["stock_m"]  = int(request.form.get(f"stock_m_{oid}", 0) or 0)
                item["stock_l"]  = int(request.form.get(f"stock_l_{oid}", 0) or 0)
                item["stock_xl"] = int(request.form.get(f"stock_xl_{oid}", 0) or 0)
        nn=request.form.getlist("new_name[]"); nc=request.form.getlist("new_category[]")
        np2=request.form.getlist("new_price[]"); ns=request.form.getlist("new_stock_s[]")
        nm=request.form.getlist("new_stock_m[]"); nl=request.form.getlist("new_stock_l[]")
        nxl=request.form.getlist("new_stock_xl[]")
        if nn:
            ei=[int(i["id"]) for i in items if "id" in i and str(i["id"]).isdigit()]
            mid=max(ei) if ei else 0
            for i,n in enumerate(nn):
                if n.strip():
                    mid+=1
                    items.append({"id":mid,"name":n.strip(),
                        "category":nc[i] if i<len(nc) else "Other",
                        "price":float(np2[i] or 0) if i<len(np2) and np2[i] else 0.0,
                        "stock_s":int(ns[i] or 0) if i<len(ns) and ns[i] else 0,
                        "stock_m":int(nm[i] or 0) if i<len(nm) and nm[i] else 0,
                        "stock_l":int(nl[i] or 0) if i<len(nl) and nl[i] else 0,
                        "stock_xl":int(nxl[i] or 0) if i<len(nxl) and nxl[i] else 0,
                        "image_filename":None})
        save_json(INVENTORY_FILE, items); return redirect(url_for("inventory"))
    return render_template("inventory.html", items=items)

@app.route("/upload_image/<int:item_id>", methods=["POST"])
def upload_image(item_id):
    if "user" not in session: return redirect(url_for("login"))
    if "image" not in request.files: return redirect(url_for("inventory"))
    image = request.files["image"]
    if image and allowed_file(image.filename):
        filename = secure_filename(image.filename)
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        name,ext = os.path.splitext(filename)
        filename = f"{name}_{int(datetime.now().timestamp())}{ext}"
        image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        items = load_json(INVENTORY_FILE)
        for item in items:
            if str(item.get("id")) == str(item_id):
                item["image_filename"] = filename; break
        save_json(INVENTORY_FILE, items)
    return redirect(url_for("inventory"))

@app.route("/remove_item/<int:item_id>", methods=["POST"])
def remove_item(item_id):
    if "user" not in session: return redirect(url_for("login"))
    save_json(INVENTORY_FILE, [i for i in load_json(INVENTORY_FILE) if str(i.get("id"))!=str(item_id)])
    return redirect(url_for("inventory"))

@app.route("/delete_sale/<source>/<int:idx>", methods=["POST"])
def delete_sale(source, idx):
    if "user" not in session: return redirect(url_for("login"))
    if source == "archive":
        records = load_json(ARCHIVE_FILE); real_idx = len(records)-1-idx
        if 0 <= real_idx < len(records): del records[real_idx]
        save_json(ARCHIVE_FILE, records); return redirect(url_for("sales_archive"))
    else:
        records = load_json(SALES_FILE)
        if 0 <= idx < len(records): del records[idx]
        save_json(SALES_FILE, records); return redirect(url_for("sales"))

@app.route("/sales")
def sales():
    if "user" not in session: return redirect(url_for("login"))
    cw, _ = auto_archive_old_sales()
    monday = get_current_week_monday(); sunday = monday + timedelta(days=6)
    wl = f"{monday.strftime('%B %d')} \u2013 {sunday.strftime('%B %d, %Y')}"
    arch = load_json(ARCHIVE_FILE)
    return render_template("sales.html", sales=cw, archive_sales=arch,
        products=load_json(INVENTORY_FILE), week_label=wl,
        total_archived=len(arch), is_archive_view=False)

@app.route("/sales/archive")
def sales_archive():
    if "user" not in session: return redirect(url_for("login"))
    archived = list(reversed(load_json(ARCHIVE_FILE)))
    monday = get_current_week_monday(); sunday = monday + timedelta(days=6)
    wl = f"{monday.strftime('%B %d')} \u2013 {sunday.strftime('%B %d, %Y')}"
    return render_template("sales.html", sales=archived, archive_sales=archived,
        products=load_json(INVENTORY_FILE), week_label=wl,
        total_archived=len(archived), is_archive_view=True)

@app.route("/forecast")
def forecast():
    if "user" not in session: return redirect(url_for("login"))
    all_sales = load_json(ARCHIVE_FILE) + load_json(SALES_FILE)
    categories_list = ["Jackets","T-shirts","Shorts"]
    current_monday  = get_current_week_monday()
    week_param = request.args.get("week","").strip()
    if week_param:
        try:
            ws = datetime.strptime(week_param,"%Y-%m-%d").date()
            ws = ws - timedelta(days=ws.weekday())
        except ValueError:
            ws = current_monday
    else:
        ws = current_monday
    we = ws + timedelta(days=6)
    daily_data = {(ws+timedelta(days=i)).strftime("%Y-%m-%d"): {c:0 for c in categories_list} for i in range(7)}
    for sale in all_sales:
        try:
            sd = datetime.strptime(sale.get("datetime","")[:10],"%Y-%m-%d").date()
        except ValueError: continue
        cat = get_category_from_sale(sale); qty = int(sale.get("quantity",0))
        key = sd.strftime("%Y-%m-%d")
        if cat in categories_list and key in daily_data: daily_data[key][cat] += qty
    hist = load_json(ARCHIVE_FILE)
    hdates = sorted(set(s.get("datetime","")[:10] for s in hist if s.get("datetime")))
    return render_template("forecast.html",
        daily_data=daily_data,
        date_range_label=f"{ws.strftime('%b %d')} \u2013 {we.strftime('%b %d, %Y')}",
        selected_week=ws.strftime("%Y-%m-%d"),
        is_current_week=(ws==current_monday),
        available_weeks=get_all_weeks(),
        hist_range=f"{hdates[0]} to {hdates[-1]}" if hdates else "N/A",
        hist_days=len(hdates))

@app.route("/pos")
def pos():
    if "user" not in session: return redirect(url_for("login"))
    return render_template("pos.html")

@app.route("/logout")
def logout():
    session.pop("user",None); return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
