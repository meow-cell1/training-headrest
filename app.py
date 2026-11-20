import io, json, os, random, re, shutil, sqlite3, uuid
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, session, flash,
                   send_file, send_from_directory, abort, jsonify)
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
    return {"local_css": os.path.exists(os.path.join(BASE, "static", "app.css")), "levels": LEVELS, "EVENT": EVENT}


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
        m, k = int(a.get("m", -1)), int(a.get("k", -1))
        t = min(max(float(a.get("t", limit)), 0), limit)
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


if __name__ == "__main__":
    from waitress import serve
    print(f"Aplikasi jalan. PC ini: http://localhost:{PORT}  |  Jaringan kantor: http://IP-PC-INI:{PORT}")
    serve(app, host="0.0.0.0", port=PORT)
