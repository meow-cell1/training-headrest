import io, json, os, random, re, shutil, sqlite3, uuid
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, session, flash,
                   send_file, send_from_directory, abort, jsonify)
from jinja2 import DictLoader
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
UP = os.path.join(BASE, "uploads")
DB = os.path.join(BASE, "training.db")
os.makedirs(UP, exist_ok=True)

# ====== PENGATURAN (ubah di sini) ======
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")   # GANTI!
PASS_SCORE = 90                                                   # skor minimal lulus (%)
PORT = 5000
LEVELS = {"mudah": ("Mudah", 10), "normal": ("Normal", 6), "sulit": ("Sulit", 4)}   # detik per soal
# =======================================

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ganti-kunci-rahasia-ini")
app.permanent_session_lifetime = timedelta(hours=12)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
PHASE = {"pre": "Pre Test", "post": "Post Test"}


def q(sql, args=(), one=False):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, args)
        rows = cur.fetchall()
        con.commit()
        last = cur.lastrowid
    finally:
        con.close()
    if sql.lstrip().upper().startswith("INSERT"):
        return last
    return (rows[0] if rows else None) if one else rows


q("""CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY AUTOINCREMENT, phase TEXT, text TEXT,
     image TEXT, opts TEXT, ans_main INTEGER, ans_kid INTEGER, explain TEXT)""")
q("""CREATE TABLE IF NOT EXISTS materials(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
     filename TEXT, created TEXT)""")
q("""CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phase TEXT,
     score INTEGER, correct INTEGER, total INTEGER, detail TEXT, created TEXT)""")
_cols = {r["name"] for r in q("PRAGMA table_info(attempts)")}
for _c, _t in [("level", "TEXT"), ("esc", "INTEGER DEFAULT 0"), ("rej", "INTEGER DEFAULT 0"),
               ("kw", "INTEGER DEFAULT 0"), ("avgt", "REAL DEFAULT 0"), ("nik", "TEXT"), ("dur", "REAL DEFAULT 0")]:   # migrasi otomatis DB lama
    if _c not in _cols:
        q(f"ALTER TABLE attempts ADD COLUMN {_c} {_t}")
q("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, nik TEXT NOT NULL)")
q("""CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, nik TEXT, kind TEXT,
     detail TEXT, secs REAL, at TEXT)""")
EVENT = {"pre": "Pre Test", "buka": "Membuka materi", "baca": "Lama membaca materi", "post": "Post Test"}


@app.context_processor
def inject():
    return {"local_tw": os.path.exists(os.path.join(BASE, "static", "tailwind.js")),
            "levels": LEVELS, "EVENT": EVENT}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def log(kind, detail="", secs=None):
    q("INSERT INTO events(name,nik,kind,detail,secs,at) VALUES(?,?,?,?,?,?)",
      (session.get("name"), session.get("nik"), kind, detail, secs, now()))


@app.template_filter("dur")
def dur_filter(s):
    if s is None:
        return "-"
    m, sec = divmod(int(round(s)), 60)
    return f"{m} mnt {sec:02d} dtk" if m else f"{sec} dtk"


def save_file(f):
    if not f or not f.filename:
        return None
    name = uuid.uuid4().hex[:8] + "_" + secure_filename(f.filename)
    f.save(os.path.join(UP, name))
    return name


def drop_file(name):
    try:
        os.remove(os.path.join(UP, name))
    except (OSError, TypeError):
        pass


def load_qs(phase):
    out = []
    for r in q("SELECT * FROM questions WHERE phase=? ORDER BY id", (phase,)):
        d = dict(r)
        d["opts"] = json.loads(d["opts"])
        out.append(d)
    return out


def fmt(opts, m, k):
    if m is None or not 0 <= m < len(opts):
        return "-"
    s = opts[m]["label"]
    if k is not None and 0 <= k < len(opts[m]["kids"]):
        s += " › " + opts[m]["kids"][k]
    return s


def kind(label):
    s = label.strip().upper()
    return "ok" if s == "OK" else "ng" if s.startswith("NG") else None


def admin_only(fn):
    @wraps(fn)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return fn(*a, **k)
    return w


# ---------------- ALUR PESERTA ----------------
def progress():
    """Status tahap peserta: pre -> materi -> post."""
    c = {p: q("SELECT COUNT(*) c FROM questions WHERE phase=?", (p,), True)["c"] for p in PHASE}
    nm = q("SELECT COUNT(*) c FROM materials", (), True)["c"]
    pre = bool(session.get("pre")) or c["pre"] == 0
    mat = bool(session.get("mat")) or nm == 0
    return {"c": c, "nm": nm, "pre": pre, "mat": mat, "pre_done": bool(session.get("pre")),
            "mat_done": bool(session.get("mat")), "post_done": bool(session.get("post"))}


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        p = q("SELECT * FROM people WHERE id=?", (request.form.get("pid", 0),), True)
        if p and p["nik"].strip().lower() == request.form.get("nik", "").strip().lower():
            session.permanent = True
            for k in ("pre", "mat", "post", "mat_t0"):
                session.pop(k, None)
            session.update(name=p["name"], nik=p["nik"], pid=p["id"])
        else:
            flash("NIK tidak cocok dengan nama yang dipilih." if p else "Pilih nama terlebih dahulu.")
        return redirect(url_for("home"))
    p = progress()
    cards = [
        dict(step="Langkah 1", title="Pre Test", desc="Ukur kemampuan awal sebelum pelatihan.", href="/tes/pre",
             meta=f"{p['c']['pre']} soal", done=p["pre_done"], on=p["c"]["pre"] > 0 and not p["pre_done"], note=""),
        dict(step="Langkah 2", title="Materi", desc="Pelajari materi pelatihan.", href="/materi",
             meta=f"{p['nm']} materi", done=p["mat_done"], on=p["pre"], note="Selesaikan Pre Test dulu"),
        dict(step="Langkah 3", title="Post Test", desc="Ukur kemampuan setelah pelatihan.", href="/tes/post",
             meta=f"{p['c']['post']} soal", done=p["post_done"], on=p["pre"] and p["mat"] and p["c"]["post"] > 0
             and not p["post_done"], note="Buka materi dulu (Lihat / Unduh)"),
    ]
    for c in cards:
        c["lock"] = not c["on"] and not c["done"]
    return render_template("home.html", cards=cards, people=q("SELECT id,name FROM people ORDER BY name COLLATE NOCASE"), all_done=p["pre_done"] and p["mat_done"] and p["post_done"])


@app.get("/selesai")
def finish():
    for k in ("name", "nik", "pid", "pre", "mat", "post", "mat_t0"):
        session.pop(k, None)
    return redirect(url_for("home"))


@app.get("/materi")
def materi():
    if not session.get("name"):
        return redirect(url_for("home"))
    p = progress()
    if not p["pre"]:
        flash("Selesaikan Pre Test terlebih dahulu.")
        return redirect(url_for("home"))
    return render_template("materi.html", mats=q("SELECT * FROM materials ORDER BY id DESC"),
                           post_open=p["mat"] and p["c"]["post"] > 0 and not p["post_done"])


@app.get("/materi/buka/<int:mid>")
def materi_open(mid):
    if not session.get("name") or not progress()["pre"]:
        return redirect(url_for("home"))
    r = q("SELECT * FROM materials WHERE id=?", (mid,), True)
    if not r:
        abort(404)
    session["mat"] = True
    session.setdefault("mat_t0", datetime.now().timestamp())     # awal membaca materi
    dl = bool(request.args.get("dl"))
    log("buka", f"{r['title']} ({'Unduh' if dl else 'Lihat'})")
    return send_from_directory(UP, r["filename"], as_attachment=dl,
                               download_name=r["filename"].split("_", 1)[-1])


@app.get("/file/<name>")
def files(name):
    return send_from_directory(UP, name)


def gate(phase):
    """Kembalikan alasan penolakan, atau None kalau boleh mengerjakan."""
    p = progress()
    if not session.get("name"):
        return ""
    if phase == "pre" and p["pre_done"]:
        return "Pre Test sudah selesai."
    if phase == "post" and not (p["pre"] and p["mat"]):
        return "Selesaikan Pre Test dan buka materi terlebih dahulu."
    if phase == "post" and p["post_done"]:
        return "Post Test sudah selesai."
    return None


@app.get("/tes/<phase>")
def quiz(phase):
    if phase not in PHASE:
        abort(404)
    why = gate(phase)
    if why is not None:
        if why:
            flash(why)
        return redirect(url_for("home"))
    if phase == "post" and session.get("mat_t0"):                  # catat lama membaca materi
        log("baca", "Sejak membuka materi pertama sampai mulai Post Test",
            datetime.now().timestamp() - session.pop("mat_t0"))
    qs = load_qs(phase)
    random.shuffle(qs)                      # urutan soal acak per peserta
    data = [{"id": x["id"], "text": x["text"], "image": x["image"], "opts": x["opts"]} for x in qs]
    return render_template("quiz.html", title=PHASE[phase], phase=phase, data=data)


@app.post("/tes/<phase>/kirim")
def quiz_submit(phase):
    if phase not in PHASE or gate(phase) is not None:
        abort(403)
    body = request.get_json(force=True)
    lv = body.get("level") if body.get("level") in LEVELS else "normal"
    limit = LEVELS[lv][1]
    qmap = {x["id"]: x for x in load_qs(phase)}
    det, ok, esc, rej, kw, times = [], 0, 0, 0, 0, []
    for a in body.get("answers", []):
        x = qmap.get(a.get("id"))
        if not x:
            continue
        try:
            m, k = int(a.get("m", -1)), int(a.get("k", -1))
            t = min(max(float(a.get("t", limit)), 0), limit)
        except (TypeError, ValueError):
            continue
        times.append(t)
        opts = x["opts"]
        truth = kind(opts[x["ans_main"]]["label"])
        mk = kind(opts[m]["label"]) if 0 <= m < len(opts) else None
        right = m == x["ans_main"] and (x["ans_kid"] is None or k == x["ans_kid"])
        ok += right
        if truth == "ng" and (m == -1 or mk == "ok"):
            esc += 1                         # NG lolos
        elif truth == "ok" and mk == "ng":
            rej += 1                         # OK salah dibuang
        elif truth == "ng" and m == x["ans_main"] and not right:
            kw += 1                          # jenis NG salah
        det.append({"text": x["text"], "image": x["image"], "explain": x["explain"], "good": right, "t": round(t, 1),
                    "yours": fmt(opts, m, k) if m >= 0 else "Tidak menjawab (waktu habis)",
                    "key": fmt(opts, x["ans_main"], x["ans_kid"])})
    total = len(det)
    score = round(ok / total * 100) if total else 0
    avg = round(sum(times) / len(times), 1) if times else 0
    dur = round(sum(times), 1)
    aid = q("""INSERT INTO attempts(name,phase,score,correct,total,detail,created,level,esc,rej,kw,avgt,nik,dur)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session["name"], phase, score, ok, total, json.dumps(det), now(), lv, esc, rej, kw, avg,
             session.get("nik"), dur))
    log(phase, f"Skor {score}% · level {LEVELS[lv][0]}", dur)
    session[phase] = True
    return jsonify(url=url_for("result", aid=aid))


@app.get("/hasil/<int:aid>")
def result(aid):
    a = q("SELECT * FROM attempts WHERE id=?", (aid,), True)
    if not a:
        abort(404)
    if not session.get("admin") and a["nik"] != session.get("nik"):
        abort(403)
    passed = a["score"] >= PASS_SCORE and (a["esc"] or 0) == 0
    return render_template("result.html", a=a, det=json.loads(a["detail"]), label=PHASE[a["phase"]], passed=passed,
                           PASS=PASS_SCORE, review=a["phase"] == "post" or session.get("admin"))


# ---------------- ADMIN ----------------
@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("dashboard"))
        flash("Password salah.")
    return render_template("login.html")


@app.get("/admin/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("home"))


def history():
    ph, s = request.args.get("phase", ""), request.args.get("q", "").strip()
    sql, a = "SELECT * FROM attempts WHERE 1=1", []
    if ph in PHASE:
        sql += " AND phase=?"; a.append(ph)
    if s:
        sql += " AND name LIKE ?"; a.append(f"%{s}%")
    return q(sql + " ORDER BY id DESC", a), ph, s


@app.get("/admin")
@admin_only
def dashboard():
    rows, ph, s = history()
    st = {r["phase"]: r for r in q("SELECT phase, COUNT(*) c, ROUND(AVG(score)) m FROM attempts GROUP BY phase")}
    people = q("SELECT COUNT(DISTINCT name) c FROM attempts", (), True)["c"]
    return render_template("admin_dash.html", rows=rows, ph=ph, s=s, st=st, people=people,
                           total=q("SELECT COUNT(*) c FROM attempts", (), True)["c"], PHASE=PHASE, PASS=PASS_SCORE)


@app.get("/admin/riwayat.xlsx")
@admin_only
def export():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    rows, _, _ = history()
    wb = Workbook()
    ws, ws2 = wb.active, wb.create_sheet("Pre-Post per Peserta")
    ws3, ws4 = wb.create_sheet("Detail Jawaban"), wb.create_sheet("Aktivitas")
    ws.title = "Riwayat"
    ws.append(["Tanggal", "Nama", "NIK", "Tipe", "Level", "Benar", "Total", "Skor (%)", "NG lolos", "OK dianggap NG",
               "Jenis NG salah", "Rata2 waktu/soal (dtk)", "Durasi pengerjaan (dtk)", "Status"])
    ws3.append(["Tanggal", "Nama", "NIK", "Tipe", "Soal", "Jawaban peserta", "Kunci", "Hasil", "Waktu (dtk)"])
    ws4.append(["Waktu", "Nama", "NIK", "Aktivitas", "Detail", "Durasi (dtk)"])
    per = {}
    for r in rows[::-1]:
        passed = r["score"] >= PASS_SCORE and (r["esc"] or 0) == 0
        ws.append([r["created"], r["name"], r["nik"], PHASE[r["phase"]], LEVELS.get(r["level"], ("-",))[0], r["correct"],
                   r["total"], r["score"], r["esc"] or 0, r["rej"] or 0, r["kw"] or 0, r["avgt"] or 0, r["dur"] or 0,
                   "LULUS" if passed else "BELUM LULUS"])
        per.setdefault(r["name"], {})[r["phase"]] = r["score"]
        for d in json.loads(r["detail"]):
            ws3.append([r["created"], r["name"], r["nik"], PHASE[r["phase"]], d["text"], d["yours"], d["key"],
                        "Benar" if d["good"] else "Salah", d.get("t")])
    ws2.append(["Nama", "Pre Test", "Post Test", "Kenaikan"])
    for n, d in per.items():
        ws2.append([n, d.get("pre"), d.get("post"), d["post"] - d["pre"] if "pre" in d and "post" in d else None])
    for e in q("SELECT * FROM events ORDER BY id"):
        ws4.append([e["at"], e["name"], e["nik"], EVENT.get(e["kind"], e["kind"]), e["detail"],
                    round(e["secs"]) if e["secs"] is not None else None])
    for sh in (ws, ws2, ws3, ws4):
        for c in sh[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="4F46E5")
        for col in sh.columns:
            sh.column_dimensions[col[0].column_letter].width = 22
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"riwayat-{datetime.now():%Y%m%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/soal/<phase>", methods=["GET", "POST"])
@admin_only
def soal(phase):
    if phase not in PHASE:
        abort(404)
    if request.method == "POST":
        f, opts, slot, star = request.form, [], {}, {}
        for i in range(4):
            lab = f.get(f"label{i}", "").strip()
            if not lab:
                continue
            kids, st = [], None
            for line in f.get(f"kids{i}", "").splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("*"):
                    line, st = line[1:].strip(), len(kids)
                kids.append(line)
            slot[i], star[i] = len(opts), st
            opts.append({"label": lab, "kids": kids})
        c = int(f.get("correct", 0))
        if len(opts) < 2 or c not in slot:
            flash("Isi minimal 2 opsi dan pilih salah satu sebagai jawaban benar.")
        elif opts[slot[c]]["kids"] and star[c] is None:
            flash("Opsi benar punya anak jawaban: awali satu baris dengan * untuk menandai yang benar.")
        else:
            q("INSERT INTO questions(phase,text,image,opts,ans_main,ans_kid,explain) VALUES(?,?,?,?,?,?,?)",
              (phase, f["text"].strip(), save_file(request.files.get("image")), json.dumps(opts),
               slot[c], star[c], f.get("explain", "").strip()))
            flash("Soal ditambahkan.")
        return redirect(url_for("soal", phase=phase))
    other = "post" if phase == "pre" else "pre"
    return render_template("admin_soal.html", phase=phase, title=PHASE[phase], qs=load_qs(phase),
                           other=other, other_title=PHASE[other], src=load_qs(other))


def copy_q(r, phase):
    """Salin satu soal (beserta fotonya) ke Pre/Post Test."""
    img = r["image"]
    if img:
        new = uuid.uuid4().hex[:8] + "_" + img.split("_", 1)[-1]
        try:
            shutil.copy(os.path.join(UP, img), os.path.join(UP, new))
            img = new
        except OSError:
            img = None
    q("INSERT INTO questions(phase,text,image,opts,ans_main,ans_kid,explain) VALUES(?,?,?,?,?,?,?)",
      (phase, r["text"], img, r["opts"], r["ans_main"], r["ans_kid"], r["explain"]))


@app.post("/admin/soal/<phase>/salin")
@admin_only
def soal_copy(phase):
    if phase not in PHASE:
        abort(404)
    n = 0
    for i in request.form.getlist("ids"):
        r = q("SELECT * FROM questions WHERE id=?", (i,), True)
        if r and r["phase"] != phase:
            copy_q(r, phase)
            n += 1
    flash(f"{n} soal disalin ke {PHASE[phase]}." if n else "Pilih soal yang ingin disalin.")
    return redirect(url_for("soal", phase=phase))


@app.post("/admin/soal/satu/<int:qid>/<to>")
@admin_only
def soal_copy_one(qid, to):
    r = q("SELECT * FROM questions WHERE id=?", (qid,), True)
    if r and to in PHASE:
        copy_q(r, to)
        flash(("Soal diduplikat di " if to == r["phase"] else "Soal disalin ke ") + PHASE[to] + ".")
    return redirect(url_for("soal", phase=r["phase"] if r else "pre"))


@app.post("/admin/soal/hapus/<int:qid>")
@admin_only
def soal_del(qid):
    r = q("SELECT * FROM questions WHERE id=?", (qid,), True)
    if r:
        drop_file(r["image"])
        q("DELETE FROM questions WHERE id=?", (qid,))
    return redirect(url_for("soal", phase=r["phase"] if r else "pre"))


@app.route("/admin/materi", methods=["GET", "POST"])
@admin_only
def admin_materi():
    if request.method == "POST":
        name = save_file(request.files.get("file"))
        if name:
            q("INSERT INTO materials(title,filename,created) VALUES(?,?,?)",
              (request.form.get("title", "").strip() or name, name, now()))
            flash("Materi diunggah.")
        return redirect(url_for("admin_materi"))
    return render_template("admin_materi.html", mats=q("SELECT * FROM materials ORDER BY id DESC"))


@app.post("/admin/materi/hapus/<int:mid>")
@admin_only
def materi_del(mid):
    r = q("SELECT * FROM materials WHERE id=?", (mid,), True)
    if r:
        drop_file(r["filename"])
        q("DELETE FROM materials WHERE id=?", (mid,))
    return redirect(url_for("admin_materi"))


@app.route("/admin/peserta", methods=["GET", "POST"])
@admin_only
def admin_people():
    if request.method == "POST":
        rows = []
        if request.form.get("bulk", "").strip():
            for line in request.form["bulk"].splitlines():
                p = [x.strip() for x in re.split(r"[\t;,]", line) if x.strip()]
                if len(p) >= 2:
                    rows.append((p[0], p[1]))
        elif request.form.get("name", "").strip() and request.form.get("nik", "").strip():
            rows.append((request.form["name"].strip(), request.form["nik"].strip()))
        add = skip = 0
        for n, k in rows:
            if q("SELECT id FROM people WHERE LOWER(nik)=LOWER(?)", (k,), True):
                skip += 1
            else:
                q("INSERT INTO people(name,nik) VALUES(?,?)", (n, k))
                add += 1
        flash(f"{add} peserta ditambahkan" + (f", {skip} dilewati (NIK sudah ada)." if skip else ".")
              if rows else "Isi nama dan NIK.")
        return redirect(url_for("admin_people"))
    return render_template("admin_people.html", people=q("SELECT * FROM people ORDER BY name COLLATE NOCASE"))


@app.post("/admin/peserta/hapus/<int:pid>")
@admin_only
def people_del(pid):
    q("DELETE FROM people WHERE id=?", (pid,))
    return redirect(url_for("admin_people"))


@app.get("/admin/aktivitas")
@admin_only
def activity():
    s, k = request.args.get("q", "").strip(), request.args.get("kind", "")
    sql, a = "SELECT * FROM events WHERE 1=1", []
    if s:
        sql += " AND (name LIKE ? OR nik LIKE ?)"
        a += [f"%{s}%"] * 2
    if k in EVENT:
        sql += " AND kind=?"
        a.append(k)
    return render_template("admin_activity.html", rows=q(sql + " ORDER BY id DESC LIMIT 500", a), s=s, k=k)


# =====================================================================
#  TEMPLATE (Tailwind) - semua tertanam di sini, tidak perlu folder templates/
# =====================================================================
TEMPLATES = {}

TEMPLATES["base.html"] = r'''<!doctype html>
<html lang="id" class="h-full">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Pelatihan{% endblock %} · Detection Training</title>
  {% if local_tw %}<script src="{{ url_for('static', filename='tailwind.js') }}"></script>
  {% else %}<script src="https://cdn.tailwindcss.com"></script>{% endif %}
  <script>
    tailwind.config = {theme: {extend: {
      fontFamily: {sans: ['ui-sans-serif','system-ui','Segoe UI','Roboto','Helvetica Neue','sans-serif']},
      colors: {brand: {50:'#eef2ff',100:'#e0e7ff',500:'#6366f1',600:'#4f46e5',700:'#4338ca'}}
    }}}
  </script>
  <style type="text/tailwindcss">
    @layer components {
      .card {@apply bg-white rounded-2xl border border-slate-200 shadow-sm}
      .btn {@apply inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold transition focus:outline-none focus:ring-4 focus:ring-brand-100 disabled:opacity-50}
      .btn-primary {@apply bg-brand-600 text-white hover:bg-brand-700}
      .btn-ghost {@apply bg-white text-slate-700 border border-slate-200 hover:bg-slate-50}
      .btn-danger {@apply bg-white text-rose-600 border border-rose-200 hover:bg-rose-50}
      .input {@apply w-full rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-sm placeholder:text-slate-400 focus:border-brand-500 focus:outline-none focus:ring-4 focus:ring-brand-100}
      .label {@apply block text-xs font-semibold uppercase tracking-wide text-slate-500 mb-1.5}
      .th {@apply px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-slate-500 whitespace-nowrap}
      .td {@apply px-4 py-3 text-sm}
      .pill {@apply inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold}
    }
  </style>
</head>
<body class="h-full bg-slate-50 font-sans text-slate-800 antialiased">

{% set is_admin_area = session.get('admin') and request.path.startswith('/admin') %}
<header class="sticky top-0 z-20 border-b border-slate-200 bg-white/80 backdrop-blur">
  <div class="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
    <a href="/" class="flex items-center gap-2.5">
      <span class="grid h-9 w-9 place-items-center rounded-xl bg-brand-600 text-white">
        <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"/></svg>
      </span>
      <span class="leading-tight"><span class="block text-sm font-bold">Detection Training</span>
      <span class="block text-xs text-slate-500">Pengecekan OK / NG</span></span>
    </a>
    <div class="flex items-center gap-3 text-sm">
      {% if session.get('name') and not is_admin_area %}
        <span class="hidden sm:block text-slate-500">{{ session.name }}</span>
        <a href="/selesai" class="btn btn-ghost !py-1.5">Keluar</a>
      {% endif %}
      {% if session.get('admin') %}
        <a href="/admin" class="btn btn-ghost !py-1.5">Admin</a>
      {% elif not session.get('name') %}
        <a href="/admin/login" class="text-slate-400 hover:text-slate-600">Admin</a>
      {% endif %}
    </div>
  </div>
  {% if is_admin_area %}
  {% set nav = [('/admin','Dashboard'),('/admin/soal/pre','Soal Pre'),('/admin/soal/post','Soal Post'),('/admin/materi','Materi'),('/admin/peserta','Peserta'),('/admin/aktivitas','Aktivitas')] %}
  <nav class="mx-auto flex max-w-6xl gap-1 overflow-x-auto px-4 pb-2">
    {% for href, t in nav %}
      <a href="{{ href }}" class="whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium {{ 'bg-brand-50 text-brand-700' if request.path == href else 'text-slate-500 hover:bg-slate-100' }}">{{ t }}</a>
    {% endfor %}
    <a href="/admin/logout" class="ml-auto whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium text-rose-600 hover:bg-rose-50">Logout</a>
  </nav>
  {% endif %}
</header>

<main class="mx-auto px-4 py-8 {% block width %}max-w-3xl{% endblock %}">
  {% with msgs = get_flashed_messages() %}
    {% for m in msgs %}
      <div class="mb-4 flex items-start gap-3 rounded-xl border border-brand-100 bg-brand-50 px-4 py-3 text-sm text-brand-700">
        <svg class="mt-0.5 h-4 w-4 shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Zm-7-4a1 1 0 1 1-2 0 1 1 0 0 1 2 0ZM9 9a1 1 0 0 0 0 2v3a1 1 0 0 0 1 1h1a1 1 0 1 0 0-2v-3a1 1 0 0 0-1-1H9Z" clip-rule="evenodd"/></svg>
        <span>{{ m }}</span>
      </div>
    {% endfor %}
  {% endwith %}
  {% block content %}{% endblock %}
</main>
</body>
</html>
'''

TEMPLATES["home.html"] = r'''{% extends "base.html" %}
{% block title %}Beranda{% endblock %}
{% block content %}
{% if not session.get('name') %}
  <div class="mx-auto max-w-md pt-6">
    <div class="mb-8 text-center">
      <h1 class="text-3xl font-bold tracking-tight">Latihan Deteksi OK / NG</h1>
      <p class="mt-2 text-slate-500">Pilih nama dan masukkan NIK untuk memulai.</p>
    </div>
    <form method="post" class="card space-y-4 p-6">
      <div>
        <label class="label">Nama</label>
        <select name="pid" required class="input">
          <option value="">Pilih nama…</option>
          {% for p in people %}<option value="{{ p.id }}">{{ p.name }}</option>{% endfor %}
        </select>
      </div>
      <div>
        <label class="label">NIK</label>
        <input name="nik" required autocomplete="off" placeholder="Masukkan NIK" class="input">
      </div>
      <button class="btn btn-primary w-full">Masuk</button>
    </form>
  </div>
{% else %}
  <div class="mb-8">
    <p class="text-sm text-slate-500">Halo,</p>
    <h1 class="text-3xl font-bold tracking-tight">{{ session.name }}</h1>
  </div>

  <div class="space-y-3">
    {% for c in cards %}
      {% set tag = 'a' if c.on else 'div' %}
      <{{ tag }} {% if c.on %}href="{{ c.href }}"{% endif %}
         class="card group flex items-center gap-4 p-5 transition {{ 'hover:border-brand-500 hover:shadow-md' if c.on else '' }} {{ 'opacity-60' if c.lock else '' }}">
        <div class="grid h-12 w-12 shrink-0 place-items-center rounded-xl text-lg font-bold
             {{ 'bg-emerald-100 text-emerald-600' if c.done else ('bg-brand-50 text-brand-600' if c.on else 'bg-slate-100 text-slate-400') }}">
          {% if c.done %}✓{% else %}{{ loop.index }}{% endif %}
        </div>
        <div class="min-w-0 flex-1">
          <p class="text-xs font-semibold uppercase tracking-wide text-slate-400">{{ c.step }} · {{ c.meta }}</p>
          <h2 class="text-lg font-semibold">{{ c.title }}</h2>
          <p class="text-sm text-slate-500">{{ c.desc }}{% if c.lock and c.note %} <span class="text-amber-600">🔒 {{ c.note }}</span>{% endif %}</p>
        </div>
        {% if c.done %}<span class="pill bg-emerald-100 text-emerald-700">Selesai</span>
        {% elif c.on %}<span class="text-brand-600 transition group-hover:translate-x-1">→</span>{% endif %}
      </{{ tag }}>
    {% endfor %}
  </div>

  {% if all_done %}
    <div class="card mt-6 p-6 text-center">
      <p class="text-2xl">🎉</p>
      <h3 class="mt-1 text-lg font-semibold">Semua tahap selesai</h3>
      <p class="mt-1 text-sm text-slate-500">Terima kasih sudah mengikuti pelatihan.</p>
      <a href="/selesai" class="btn btn-primary mt-4">Selesai</a>
    </div>
  {% endif %}
{% endif %}
{% endblock %}
'''

TEMPLATES["materi.html"] = r'''{% extends "base.html" %}
{% block title %}Materi{% endblock %}
{% block content %}
<a href="/" class="text-sm text-slate-500 hover:text-slate-800">← Kembali</a>
<h1 class="mt-2 text-2xl font-bold">Materi Pelatihan</h1>
<p class="mt-1 text-sm text-slate-500">Buka (Lihat atau Unduh) minimal satu materi sebelum Post Test.</p>

<div class="mt-6 space-y-3">
  {% for m in mats %}
  <div class="card flex flex-wrap items-center gap-4 p-4">
    <div class="grid h-11 w-11 place-items-center rounded-xl bg-brand-50 text-brand-600">
      <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z"/></svg>
    </div>
    <div class="min-w-0 flex-1">
      <p class="truncate font-semibold">{{ m.title }}</p>
      <p class="text-xs text-slate-400">{{ m.created }}</p>
    </div>
    <div class="flex gap-2">
      <a href="/materi/buka/{{ m.id }}" target="_blank" class="btn btn-primary !py-2">Lihat</a>
      <a href="/materi/buka/{{ m.id }}?dl=1" class="btn btn-ghost !py-2">Unduh</a>
    </div>
  </div>
  {% else %}
  <div class="card p-8 text-center text-slate-500">Belum ada materi.</div>
  {% endfor %}
</div>

{% if post_open %}
<div class="card mt-6 flex flex-wrap items-center justify-between gap-3 border-brand-100 bg-brand-50 p-5">
  <div><p class="font-semibold text-brand-700">Siap untuk Post Test?</p>
  <p class="text-sm text-brand-600/80">Waktu membaca materi dicatat sampai kamu mulai tes.</p></div>
  <a href="/tes/post" class="btn btn-primary">Mulai Post Test →</a>
</div>
{% endif %}
{% endblock %}
'''

TEMPLATES["quiz.html"] = r'''{% extends "base.html" %}
{% block title %}{{ title }}{% endblock %}
{% block content %}
<div class="mx-auto max-w-2xl">

  <section id="intro" class="card p-6 sm:p-8">
    <p class="text-xs font-semibold uppercase tracking-wide text-brand-600">{{ title }}</p>
    <h1 class="mt-1 text-2xl font-bold">Siap mulai?</h1>
    <p class="mt-2 text-sm text-slate-500">
      {{ data|length }} soal. Setiap soal ada batas waktu. Pilih jawaban secepat dan setepat mungkin;
      jawaban terkirim otomatis. Tips: tekan angka <kbd class="rounded border px-1">1</kbd>–<kbd class="rounded border px-1">4</kbd> di keyboard.
    </p>
    <div class="mt-6">
      <span class="label">Tingkat kesulitan</span>
      <div class="grid grid-cols-3 gap-3">
        {% for key, v in levels.items() %}
        <label class="cursor-pointer">
          <input type="radio" name="level" value="{{ key }}" class="peer sr-only" {{ 'checked' if key == 'normal' }}>
          <div class="rounded-xl border border-slate-200 p-3 text-center transition peer-checked:border-brand-600 peer-checked:bg-brand-50 peer-checked:ring-4 peer-checked:ring-brand-100">
            <div class="font-semibold">{{ v[0] }}</div>
            <div class="text-xs text-slate-500">{{ v[1] }} dtk / soal</div>
          </div>
        </label>
        {% endfor %}
      </div>
    </div>
    <button id="start" class="btn btn-primary mt-6 w-full !py-3">Mulai</button>
  </section>

  <section id="play" class="hidden">
    <div class="mb-3 flex items-center justify-between text-sm">
      <span id="counter" class="font-semibold text-slate-600"></span>
      <span id="timeTxt" class="font-semibold tabular-nums text-slate-600"></span>
    </div>
    <div class="h-2 overflow-hidden rounded-full bg-slate-200">
      <div id="bar" class="h-full w-full rounded-full bg-brand-600"></div>
    </div>
    <div class="card mt-4 p-5 sm:p-6">
      <h2 id="qtext" class="text-lg font-semibold"></h2>
      <div id="qimg" class="mt-4 hidden">
        <img alt="" class="max-h-[420px] w-full rounded-xl bg-slate-100 object-contain">
      </div>
      <p id="hint" class="label mt-6"></p>
      <div id="opts" class="grid gap-2.5 sm:grid-cols-2"></div>
    </div>
  </section>

  <section id="sending" class="card hidden p-10 text-center">
    <div class="mx-auto h-8 w-8 animate-spin rounded-full border-4 border-brand-100 border-t-brand-600"></div>
    <p class="mt-4 font-semibold">Mengirim jawaban…</p>
    <button id="retry" class="btn btn-primary mt-4 hidden">Coba kirim lagi</button>
  </section>
</div>

<script>
const DATA = {{ data|tojson }};
const LEVELS = {{ levels|tojson }};
const SUBMIT = "/tes/{{ phase }}/kirim";
const IMG = "/file/";
const $ = id => document.getElementById(id);

let level, limit, i = 0, answers = [], t0, timer, cur, locked = true, playing = false;

$('start').onclick = () => {
  level = document.querySelector('input[name=level]:checked').value;
  limit = LEVELS[level][1];
  $('intro').classList.add('hidden');
  $('play').classList.remove('hidden');
  playing = true;
  show();
};

function renderOpts(items, hint, onPick) {
  $('hint').textContent = hint;
  const box = $('opts'); box.innerHTML = '';
  items.forEach((it, n) => {
    const b = document.createElement('button');
    b.className = 'flex items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 text-left text-sm font-medium transition hover:border-brand-500 hover:bg-brand-50 active:scale-[.98]';
    const k = document.createElement('span');
    k.className = 'grid h-6 w-6 shrink-0 place-items-center rounded-md bg-slate-100 text-xs font-bold text-slate-500';
    k.textContent = n + 1;
    const t = document.createElement('span'); t.textContent = it.label;
    b.append(k, t);
    b.onclick = () => onPick(it.idx);
    box.appendChild(b);
  });
}

function show() {
  const d = DATA[i];
  cur = {m: -1, k: -1}; locked = true;
  clearInterval(timer);
  $('counter').textContent = `Soal ${i + 1} / ${DATA.length}`;
  $('qtext').textContent = d.text;
  $('bar').style.width = '100%';
  $('timeTxt').textContent = limit + ' dtk';
  $('opts').innerHTML = ''; $('hint').textContent = 'Memuat…';
  const go = () => {
    renderOpts(d.opts.map((o, idx) => ({label: o.label, idx})), 'Pilih jawaban', pickMain);
    startTimer();
  };
  const box = $('qimg');
  if (d.image) {
    const im = box.firstElementChild;
    box.classList.remove('hidden');
    im.onload = im.onerror = go;
    im.src = IMG + d.image;
  } else { box.classList.add('hidden'); go(); }
}

function pickMain(idx) {
  if (locked) return;
  cur.m = idx;
  const o = DATA[i].opts[idx];
  if (o.kids.length) {
    renderOpts(o.kids.map((l, k) => ({label: l, idx: k})), 'Jenis: ' + o.label, k => { cur.k = k; finish(); });
  } else finish();
}

function startTimer() {
  locked = false; t0 = performance.now();
  timer = setInterval(() => {
    const left = Math.max(limit - (performance.now() - t0) / 1000, 0);
    $('bar').style.width = (left / limit * 100) + '%';
    $('bar').className = 'h-full rounded-full ' + (left < limit * 0.3 ? 'bg-rose-500' : 'bg-brand-600');
    $('timeTxt').textContent = left.toFixed(1) + ' dtk';
    if (left <= 0) finish();
  }, 100);
}

function finish() {
  if (locked) return;
  locked = true; clearInterval(timer);
  const t = Math.min((performance.now() - t0) / 1000, limit);
  answers.push({id: DATA[i].id, m: cur.m, k: cur.k, t: +t.toFixed(2)});
  i++;
  i < DATA.length ? show() : send();
}

function send() {
  playing = false;
  $('play').classList.add('hidden'); $('sending').classList.remove('hidden');
  $('retry').classList.add('hidden');
  fetch(SUBMIT, {method: 'POST', headers: {'Content-Type': 'application/json'},
                 body: JSON.stringify({level, answers})})
    .then(r => { if (!r.ok) throw 0; return r.json(); })
    .then(j => location.href = j.url)
    .catch(() => { $('retry').classList.remove('hidden'); });
}
$('retry').onclick = send;

document.addEventListener('keydown', e => {
  if (!playing || locked) return;
  const n = parseInt(e.key, 10);
  const btns = $('opts').children;
  if (n >= 1 && n <= btns.length) btns[n - 1].click();
});
window.addEventListener('beforeunload', e => { if (playing) { e.preventDefault(); e.returnValue = ''; } });
</script>
{% endblock %}
'''

TEMPLATES["result.html"] = r'''{% extends "base.html" %}
{% block title %}Hasil {{ label }}{% endblock %}
{% block content %}
<div class="card p-6 text-center sm:p-8">
  <p class="text-xs font-semibold uppercase tracking-wide text-slate-400">{{ label }} · {{ a['name'] }}</p>
  <p class="mt-3 text-6xl font-bold tracking-tight {{ 'text-emerald-600' if passed else 'text-rose-600' }}">{{ a['score'] }}<span class="text-3xl">%</span></p>
  <span class="pill mt-3 !px-4 !py-1 text-sm {{ 'bg-emerald-100 text-emerald-700' if passed else 'bg-rose-100 text-rose-700' }}">
    {{ 'LULUS' if passed else 'BELUM LULUS' }}
  </span>
  <p class="mt-3 text-xs text-slate-400">Syarat lulus: skor ≥ {{ PASS }}% dan tidak ada NG yang lolos.</p>

  <div class="mt-6 grid grid-cols-2 gap-3 text-left sm:grid-cols-3">
    {% set stats = [
      ('Benar', a['correct'] ~ ' / ' ~ a['total'], ''),
      ('Rata-rata waktu', (a['avgt'] or 0) ~ ' dtk', ''),
      ('Level', levels[a['level']][0] if a['level'] in levels else '-', ''),
      ('NG lolos', a['esc'] or 0, 'text-rose-600' if (a['esc'] or 0) else ''),
      ('OK dianggap NG', a['rej'] or 0, 'text-amber-600' if (a['rej'] or 0) else ''),
      ('Jenis NG salah', a['kw'] or 0, 'text-amber-600' if (a['kw'] or 0) else '')] %}
    {% for t, v, c in stats %}
    <div class="rounded-xl bg-slate-50 p-3">
      <p class="text-xs text-slate-500">{{ t }}</p>
      <p class="text-lg font-semibold {{ c }}">{{ v }}</p>
    </div>
    {% endfor %}
  </div>

  <div class="mt-6 flex flex-wrap justify-center gap-3">
    {% if session.get('admin') %}<a href="/admin" class="btn btn-ghost">← Dashboard</a>{% endif %}
    {% if a['phase'] == 'pre' %}<a href="/" class="btn btn-primary">Lanjut ke Materi →</a>
    {% else %}<a href="/" class="btn btn-primary">Kembali ke Beranda</a>{% endif %}
  </div>
</div>

{% if review %}
<h2 class="mb-3 mt-8 text-lg font-semibold">Pembahasan</h2>
<div class="space-y-3">
  {% for d in det %}
  <div class="card overflow-hidden {{ 'border-emerald-200' if d.good else 'border-rose-200' }}">
    <div class="flex items-center justify-between px-4 py-2 text-xs font-semibold {{ 'bg-emerald-50 text-emerald-700' if d.good else 'bg-rose-50 text-rose-700' }}">
      <span>Soal {{ loop.index }} · {{ 'Benar' if d.good else 'Salah' }}</span><span>{{ d.t }} dtk</span>
    </div>
    <div class="flex flex-col gap-4 p-4 sm:flex-row">
      {% if d.image %}<img src="/file/{{ d.image }}" class="h-32 w-full rounded-xl bg-slate-100 object-contain sm:w-44" alt="">{% endif %}
      <div class="min-w-0 flex-1 space-y-1.5 text-sm">
        <p class="font-medium">{{ d.text }}</p>
        <p><span class="text-slate-400">Jawabanmu:</span> <span class="font-medium {{ 'text-emerald-700' if d.good else 'text-rose-700' }}">{{ d.yours }}</span></p>
        {% if not d.good %}<p><span class="text-slate-400">Kunci:</span> <span class="font-medium text-emerald-700">{{ d.key }}</span></p>{% endif %}
        {% if d.explain %}<p class="rounded-lg bg-slate-50 p-2.5 text-slate-600">{{ d.explain }}</p>{% endif %}
      </div>
    </div>
  </div>
  {% endfor %}
</div>
{% endif %}
{% endblock %}
'''

TEMPLATES["login.html"] = r'''{% extends "base.html" %}
{% block title %}Login Admin{% endblock %}
{% block content %}
<div class="mx-auto max-w-sm pt-10">
  <h1 class="mb-6 text-center text-2xl font-bold">Login Admin</h1>
  <form method="post" class="card space-y-4 p-6">
    <div>
      <label class="label">Password</label>
      <input type="password" name="password" required autofocus class="input">
    </div>
    <button class="btn btn-primary w-full">Masuk</button>
  </form>
</div>
{% endblock %}
'''

TEMPLATES["admin_dash.html"] = r'''{% extends "base.html" %}
{% block title %}Dashboard{% endblock %}
{% block width %}max-w-6xl{% endblock %}
{% block content %}
{% set pre = st.get('pre') %}{% set post = st.get('post') %}
<div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
  {% set cards = [('Total percobaan', total), ('Peserta', people),
                  ('Rata-rata Pre', (pre.m|int ~ '%') if pre else '-'),
                  ('Rata-rata Post', (post.m|int ~ '%') if post else '-')] %}
  {% for t, v in cards %}
  <div class="card p-5"><p class="text-xs font-semibold uppercase tracking-wide text-slate-400">{{ t }}</p>
  <p class="mt-1 text-3xl font-bold">{{ v }}</p></div>
  {% endfor %}
</div>

<div class="mt-8 flex flex-wrap items-end justify-between gap-3">
  <form class="flex flex-wrap gap-2" method="get">
    <select name="phase" class="input !w-auto">
      <option value="">Semua tipe</option>
      {% for k, v in PHASE.items() %}<option value="{{ k }}" {{ 'selected' if ph == k }}>{{ v }}</option>{% endfor %}
    </select>
    <input name="q" value="{{ s }}" placeholder="Cari nama…" class="input !w-56">
    <button class="btn btn-ghost">Filter</button>
  </form>
  <a href="/admin/riwayat.xlsx?phase={{ ph }}&q={{ s }}" class="btn btn-primary">Unduh Excel</a>
</div>

<div class="card mt-4 overflow-x-auto">
  <table class="min-w-full divide-y divide-slate-100">
    <thead class="bg-slate-50"><tr>
      <th class="th">Tanggal</th><th class="th">Nama</th><th class="th">Tipe</th><th class="th">Level</th>
      <th class="th">Skor</th><th class="th">NG lolos</th><th class="th">Status</th><th class="th"></th>
    </tr></thead>
    <tbody class="divide-y divide-slate-100">
      {% for r in rows %}
      {% set ok = r['score'] >= PASS and (r['esc'] or 0) == 0 %}
      <tr class="hover:bg-slate-50">
        <td class="td whitespace-nowrap text-slate-500">{{ r['created'] }}</td>
        <td class="td font-medium">{{ r['name'] }}<span class="block text-xs font-normal text-slate-400">{{ r['nik'] }}</span></td>
        <td class="td">{{ PHASE[r['phase']] }}</td>
        <td class="td">{{ levels[r['level']][0] if r['level'] in levels else '-' }}</td>
        <td class="td font-semibold">{{ r['score'] }}%</td>
        <td class="td {{ 'font-semibold text-rose-600' if (r['esc'] or 0) }}">{{ r['esc'] or 0 }}</td>
        <td class="td"><span class="pill {{ 'bg-emerald-100 text-emerald-700' if ok else 'bg-rose-100 text-rose-700' }}">{{ 'Lulus' if ok else 'Belum' }}</span></td>
        <td class="td text-right"><a href="/hasil/{{ r['id'] }}" class="font-medium text-brand-600 hover:underline">Detail</a></td>
      </tr>
      {% else %}
      <tr><td colspan="8" class="p-8 text-center text-slate-400">Belum ada data.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
'''

TEMPLATES["admin_soal.html"] = r'''{% extends "base.html" %}
{% block title %}Soal {{ title }}{% endblock %}
{% block width %}max-w-4xl{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold">Soal {{ title }} <span class="text-base font-normal text-slate-400">({{ qs|length }})</span></h1>

<form method="post" enctype="multipart/form-data" class="card mt-6 space-y-5 p-5 sm:p-6">
  <h2 class="font-semibold">Tambah soal</h2>
  <div>
    <label class="label">Pertanyaan</label>
    <input name="text" required class="input" placeholder="Contoh: Apakah headrest ini OK?">
  </div>
  <div>
    <label class="label">Foto (opsional)</label>
    <input type="file" name="image" accept="image/*" class="block w-full text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:font-semibold file:text-brand-700 hover:file:bg-brand-100">
  </div>

  <div>
    <label class="label">Opsi jawaban (minimal 2, pilih 1 yang benar)</label>
    <div class="grid gap-3 sm:grid-cols-2">
      {% for n in range(4) %}
      <div class="rounded-xl border border-slate-200 p-3">
        <div class="flex items-center gap-2">
          <input type="radio" name="correct" value="{{ n }}" {{ 'checked' if n == 0 }} class="h-4 w-4 accent-emerald-600" title="Jawaban benar">
          <input name="label{{ n }}" class="input" placeholder="{{ ['OK','NG - Jahitan','NG - Kotor','NG - Sobek'][n] }}">
        </div>
        <textarea name="kids{{ n }}" rows="3" class="input mt-2" placeholder="Jawaban lanjutan (opsional), satu per baris. Awali * untuk yang benar"></textarea>
      </div>
      {% endfor %}
    </div>
    <p class="mt-2 text-xs text-slate-400">Label "OK" dianggap OK, berawalan "NG" dianggap NG (dipakai untuk menghitung NG lolos).</p>
  </div>

  <div>
    <label class="label">Penjelasan (muncul di pembahasan)</label>
    <textarea name="explain" rows="2" class="input"></textarea>
  </div>
  <button class="btn btn-primary">Simpan soal</button>
</form>

{% if src %}
<details class="card mt-4 p-4">
  <summary class="cursor-pointer text-sm font-semibold">Salin soal dari {{ other_title }}</summary>
  <form method="post" action="/admin/soal/{{ phase }}/salin" class="mt-3 space-y-2">
    {% for s in src %}
    <label class="flex items-start gap-3 rounded-lg p-2 text-sm hover:bg-slate-50">
      <input type="checkbox" name="ids" value="{{ s.id }}" class="mt-1 accent-indigo-600"> <span>{{ s.text }}</span>
    </label>
    {% endfor %}
    <button class="btn btn-ghost">Salin yang dipilih</button>
  </form>
</details>
{% endif %}

<div class="mt-6 space-y-3">
  {% for x in qs %}
  <div class="card p-4">
    <div class="flex flex-col gap-4 sm:flex-row">
      {% if x.image %}<img src="/file/{{ x.image }}" class="h-28 w-full rounded-xl bg-slate-100 object-contain sm:w-40" alt="">{% endif %}
      <div class="min-w-0 flex-1">
        <p class="font-medium"><span class="text-slate-400">#{{ loop.index }}</span> {{ x.text }}</p>
        <div class="mt-2 flex flex-wrap gap-1.5">
          {% for o in x.opts %}
            {% set right = loop.index0 == x.ans_main %}
            <span class="pill {{ 'bg-emerald-100 text-emerald-700' if right else 'bg-slate-100 text-slate-600' }}">
              {{ o.label }}{% if o.kids %} ▸ {{ o.kids|join(' / ') }}{% endif %}
            </span>
          {% endfor %}
        </div>
        {% if x.explain %}<p class="mt-2 text-xs text-slate-500">{{ x.explain }}</p>{% endif %}
      </div>
    </div>
    <div class="mt-3 flex flex-wrap gap-2 border-t border-slate-100 pt-3">
      <form method="post" action="/admin/soal/satu/{{ x.id }}/{{ other }}"><button class="btn btn-ghost !py-1.5 !text-xs">Salin ke {{ other_title }}</button></form>
      <form method="post" action="/admin/soal/satu/{{ x.id }}/{{ phase }}"><button class="btn btn-ghost !py-1.5 !text-xs">Duplikat</button></form>
      <form method="post" action="/admin/soal/hapus/{{ x.id }}" onsubmit="return confirm('Hapus soal ini?')"><button class="btn btn-danger !py-1.5 !text-xs">Hapus</button></form>
    </div>
  </div>
  {% else %}
  <div class="card p-8 text-center text-slate-400">Belum ada soal.</div>
  {% endfor %}
</div>
{% endblock %}
'''

TEMPLATES["admin_materi.html"] = r'''{% extends "base.html" %}
{% block title %}Materi{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold">Materi</h1>
<form method="post" enctype="multipart/form-data" class="card mt-6 space-y-4 p-5">
  <div><label class="label">Judul</label><input name="title" class="input" placeholder="Contoh: Panduan Cacat Headrest"></div>
  <div><label class="label">File (PDF, gambar, video, dll.)</label>
    <input type="file" name="file" required class="block w-full text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:font-semibold file:text-brand-700 hover:file:bg-brand-100"></div>
  <button class="btn btn-primary">Unggah</button>
</form>

<div class="mt-6 space-y-2">
  {% for m in mats %}
  <div class="card flex items-center gap-3 p-4">
    <div class="min-w-0 flex-1"><p class="truncate font-medium">{{ m.title }}</p><p class="text-xs text-slate-400">{{ m.created }}</p></div>
    <a href="/file/{{ m.filename }}" target="_blank" class="btn btn-ghost !py-1.5">Buka</a>
    <form method="post" action="/admin/materi/hapus/{{ m.id }}" onsubmit="return confirm('Hapus materi ini?')"><button class="btn btn-danger !py-1.5">Hapus</button></form>
  </div>
  {% else %}
  <div class="card p-8 text-center text-slate-400">Belum ada materi.</div>
  {% endfor %}
</div>
{% endblock %}
'''

TEMPLATES["admin_people.html"] = r'''{% extends "base.html" %}
{% block title %}Peserta{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold">Peserta <span class="text-base font-normal text-slate-400">({{ people|length }})</span></h1>

<div class="mt-6 grid gap-4 md:grid-cols-2">
  <form method="post" class="card space-y-4 p-5">
    <h2 class="font-semibold">Tambah satu</h2>
    <div><label class="label">Nama</label><input name="name" class="input"></div>
    <div><label class="label">NIK</label><input name="nik" class="input"></div>
    <button class="btn btn-primary">Tambah</button>
  </form>
  <form method="post" class="card space-y-4 p-5">
    <h2 class="font-semibold">Tambah banyak (paste dari Excel)</h2>
    <textarea name="bulk" rows="5" class="input font-mono" placeholder="Budi Santoso;12345&#10;Siti Aminah;12346"></textarea>
    <p class="text-xs text-slate-400">Satu baris satu orang: Nama dan NIK dipisah tab, titik koma, atau koma.</p>
    <button class="btn btn-primary">Tambah semua</button>
  </form>
</div>

<div class="card mt-6 overflow-hidden">
  <table class="min-w-full divide-y divide-slate-100">
    <thead class="bg-slate-50"><tr><th class="th">Nama</th><th class="th">NIK</th><th class="th"></th></tr></thead>
    <tbody class="divide-y divide-slate-100">
      {% for p in people %}
      <tr class="hover:bg-slate-50">
        <td class="td font-medium">{{ p.name }}</td><td class="td text-slate-500">{{ p.nik }}</td>
        <td class="td text-right"><form method="post" action="/admin/peserta/hapus/{{ p.id }}" onsubmit="return confirm('Hapus peserta ini?')"><button class="text-sm font-medium text-rose-600 hover:underline">Hapus</button></form></td>
      </tr>
      {% else %}
      <tr><td colspan="3" class="p-8 text-center text-slate-400">Belum ada peserta.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
'''

TEMPLATES["admin_activity.html"] = r'''{% extends "base.html" %}
{% block title %}Aktivitas{% endblock %}
{% block width %}max-w-6xl{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold">Log Aktivitas</h1>
<form method="get" class="mt-4 flex flex-wrap gap-2">
  <input name="q" value="{{ s }}" placeholder="Cari nama / NIK…" class="input !w-64">
  <select name="kind" class="input !w-auto">
    <option value="">Semua aktivitas</option>
    {% for key, label in EVENT.items() %}<option value="{{ key }}" {{ 'selected' if k == key }}>{{ label }}</option>{% endfor %}
  </select>
  <button class="btn btn-ghost">Filter</button>
</form>

<div class="card mt-4 overflow-x-auto">
  <table class="min-w-full divide-y divide-slate-100">
    <thead class="bg-slate-50"><tr><th class="th">Waktu</th><th class="th">Nama</th><th class="th">NIK</th><th class="th">Aktivitas</th><th class="th">Detail</th><th class="th">Durasi</th></tr></thead>
    <tbody class="divide-y divide-slate-100">
      {% for r in rows %}
      <tr class="hover:bg-slate-50">
        <td class="td whitespace-nowrap text-slate-500">{{ r['at'] }}</td>
        <td class="td font-medium">{{ r['name'] }}</td>
        <td class="td text-slate-500">{{ r['nik'] }}</td>
        <td class="td"><span class="pill bg-brand-50 text-brand-700">{{ EVENT.get(r['kind'], r['kind']) }}</span></td>
        <td class="td text-slate-600">{{ r['detail'] }}</td>
        <td class="td whitespace-nowrap">{{ r['secs']|dur }}</td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="p-8 text-center text-slate-400">Belum ada aktivitas.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
<p class="mt-2 text-xs text-slate-400">Menampilkan maksimal 500 aktivitas terbaru.</p>
{% endblock %}
'''

app.jinja_loader = DictLoader(TEMPLATES)


if __name__ == "__main__":
    from waitress import serve
    print(f"Aplikasi jalan. PC ini: http://localhost:{PORT}  |  Jaringan kantor: http://IP-PC-INI:{PORT}")
    serve(app, host="0.0.0.0", port=PORT)