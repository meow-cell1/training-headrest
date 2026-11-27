import hmac, io, json, os, random, re, shutil, sqlite3, time, uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse, urlencode
from flask import (Flask, render_template, request, redirect, url_for, session, flash,
                   send_file, send_from_directory, abort, jsonify)
from jinja2 import DictLoader
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
UP = os.path.join(BASE, "uploads")
DB = os.path.join(BASE, "training.db")
os.makedirs(UP, exist_ok=True)

# ====== PENGATURAN (ubah di sini) ======
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")   # GANTI! (atau set lewat environment variable)
PASS_SCORE = 90                                                   # skor minimal lulus (%)
PORT = int(os.environ.get("PORT", 5000))
TZ_OFFSET = 7                                                     # zona waktu: 7 = WIB, 8 = WITA, 9 = WIT
REQUIRE_SCHEDULE = True                                           # True = peserta hanya bisa masuk saat jadwalnya aktif
CHECK_ORIGIN = True                                               # tolak POST dari website lain (keamanan)
TRASH_DAYS = 31                                                   # lama riwayat terhapus disimpan di Sampah (hari)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"       # set 1 jika diakses lewat HTTPS
LEVELS = {"mudah": ("Mudah", 10), "normal": ("Normal", 6), "sulit": ("Sulit", 4)}   # detik per soal
# =======================================


def load_secret():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    fp = os.path.join(BASE, "secret.key")           # dibuat otomatis, jangan dibagikan
    if not os.path.exists(fp):
        with open(fp, "w") as fh:
            fh.write(uuid.uuid4().hex + uuid.uuid4().hex)
    with open(fp) as fh:
        return fh.read().strip()


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)   # agar benar di belakang tunnel / reverse proxy
app.secret_key = load_secret()
app.permanent_session_lifetime = timedelta(hours=12)
app.config.update(MAX_CONTENT_LENGTH=100 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=COOKIE_SECURE)
PHASE = {"pre": "Pre Test", "post": "Post Test", "remed": "Post Test Remedial"}
DTF, DTMIN = "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"


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


def add_cols(table, cols):                       # migrasi otomatis DB lama
    have = {r["name"] for r in q(f"PRAGMA table_info({table})")}
    for c, t in cols:
        if c not in have:
            q(f"ALTER TABLE {table} ADD COLUMN {c} {t}")


q("""CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY AUTOINCREMENT, phase TEXT, text TEXT,
     image TEXT, opts TEXT, ans_main INTEGER, ans_kid INTEGER, explain TEXT)""")
q("""CREATE TABLE IF NOT EXISTS materials(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
     filename TEXT, created TEXT)""")
q("""CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phase TEXT,
     score INTEGER, correct INTEGER, total INTEGER, detail TEXT, created TEXT)""")
q("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, nik TEXT NOT NULL)")
q("""CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, nik TEXT, kind TEXT,
     detail TEXT, secs REAL, at TEXT)""")
q("CREATE TABLE IF NOT EXISTS divisions(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, active INTEGER DEFAULT 0)")
q("""CREATE TABLE IF NOT EXISTS schedules(id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER NOT NULL, div_id INTEGER,
     t_start TEXT, t_end TEXT)""")
add_cols("attempts", [("level", "TEXT"), ("esc", "INTEGER DEFAULT 0"), ("rej", "INTEGER DEFAULT 0"),
                      ("kw", "INTEGER DEFAULT 0"), ("avgt", "REAL DEFAULT 0"), ("nik", "TEXT"),
                      ("dur", "REAL DEFAULT 0"), ("div_id", "INTEGER")])
add_cols("questions", [("div_id", "INTEGER"), ("explain_img", "TEXT")])      # NULL = berlaku untuk semua divisi
add_cols("materials", [("div_id", "INTEGER")])
add_cols("people", [("div_id", "INTEGER")])          # divisi asal peserta (untuk pengelompokan di admin)
q("CREATE TABLE IF NOT EXISTS resets(id INTEGER PRIMARY KEY AUTOINCREMENT, nik TEXT, div_id INTEGER, at TEXT)")
q("""CREATE TABLE IF NOT EXISTS trash(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, data TEXT, deleted_at TEXT,
     expires TEXT, batch TEXT, reason TEXT)""")
add_cols("schedules", [("created", "TEXT")])          # kapan jadwal diinput admin (dipakai untuk putaran baru)
add_cols("events", [("div_id", "INTEGER")])
EVENT = {"pre": "Pre Test", "buka": "Membuka materi", "baca": "Lama membaca materi", "post": "Post Test",
         "remed": "Post Remedial"}


@app.context_processor
def inject():
    logo = None                                   # gambar logo: static/logo.png (atau .jpg .jpeg .webp .svg .gif)
    for ext in ("png", "jpg", "jpeg", "webp", "svg", "gif"):
        fp = os.path.join(BASE, "static", f"logo.{ext}")
        if os.path.exists(fp):
            logo = url_for("static", filename=f"logo.{ext}", v=int(os.path.getmtime(fp)))
            break
    return {"local_tw": os.path.exists(os.path.join(BASE, "static", "tailwind.js")),
            "local_chart": os.path.exists(os.path.join(BASE, "static", "chart.js")),
            "levels": LEVELS, "EVENT": EVENT, "PHASE": PHASE, "logo_url": logo,
            "divmap": {d["id"]: d["name"] for d in q("SELECT id,name FROM divisions")}}


def now_dt():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TZ_OFFSET)


def now():
    return now_dt().strftime(DTF)


def now_min():
    return now_dt().strftime(DTMIN)


def log(kind, detail="", secs=None):
    q("INSERT INTO events(name,nik,kind,detail,secs,at,div_id) VALUES(?,?,?,?,?,?,?)",
      (session.get("name"), session.get("nik"), kind, detail, secs, now(), session.get("div")))


_purge = {"t": 0}


def purge_trash():
    # Hapus permanen isi Sampah yang sudah lewat TRASH_DAYS hari
    q("DELETE FROM trash WHERE expires < ?", (now(),))
    _purge["t"] = time.time()


@app.before_request
def auto_purge():
    if time.time() - _purge["t"] > 1800 and request.path.startswith("/admin"):
        purge_trash()


def to_trash(kind, rows, reason):
    # Simpan baris yang dihapus admin ke Sampah; hilang otomatis setelah TRASH_DAYS hari
    if not rows:
        return 0
    at = now_dt()
    exp = (at + timedelta(days=TRASH_DAYS)).strftime(DTF)
    batch = uuid.uuid4().hex[:10]
    con = sqlite3.connect(DB)
    try:
        con.executemany("INSERT INTO trash(kind,data,deleted_at,expires,batch,reason) VALUES(?,?,?,?,?,?)",
                        [(kind, json.dumps(dict(r), ensure_ascii=False), at.strftime(DTF), exp, batch, reason) for r in rows])
        con.commit()
    finally:
        con.close()
    return len(rows)


def restore_item(t):
    d = json.loads(t["data"])
    table = "attempts" if t["kind"] == "attempt" else "events"
    cols = {r["name"] for r in q(f"PRAGMA table_info({table})")}
    d = {k: v for k, v in d.items() if k in cols}
    if d:
        q(f"INSERT OR IGNORE INTO {table}({','.join(d)}) VALUES({','.join('?' * len(d))})", list(d.values()))
    q("DELETE FROM trash WHERE id=?", (t["id"],))


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


IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
IMG_MSG = "File gambar harus berformat PNG, JPG, GIF, atau WEBP."


def bad_image(f):
    return bool(f and f.filename and os.path.splitext(f.filename)[1].lower() not in IMG_EXT)


def save_image(f):
    return None if (not f or not f.filename or bad_image(f)) else save_file(f)


def clone_file(name):
    if not name:
        return None
    new = uuid.uuid4().hex[:8] + "_" + name.split("_", 1)[-1]
    try:
        shutil.copy(os.path.join(UP, name), os.path.join(UP, new))
        return new
    except OSError:
        return None


def parse_opts(f):
    # Membaca 4 slot opsi dari form. Hasil: ((opts, ans_main, ans_kid), None) atau (None, pesan_error)
    opts, slot, star = [], {}, {}
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
    try:
        c = int(f.get("correct", 0))
    except ValueError:
        c = -1
    if len(opts) < 2 or c not in slot:
        return None, "Isi minimal 2 opsi dan pilih salah satu sebagai jawaban benar."
    if opts[slot[c]]["kids"] and star[c] is None:
        return None, "Opsi benar punya anak jawaban: awali satu baris dengan * untuk menandai yang benar."
    return (opts, slot[c], star[c]), None


def load_qs(phase, did=None):
    """did=None -> semua soal (admin). did=ID -> soal untuk divisi itu + soal 'semua divisi'."""
    sql, a = "SELECT * FROM questions WHERE phase=?", [phase]
    if did is not None:
        sql += " AND (div_id IS NULL OR div_id=?)"
        a.append(did)
    out = []
    for r in q(sql + " ORDER BY id", a):
        d = dict(r)
        d["opts"] = json.loads(d["opts"])
        out.append(d)
    return out


def bank(phase, did):
    """Remedial memakai bank soal Remedial divisi itu; kalau kosong, memakai soal Post Test."""
    if phase == "remed":
        return load_qs("remed", did) or load_qs("post", did)
    return load_qs(phase, did)


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


def get_div(did):
    return q("SELECT * FROM divisions WHERE id=?", (did,), True) if did else None


# ---------------- JADWAL ----------------
def person_windows(pid):
    return q("""SELECT s.*, d.name dname FROM schedules s LEFT JOIN divisions d ON d.id=s.div_id
                WHERE s.pid=? ORDER BY s.t_start""", (pid,))


def sched_ok(pid, did=None):
    if not REQUIRE_SCHEDULE:
        return True
    n = now_min()
    return any(w["t_start"] <= n <= w["t_end"] and (did is None or w["div_id"] is None or w["div_id"] == did)
               for w in person_windows(pid))


def sched_until(pid, did):
    n = now_min()
    ends = [w["t_end"] for w in person_windows(pid)
            if w["t_start"] <= n <= w["t_end"] and (w["div_id"] is None or w["div_id"] == did)]
    return min(ends) if ends else None


def win_status(w):
    n = now_min()
    return "aktif" if w["t_start"] <= n <= w["t_end"] else "akan" if w["t_start"] > n else "lewat"


def sched_text(w):
    return f"{w['t_start']} s/d {w['t_end']} ({w['dname'] or 'Semua divisi'})"


# ---------------- SIKLUS PRE -> POST -> REMEDIAL ----------------
def passed(r):
    return r["score"] >= PASS_SCORE and (r["esc"] or 0) == 0


def need_remed(pre, post, rem):
    """Remedial wajib bila Post belum lulus, atau Post lebih rendah dari Pre. Setelah remedial: wajib lagi bila belum lulus."""
    if rem:
        return not passed(rem[-1])
    return (not passed(post)) or (pre is not None and post["score"] < pre["score"])


def reset_at(nik, did):
    if not nik:
        return ""
    r = q("SELECT MAX(at) m FROM resets WHERE nik=? AND div_id IS ?", (nik, did), True)
    return r["m"] if r and r["m"] else ""


def get_cycle(nik, name, did, upto=None):
    """Siklus terakhir seseorang di satu divisi: (pre, post, [remedial...])."""
    sql = "SELECT * FROM attempts WHERE " + ("nik=?" if nik else "name=?") + " AND div_id IS ?"
    a = [nik or name, did]
    if upto:
        sql += " AND id<=?"
        a.append(upto)
    ra = reset_at(nik, did)                       # admin bisa mereset siklus: tes sebelum reset tidak dihitung
    if ra:
        sql += " AND created>?"
        a.append(ra)
    pre = post = None
    rem = []
    for r in q(sql + " ORDER BY id", a):
        if r["phase"] == "pre":
            pre, post, rem = r, None, []
        elif r["phase"] == "post":
            post, rem = r, []
        elif post is not None:
            rem.append(r)
    return pre, post, rem


def build_summary(pre, post, rem):
    """Kesimpulan: Pre Test, Post (sebelum remedial), dan Remedial (kalau ada)."""
    steps = [{"label": "Pre Test", "score": pre["score"], "kind": "pre", "delta": None},
             {"label": "Post Test" if not rem else "Post (sebelum remedial)", "score": post["score"],
              "kind": "post", "delta": post["score"] - pre["score"]}]
    prev = post["score"]
    for i, r in enumerate(rem, 1):
        steps.append({"label": "Post Remedial" if len(rem) == 1 else f"Remedial {i}", "score": r["score"],
                      "kind": "remed", "delta": r["score"] - prev})
        prev = r["score"]
    last = rem[-1] if rem else post
    a, b, c = pre["score"], post["score"], last["score"]
    d = c - a
    need = need_remed(pre, post, rem)
    ok = passed(last)
    verdict = "naik" if d > 0 else "turun" if d < 0 else "tetap"
    if not rem:
        if d > 0:
            title, text = "Ada peningkatan! 🎉", (f"Nilai naik {d} poin, dari {a}% (Pre Test) menjadi {b}% (Post Test). "
                                                  "Training ini berhasil meningkatkan kemampuan deteksi OK / NG.")
        elif d < 0:
            title, text = "Nilai menurun", (f"Nilai turun {-d} poin, dari {a}% (Pre Test) menjadi {b}% (Post Test). "
                                            "Disarankan mengulang materi dan mengikuti remedial.")
        elif b >= 100:
            title, text = "Nilai tetap sempurna", "Nilai Pre Test dan Post Test sama-sama 100%. Kemampuan deteksi sudah sangat baik sejak awal."
        else:
            title, text = "Belum ada peningkatan", f"Nilai Pre Test dan Post Test sama ({b}%). Perlu pendampingan atau pengulangan materi."
        if need:
            text += " Kamu perlu mengikuti Post Test Remedial: buka materi lagi, lalu kerjakan ulang."
    else:
        title = "Remedial berhasil! 🎉" if ok else "Remedial belum memenuhi syarat"
        chg = (f"naik {c - b} poin" if c > b else f"turun {b - c} poin" if c < b else "sama")
        text = (f"Pre Test {a}%, Post Test (sebelum remedial) {b}%, Post Remedial {c}%. "
                f"Dibanding Post Test sebelum remedial, nilai {chg}. Dibanding Pre Test, selisihnya "
                f"{'+' if d > 0 else ''}{d} poin.")
        if need:
            text += " Masih perlu remedial lagi: pelajari materi dan kerjakan ulang."
    notes = []
    if rem:
        notes.append(f"Jumlah remedial yang sudah dikerjakan: {len(rem)} kali.")
    e1, e2 = pre["esc"] or 0, last["esc"] or 0
    if e1 == 0 and e2 == 0:
        notes.append("Tidak ada NG yang lolos di Pre Test maupun tes terakhir.")
    elif e2 < e1:
        notes.append(f"NG yang lolos berkurang dari {e1} menjadi {e2}. Deteksi cacat makin teliti.")
    elif e2 > e1:
        notes.append(f"NG yang lolos bertambah dari {e1} menjadi {e2}. Perlu latihan lagi untuk mendeteksi NG.")
    else:
        notes.append(f"NG yang lolos tetap {e2}. Masih perlu latihan mendeteksi NG.")
    t1, t2 = pre["avgt"] or 0, last["avgt"] or 0
    if pre["level"] == last["level"] and t1 and t2:
        dt = round(t2 - t1, 1)
        if dt <= -0.2:
            notes.append(f"Menjawab lebih cepat: rata-rata {t1} dtk menjadi {t2} dtk per soal.")
        elif dt >= 0.2:
            notes.append(f"Menjawab lebih lambat: rata-rata {t1} dtk menjadi {t2} dtk per soal.")
        else:
            notes.append(f"Kecepatan menjawab relatif sama (sekitar {t2} dtk per soal).")
    notes.append("Status akhir: " + ("LULUS." if ok else
                 f"BELUM LULUS (syarat skor minimal {PASS_SCORE}% dan tidak ada NG yang lolos)."))
    return {"steps": steps, "final": c, "diff": d, "verdict": verdict, "title": title, "text": text,
            "notes": notes, "passed": ok, "need_remed": need}


def progress_rows(did=None, df="", dt="", s=""):
    # Pre, Post (sebelum remedial), Remedial terakhir tiap peserta per divisi. Bisa difilter rentang tanggal.
    dm = {d["id"]: d["name"] for d in q("SELECT id,name FROM divisions")}
    rs = {(r["nik"], r["div_id"]): r["m"] for r in q("SELECT nik, div_id, MAX(at) m FROM resets GROUP BY nik, div_id")}
    ppl = {}
    for r in q("SELECT name, nik, phase, score, div_id, created FROM attempts ORDER BY id"):
        ra = rs.get((r["nik"], r["div_id"]))
        if ra and r["created"] <= ra:
            continue
        d = ppl.setdefault((r["nik"] or r["name"], r["div_id"]),
                           {"name": r["name"], "nik": r["nik"] or "-", "rnik": r["nik"], "div": dm.get(r["div_id"], "-"),
                            "div_id": r["div_id"], "pre": None, "post": None, "remed": None, "nrem": 0,
                            "first": r["created"], "last": r["created"]})
        d["name"], d["last"] = r["name"], r["created"]
        if r["phase"] == "pre":
            d["pre"], d["post"], d["remed"], d["nrem"], d["first"] = r["score"], None, None, 0, r["created"]
        elif r["phase"] == "post":
            d["post"], d["remed"], d["nrem"] = r["score"], None, 0
        else:
            d["remed"] = r["score"]
            d["nrem"] += 1
    rows = [d for d in ppl.values()
            if (did is None or d["div_id"] == did)
            and (not df or d["last"] >= df + " 00:00:00") and (not dt or d["first"] <= dt + " 23:59:59")
            and (not s or s.lower() in d["name"].lower())]
    for d in rows:
        d["final"] = d["remed"] if d["remed"] is not None else d["post"]
        if d["pre"] is not None and d["final"] is not None:
            d["diff"] = d["final"] - d["pre"]
            d["status"] = "naik" if d["diff"] > 0 else "turun" if d["diff"] < 0 else "tetap"
        else:
            d["diff"], d["status"] = None, "belum"
    rows.sort(key=lambda x: (x["diff"] is None, -(x["diff"] or 0), x["name"].lower()))
    both = [d for d in rows if d["diff"] is not None]

    def avg(v):
        return round(sum(v) / len(v)) if v else None
    cnt = {k: sum(1 for d in rows if d["status"] == k) for k in ("naik", "tetap", "turun", "belum")}
    cnt.update(total=len(both), remed=sum(1 for d in rows if d["remed"] is not None),
               avg_pre=avg([d["pre"] for d in both]), avg_post=avg([d["final"] for d in both]),
               avg_diff=avg([d["diff"] for d in both]))
    if both:
        p1 = cnt["naik"] / len(both) * 100
        p2 = p1 + cnt["tetap"] / len(both) * 100
        donut = f"conic-gradient(#10b981 0 {p1:.2f}%, #f59e0b {p1:.2f}% {p2:.2f}%, #f43f5e {p2:.2f}% 100%)"
    else:
        donut = "conic-gradient(#ede9fe 0 100%)"
    return rows, cnt, donut


# ---------------- KEAMANAN ----------------
FAILS = {}


@app.before_request
def csrf_origin():
    """Tolak POST yang berasal dari website lain."""
    if CHECK_ORIGIN and request.method == "POST":
        o = request.headers.get("Origin") or request.headers.get("Referer")
        if o:
            h = urlparse(o).netloc
            if h != request.host and h != request.headers.get("X-Forwarded-Host", request.host):
                abort(403)


@app.before_request
def auto_logout_admin():
    """Admin otomatis keluar begitu membuka halaman peserta."""
    p = request.path
    if session.get("admin") and (p == "/" or p.startswith(("/materi", "/tes/", "/selesai", "/divisi", "/ulang"))):
        session.pop("admin", None)


def admin_only(fn):
    @wraps(fn)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return fn(*a, **k)
    return w


# ---------------- ALUR PESERTA ----------------
def deny():
    """Cek login, divisi aktif, dan jadwal. Mengembalikan redirect bila tidak boleh, None bila boleh."""
    if not session.get("name"):
        return redirect(url_for("home"))
    did = session.get("div")
    if not did:
        return redirect(url_for("divisi"))
    d = get_div(did)
    if not d or not d["active"]:
        session.pop("div", None)
        flash("Divisi ini sedang tidak aktif.")
        return redirect(url_for("divisi"))
    if not sched_ok(session.get("pid"), did):
        session.pop("div", None)
        flash("Saat ini di luar jadwal training kamu untuk divisi tersebut.")
        return redirect(url_for("divisi"))
    return None


def mat_open_since(did, ref):
    return bool(q("SELECT 1 x FROM events WHERE nik=? AND div_id=? AND kind='buka' AND at>? LIMIT 1",
                  (session.get("nik"), did, ref), True))


def new_round(pid, did, done_at):
    # Putaran baru terbuka jika ada jadwal aktif (untuk divisi ini) yang MULAI atau DIINPUT admin setelah peserta selesai
    if not REQUIRE_SCHEDULE:
        return False
    n = now_min()
    return any(w["t_start"] <= n <= w["t_end"] and (w["div_id"] is None or w["div_id"] == did)
               and (w["t_start"] > done_at or (w["created"] or "") > done_at) for w in person_windows(pid))


def div_state(pid, nik, name, did):
    pre, post, rem = get_cycle(nik, name, did)
    if post and not need_remed(pre, post, rem):
        done_at = (rem[-1] if rem else post)["created"]
        return "baru" if new_round(pid, did, done_at) else "selesai"
    return "jalan" if (pre or post) else "belum"


def flow():
    """Status tahap peserta pada divisi yang dipilih (dibaca dari database)."""
    did, nik, name = session.get("div"), session.get("nik"), session.get("name")
    pre, post, rem = get_cycle(nik, name, did)
    t0 = session.get("t0", "")
    if post and not need_remed(pre, post, rem):                  # siklus sebelumnya sudah selesai
        done_at = (rem[-1] if rem else post)["created"]
        if new_round(session.get("pid"), did, done_at):          # jadwal baru sudah ada -> mulai putaran baru
            pre, post, rem, t0 = None, None, [], max(t0, done_at)
    c = {p: len(bank(p, did)) for p in PHASE}
    nm = q("SELECT COUNT(*) c FROM materials WHERE div_id IS NULL OR div_id=?", (did,), True)["c"]
    pre_ok = pre is not None or c["pre"] == 0
    mat_open = mat_open_since(did, pre["created"] if pre else t0)
    mat_ok = nm == 0 or mat_open
    need = bool(post) and need_remed(pre, post, rem)
    last = rem[-1] if rem else post
    mat2 = need and (nm == 0 or mat_open_since(did, last["created"]))
    done = bool(post) and not need
    return {"pre": pre, "post": post, "rem": rem, "c": c, "nm": nm, "pre_ok": pre_ok, "mat_open": mat_open,
            "mat_ok": mat_ok, "need": need, "mat2": mat2, "done": done,
            "cc": build_summary(pre, post, rem) if done and pre else None}


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        p = q("SELECT * FROM people WHERE id=?", (request.form.get("pid", 0),), True)
        if not p:
            flash("Pilih nama terlebih dahulu.")
        elif p["nik"].strip().lower() != request.form.get("nik", "").strip().lower():
            flash("NIK tidak cocok dengan nama yang dipilih.")
        elif not sched_ok(p["id"]):
            ws = person_windows(p["id"])
            up = [w for w in ws if w["t_start"] > now_min()]
            if up:
                flash(f"Belum waktunya training. Jadwal terdekatmu: {sched_text(up[0])}.")
            elif ws:
                flash("Jadwal training kamu sudah berakhir. Hubungi admin untuk jadwal baru.")
            else:
                flash("Kamu belum punya jadwal training. Hubungi admin.")
        else:
            session.permanent = True
            for k in ("pre", "mat", "post", "mat_t0", "div", "fresh", "t0"):
                session.pop(k, None)
            session.update(name=p["name"], nik=p["nik"], pid=p["id"])
            return redirect(url_for("divisi"))
        return redirect(url_for("home"))
    if not session.get("name"):
        return render_template("home.html", cards=[], people=[dict(r) for r in q(
            "SELECT p.id, p.name, COALESCE(d.name, 'Tanpa divisi') g FROM people p LEFT JOIN divisions d ON d.id=p.div_id "
            "ORDER BY g COLLATE NOCASE, p.name COLLATE NOCASE")])
    r = deny()
    if r:
        return r
    f, c, did = flow(), None, session["div"]
    c = f["c"]
    cards = [
        dict(step="Langkah 1", title="Pre Test", desc="Ukur kemampuan awal sebelum pelatihan.", href="/tes/pre",
             meta=f"{c['pre']} soal", done=bool(f["pre"]), on=c["pre"] > 0 and not f["pre"],
             note="Belum ada soal Pre Test. Admin perlu menambahkannya dulu." if c["pre"] == 0 else ""),
        dict(step="Langkah 2", title="Materi", desc="Pelajari materi pelatihan.", href="/materi",
             meta=f"{f['nm']} materi", done=f["mat_open"] and f["pre_ok"], on=f["pre_ok"], note="Selesaikan Pre Test dulu"),
        dict(step="Langkah 3", title="Post Test", desc="Ukur kemampuan setelah pelatihan.", href="/tes/post",
             meta=f"{c['post']} soal", done=bool(f["post"]),
             on=f["pre_ok"] and f["mat_ok"] and c["post"] > 0 and not f["post"],
             note="Belum ada soal Post Test." if c["post"] == 0 else "Buka materi dulu (Lihat / Unduh)"),
    ]
    alert = None
    if f["post"] and (f["need"] or f["rem"]):
        cards += [
            dict(step="Remedial", title="Materi Ulang", desc="Pelajari materi sekali lagi sebelum remedial.", href="/materi",
                 meta=f"{f['nm']} materi", done=f["mat2"], on=f["need"], note=""),
            dict(step="Remedial", title="Post Test Remedial", desc="Kerjakan ulang Post Test untuk memperbaiki nilai.",
                 href="/tes/remed", meta=f"{c['remed']} soal", done=bool(f["rem"]) and not f["need"],
                 on=f["need"] and f["mat2"] and c["remed"] > 0, note="Buka materi ulang dulu (Lihat / Unduh)"),
        ]
        if f["need"]:
            last = f["rem"][-1] if f["rem"] else f["post"]
            alert = (f"Nilai {'Post Remedial' if f['rem'] else 'Post Test'} kamu {last['score']}% "
                     f"{'belum memenuhi syarat lulus' if not passed(last) else 'lebih rendah dari Pre Test'}. "
                     "Silakan buka materi lagi, lalu kerjakan Post Test Remedial.")
    for x in cards:
        x["lock"] = not x["on"] and not x["done"]
    return render_template("home.html", cards=cards, all_done=f["done"], cc=f["cc"], alert=alert,
                           until=sched_until(session["pid"], did), dname=get_div(did)["name"])


@app.get("/divisi")
def divisi():
    if not session.get("name"):
        return redirect(url_for("home"))
    pid = session["pid"]
    items = []
    for d in q("SELECT * FROM divisions ORDER BY name COLLATE NOCASE"):
        if not d["active"]:
            why = "Belum diaktifkan admin"
        elif not sched_ok(pid, d["id"]):
            why = "Di luar jadwalmu"
        else:
            why = ""
        items.append(dict(id=d["id"], name=d["name"], open=not why, why=why,
                          state=div_state(pid, session.get("nik"), session.get("name"), d["id"])))
    ws = [dict(t_start=w["t_start"], t_end=w["t_end"], dname=w["dname"] or "Semua divisi", st=win_status(w))
          for w in person_windows(pid)]
    return render_template("divisi.html", items=items, ws=ws)


@app.get("/divisi/pilih/<int:did>")
def divisi_pick(did):
    if not session.get("name"):
        return redirect(url_for("home"))
    d = get_div(did)
    if not d or not d["active"]:
        flash("Divisi ini belum diaktifkan.")
    elif not sched_ok(session["pid"], did):
        flash("Divisi ini di luar jadwal training kamu.")
    else:
        for k in ("fresh", "mat_t0"):
            session.pop(k, None)
        session.update(div=did, t0=now())
        return redirect(url_for("home"))
    return redirect(url_for("divisi"))


@app.get("/divisi/ganti")
def divisi_change():
    session.pop("div", None)
    return redirect(url_for("divisi"))


@app.get("/selesai")
def finish():
    for k in ("name", "nik", "pid", "pre", "mat", "post", "mat_t0", "div", "fresh", "t0"):
        session.pop(k, None)
    return redirect(url_for("home"))


@app.get("/materi")
def materi():
    r = deny()
    if r:
        return r
    f, did = flow(), session["div"]
    if not f["pre_ok"]:
        flash("Selesaikan Pre Test terlebih dahulu.")
        return redirect(url_for("home"))
    c = f["c"]
    if f["done"]:
        nx = dict(done=True, hint="Training divisi ini sudah selesai.")
    elif f["need"]:
        has = c["remed"] > 0
        nx = dict(done=False, has=has, ok=f["mat2"] and has, href="/tes/remed", label="Lanjut ke Post Remedial →",
                  hint_ok="Siap! Waktu membaca materi dicatat sampai kamu mulai remedial.",
                  hint_lock="Buka (Lihat / Unduh) minimal satu materi agar tombol remedial aktif.",
                  no="Belum ada soal Remedial.")
    else:
        has = c["post"] > 0
        nx = dict(done=False, has=has, ok=f["pre_ok"] and f["mat_ok"] and has, href="/tes/post",
                  label="Lanjut ke Post Test →", hint_ok="Waktu membaca materi dicatat sampai kamu mulai tes.",
                  hint_lock="Buka (Lihat / Unduh) minimal satu materi agar tombol ini aktif.", no="Belum ada soal Post Test.")
    return render_template("materi.html", mats=q("SELECT * FROM materials WHERE div_id IS NULL OR div_id=? ORDER BY id DESC", (did,)),
                           nx=nx, remed_mode=f["need"], dname=get_div(did)["name"])


@app.get("/materi/buka/<int:mid>")
def materi_open(mid):
    r = deny()
    if r:
        return r
    if not flow()["pre_ok"]:
        return redirect(url_for("home"))
    m = q("SELECT * FROM materials WHERE id=?", (mid,), True)
    if not m or (m["div_id"] is not None and m["div_id"] != session["div"]):
        abort(404)
    session.setdefault("mat_t0", datetime.now().timestamp())     # awal membaca materi
    dl = bool(request.args.get("dl"))
    log("buka", f"{m['title']} ({'Unduh' if dl else 'Lihat'})")
    return send_from_directory(UP, m["filename"], as_attachment=dl, download_name=m["filename"].split("_", 1)[-1])


@app.get("/file/<name>")
def files(name):
    if not (session.get("name") or session.get("admin")):
        abort(403)
    return send_from_directory(UP, name)


def gate(phase):
    """Kembalikan alasan penolakan, atau None kalau boleh mengerjakan."""
    if not session.get("name") or not session.get("div"):
        return ""
    f = flow()
    if phase == "pre":
        return "Pre Test sudah selesai." if f["pre"] else None
    if phase == "post":
        if f["post"]:
            return "Post Test sudah selesai."
        if not (f["pre_ok"] and f["mat_ok"]):
            return "Selesaikan Pre Test dan buka materi terlebih dahulu."
        return None
    if not f["need"]:
        return "Remedial tidak diperlukan."
    if not f["mat2"]:
        return "Buka materi lagi terlebih dahulu sebelum remedial."
    return None


@app.get("/tes/<phase>")
def quiz(phase):
    if phase not in PHASE:
        abort(404)
    r = deny()
    if r:
        return r
    why = gate(phase)
    if why is not None:
        if why:
            flash(why)
        return redirect(url_for("home"))
    if phase in ("post", "remed") and session.get("mat_t0"):          # catat lama membaca materi
        log("baca", "Sejak membuka materi sampai mulai " + PHASE[phase],
            datetime.now().timestamp() - session.pop("mat_t0"))
    qs = bank(phase, session["div"])
    random.shuffle(qs)                      # urutan soal acak per peserta
    data = [{"id": x["id"], "text": x["text"], "image": x["image"], "opts": x["opts"]} for x in qs]
    return render_template("quiz.html", title=PHASE[phase], phase=phase, data=data, dname=get_div(session["div"])["name"])


@app.post("/tes/<phase>/kirim")
def quiz_submit(phase):
    if phase not in PHASE or gate(phase) is not None:
        abort(403)
    did = session["div"]
    body = request.get_json(force=True)
    lv = body.get("level") if body.get("level") in LEVELS else "normal"
    limit = LEVELS[lv][1]
    qmap = {x["id"]: x for x in bank(phase, did)}
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
        det.append({"text": x["text"], "image": x["image"], "explain": x["explain"], "explain_img": x.get("explain_img"), "good": right, "t": round(t, 1),
                    "yours": fmt(opts, m, k) if m >= 0 else "Tidak menjawab (waktu habis)",
                    "key": fmt(opts, x["ans_main"], x["ans_kid"])})
    total = len(det)
    score = round(ok / total * 100) if total else 0
    avg = round(sum(times) / len(times), 1) if times else 0
    dur = round(sum(times), 1)
    aid = q("""INSERT INTO attempts(name,phase,score,correct,total,detail,created,level,esc,rej,kw,avgt,nik,dur,div_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session["name"], phase, score, ok, total, json.dumps(det), now(), lv, esc, rej, kw, avg,
             session.get("nik"), dur, did))
    log(phase, f"Skor {score}% · level {LEVELS[lv][0]}", dur)
    session.pop("fresh", None)
    return jsonify(url=url_for("result", aid=aid))


@app.get("/hasil/<int:aid>")
def result(aid):
    a = q("SELECT * FROM attempts WHERE id=?", (aid,), True)
    if not a:
        abort(404)
    if not session.get("admin") and a["nik"] != session.get("nik"):
        abort(403)
    ok = passed(a)
    cc = None
    if a["phase"] in ("post", "remed"):
        pre, post, rem = get_cycle(a["nik"], a["name"], a["div_id"], upto=aid)
        cc = build_summary(pre, post, rem) if pre and post else None
    need = (cc["need_remed"] if cc else not ok) if a["phase"] != "pre" else False
    return render_template("result.html", a=a, det=json.loads(a["detail"]), label=PHASE[a["phase"]], passed=ok,
                           PASS=PASS_SCORE, review=a["phase"] != "pre" or session.get("admin"), cc=cc, need=need)


# ---------------- ADMIN ----------------
@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr
        n, t = FAILS.get(ip, (0, 0))
        if n >= 5 and time.time() - t < 300:
            flash("Terlalu banyak percobaan salah. Coba lagi 5 menit lagi.")
        elif hmac.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            FAILS.pop(ip, None)
            session["admin"] = True
            return redirect(url_for("dashboard"))
        else:
            FAILS[ip] = (n + 1 if time.time() - t < 300 else 1, time.time())
            flash("Password salah.")
    return render_template("login.html")


@app.get("/admin/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("home"))


def valid_date(v):
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return v
    except ValueError:
        return ""


def get_filters(src=None):
    a = src if src is not None else request.args
    return {"ph": a.get("phase", ""), "s": a.get("q", "").strip(), "dv": a.get("div", ""),
            "df": valid_date(a.get("from", "")), "dt": valid_date(a.get("to", ""))}


def where(f, extra=True):
    sql, a = "", []
    if f["dv"].isdigit():
        sql += " AND div_id=?"; a.append(int(f["dv"]))
    if f["df"]:
        sql += " AND created>=?"; a.append(f["df"] + " 00:00:00")
    if f["dt"]:
        sql += " AND created<=?"; a.append(f["dt"] + " 23:59:59")
    if extra:
        if f["ph"] in PHASE:
            sql += " AND phase=?"; a.append(f["ph"])
        if f["s"]:
            sql += " AND name LIKE ?"; a.append("%" + f["s"] + "%")
    return sql, a


def history(f):
    w, a = where(f)
    return q("SELECT * FROM attempts WHERE 1=1" + w + " ORDER BY id DESC", a)


SHORT = {"pre": "Pre", "post": "Post", "remed": "Remedial"}


def timeline_data(f):
    # Semua tes tiap peserta dalam rentang waktu, urut kronologis (untuk grafik tren)
    w, a = where(f, False)
    if f["s"]:
        w += " AND name LIKE ?"
        a.append("%" + f["s"] + "%")
    dm = {d["id"]: d["name"] for d in q("SELECT id,name FROM divisions")}
    ppl = {}
    for r in q("SELECT name, nik, phase, score, div_id, created FROM attempts WHERE 1=1" + w + " ORDER BY id", a):
        d = ppl.setdefault((r["nik"] or r["name"], r["div_id"]),
                           {"name": r["name"], "div": dm.get(r["div_id"], "-"), "pts": []})
        c = r["created"]
        d["pts"].append({"l": f"{SHORT[r['phase']]} {c[8:10]}/{c[5:7]}", "k": SHORT[r["phase"]], "p": r["phase"],
                         "s": r["score"], "d": c[:16]})
    return sorted(ppl.values(), key=lambda d: d["name"].lower())


PAGE = 25


def back_to():
    n = request.form.get("next", "")
    return redirect(n if n.startswith("/admin") and not n.startswith("//") else url_for("dashboard"))


@app.get("/admin")
@admin_only
def dashboard():
    f = get_filters()
    rows = history(f)
    did = int(f["dv"]) if f["dv"].isdigit() else None
    w, a = where(f, False)
    st = {r["phase"]: r for r in q(f"SELECT phase, COUNT(*) c, ROUND(AVG(score)) m FROM attempts WHERE 1=1{w} GROUP BY phase", a)}
    people = q(f"SELECT COUNT(DISTINCT name) c FROM attempts WHERE 1=1{w}", a, True)["c"]
    total = q(f"SELECT COUNT(*) c FROM attempts WHERE 1=1{w}", a, True)["c"]
    prog, cnt, donut = progress_rows(did, f["df"], f["dt"], f["s"])
    chart = {"labels": [d["name"] + ("" if did else " · " + d["div"]) for d in prog],
             "pre": [d["pre"] for d in prog], "post": [d["post"] for d in prog], "remed": [d["remed"] for d in prog],
             "diff": [d["diff"] for d in prog], "status": [d["status"] for d in prog]}
    base = {k: v for k, v in (("div", f["dv"]), ("phase", f["ph"]), ("q", f["s"])) if v}
    t = now_dt().date()

    def day(n):
        return (t - timedelta(days=n)).isoformat()
    presets = []
    for lab, x, y in [("Hari ini", day(0), day(0)), ("7 hari terakhir", day(6), day(0)), ("30 hari terakhir", day(29), day(0)),
                      ("Bulan ini", t.replace(day=1).isoformat(), day(0)), ("Semua waktu", "", "")]:
        p = dict(base)
        if x:
            p["from"] = x
        if y:
            p["to"] = y
        presets.append({"label": lab, "href": "/admin?" + urlencode(p), "on": (f["df"], f["dt"]) == (x, y)})
    pq = {k: v for k, v in (("div", f["dv"]), ("phase", f["ph"]), ("q", f["s"]), ("from", f["df"]), ("to", f["dt"])) if v}
    # halaman tabel riwayat
    n_rows = len(rows)
    pages = max(1, -(-n_rows // PAGE))
    pg = request.args.get("page", "1")
    pg = min(int(pg) if pg.isdigit() and int(pg) > 0 else 1, pages)

    def plink(n):
        return "/admin?" + urlencode({**pq, "page": n}) + "#riwayat"
    pager = {"page": pg, "pages": pages, "total": n_rows, "first": (pg - 1) * PAGE + 1 if n_rows else 0,
             "last": min(pg * PAGE, n_rows), "prev": plink(pg - 1) if pg > 1 else None,
             "next": plink(pg + 1) if pg < pages else None,
             "nums": [(n, plink(n)) for n in range(max(1, pg - 2), min(pages, pg + 2) + 1)]}
    return render_template("admin_dash.html", rows=rows[(pg - 1) * PAGE: pg * PAGE], pager=pager, f=f, st=st, people=people,
                           total=total, PASS=PASS_SCORE, prog=prog, cnt=cnt, donut=donut, chart=chart,
                           tl=timeline_data(f), presets=presets, qs=urlencode(pq), filtered=bool(pq),
                           all_count=q("SELECT COUNT(*) c FROM attempts", (), True)["c"],
                           trash_count=q("SELECT COUNT(*) c FROM trash", (), True)["c"],
                           nxt=request.full_path.rstrip("?") + "#riwayat",
                           divs=q("SELECT * FROM divisions ORDER BY name COLLATE NOCASE"))


@app.post("/admin/riwayat/hapus/<int:aid>")
@admin_only
def attempt_del(aid):
    r = q("SELECT * FROM attempts WHERE id=?", (aid,), True)
    if r:
        to_trash("attempt", [r], "Hapus satu tes")
        q("DELETE FROM attempts WHERE id=?", (aid,))
        flash(f"Riwayat tes dipindahkan ke Sampah (tersimpan {TRASH_DAYS} hari, bisa dipulihkan).")
    return back_to()


@app.post("/admin/riwayat/hapus-semua")
@admin_only
def attempts_del_all():
    if request.form.get("mode") == "all":
        if request.form.get("ok") != "HAPUS":
            flash("Dibatalkan: konfirmasi tidak sesuai.")
            return back_to()
        rows, reason, cond, a = q("SELECT * FROM attempts"), "Hapus semua riwayat", "", []
    else:
        cond, a = where(get_filters(request.form))
        rows, reason = q("SELECT * FROM attempts WHERE 1=1" + cond, a), "Hapus hasil filter"
    n = to_trash("attempt", rows, reason)
    q("DELETE FROM attempts WHERE 1=1" + cond, a)
    flash(f"{n} riwayat tes dipindahkan ke Sampah (tersimpan {TRASH_DAYS} hari, bisa dipulihkan).")
    return back_to()


@app.get("/admin/backup.db")
@admin_only
def backup():
    tmp = os.path.join(BASE, "backup_tmp.db")
    src, dst = sqlite3.connect(DB), sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with open(tmp, "rb") as fh:
        data = fh.read()
    try:
        os.remove(tmp)
    except OSError:
        pass
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f"training-{now_dt():%Y%m%d-%H%M}.db",
                     mimetype="application/octet-stream")


@app.post("/admin/reset")
@admin_only
def admin_reset():
    nik, did = request.form.get("nik", "").strip(), request.form.get("div_id", "")
    if nik:
        q("INSERT INTO resets(nik,div_id,at) VALUES(?,?,?)", (nik, int(did) if did.isdigit() else None, now()))
        flash("Siklus peserta direset. Peserta bisa mengulang dari Pre Test, riwayat lama tetap tersimpan di tabel riwayat.")
    return redirect(url_for("dashboard"))


@app.get("/admin/riwayat.xlsx")
@admin_only
def export():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    f = get_filters()
    rows = history(f)
    dm = {d["id"]: d["name"] for d in q("SELECT id,name FROM divisions")}
    wb = Workbook()
    ws, ws2 = wb.active, wb.create_sheet("Pre-Post-Remedial")
    ws3, ws4 = wb.create_sheet("Detail Jawaban"), wb.create_sheet("Aktivitas")
    ws.title = "Riwayat"
    ws.append(["Tanggal", "Nama", "NIK", "Divisi", "Tipe", "Level", "Benar", "Total", "Skor (%)", "NG lolos",
               "OK dianggap NG", "Jenis NG salah", "Rata2 waktu/soal (dtk)", "Durasi pengerjaan (dtk)", "Status"])
    ws2.append(["Nama", "NIK", "Divisi", "Pre Test", "Post (sebelum remedial)", "Remedial terakhir", "Jumlah remedial",
                "Nilai akhir", "Selisih akhir - Pre", "Status"])
    ws3.append(["Tanggal", "Nama", "NIK", "Divisi", "Tipe", "Soal", "Jawaban peserta", "Kunci", "Hasil", "Waktu (dtk)"])
    ws4.append(["Waktu", "Nama", "NIK", "Divisi", "Aktivitas", "Detail", "Durasi (dtk)"])
    for r in rows[::-1]:
        dn = dm.get(r["div_id"], "-")
        ws.append([r["created"], r["name"], r["nik"], dn, PHASE[r["phase"]], LEVELS.get(r["level"], ("-",))[0],
                   r["correct"], r["total"], r["score"], r["esc"] or 0, r["rej"] or 0, r["kw"] or 0, r["avgt"] or 0,
                   r["dur"] or 0, "LULUS" if passed(r) else "BELUM LULUS"])
        for d in json.loads(r["detail"]):
            ws3.append([r["created"], r["name"], r["nik"], dn, PHASE[r["phase"]], d["text"], d["yours"], d["key"],
                        "Benar" if d["good"] else "Salah", d.get("t")])
    for d in progress_rows(int(f["dv"]) if f["dv"].isdigit() else None, f["df"], f["dt"], f["s"])[0]:
        ws2.append([d["name"], d["nik"], d["div"], d["pre"], d["post"], d["remed"], d["nrem"], d["final"], d["diff"],
                    d["status"].upper()])
    for e in q("SELECT * FROM events ORDER BY id"):
        ws4.append([e["at"], e["name"], e["nik"], dm.get(e["div_id"], "-"), EVENT.get(e["kind"], e["kind"]),
                    e["detail"], round(e["secs"]) if e["secs"] is not None else None])
    for sh in (ws, ws2, ws3, ws4):
        for c in sh[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="7C3AED")
        for col in sh.columns:
            sh.column_dimensions[col[0].column_letter].width = 22
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"riwayat-{now_dt():%Y%m%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def div_or_none(v):
    return int(v) if str(v or "").isdigit() and get_div(int(v)) else None


# ----- divisi -----
@app.route("/admin/divisi", methods=["GET", "POST"])
@admin_only
def admin_divs():
    if request.method == "POST":
        n = request.form.get("name", "").strip()
        if not n:
            flash("Isi nama divisi.")
        elif q("SELECT id FROM divisions WHERE LOWER(name)=LOWER(?)", (n,), True):
            flash("Nama divisi sudah ada.")
        else:
            q("INSERT INTO divisions(name,active) VALUES(?,0)", (n,))
            flash("Divisi ditambahkan (masih nonaktif, aktifkan saat training akan dimulai).")
        return redirect(url_for("admin_divs"))
    rows = []
    for d in q("SELECT * FROM divisions ORDER BY name COLLATE NOCASE"):
        c = {r["phase"]: r["c"] for r in q("SELECT phase, COUNT(*) c FROM questions WHERE div_id=? GROUP BY phase", (d["id"],))}
        rows.append(dict(d, pre=c.get("pre", 0), post=c.get("post", 0), remed=c.get("remed", 0),
                         mats=q("SELECT COUNT(*) c FROM materials WHERE div_id=?", (d["id"],), True)["c"],
                         scheds=q("SELECT COUNT(*) c FROM schedules WHERE div_id=?", (d["id"],), True)["c"]))
    free = {r["phase"]: r["c"] for r in q("SELECT phase, COUNT(*) c FROM questions WHERE div_id IS NULL GROUP BY phase")}
    return render_template("admin_divs.html", rows=rows, free=free)


@app.post("/admin/divisi/rename/<int:did>")
@admin_only
def div_rename(did):
    n = request.form.get("name", "").strip()
    if not n:
        flash("Nama divisi tidak boleh kosong.")
    elif q("SELECT id FROM divisions WHERE LOWER(name)=LOWER(?) AND id<>?", (n, did), True):
        flash("Nama divisi sudah ada.")
    else:
        q("UPDATE divisions SET name=? WHERE id=?", (n, did))
        flash("Nama divisi diubah.")
    return redirect(url_for("admin_divs"))


@app.post("/admin/divisi/aktif/<int:did>")
@admin_only
def div_toggle(did):
    q("UPDATE divisions SET active=1-active WHERE id=?", (did,))
    return redirect(url_for("admin_divs"))


@app.post("/admin/divisi/hapus/<int:did>")
@admin_only
def div_del(did):
    used = (q("SELECT COUNT(*) c FROM questions WHERE div_id=?", (did,), True)["c"]
            + q("SELECT COUNT(*) c FROM materials WHERE div_id=?", (did,), True)["c"]
            + q("SELECT COUNT(*) c FROM attempts WHERE div_id=?", (did,), True)["c"])
    if used:
        flash("Divisi tidak bisa dihapus karena sudah punya soal, materi, atau hasil. Nonaktifkan saja.")
    else:
        q("DELETE FROM schedules WHERE div_id=?", (did,))
        q("DELETE FROM divisions WHERE id=?", (did,))
        flash("Divisi dihapus.")
    return redirect(url_for("admin_divs"))


# ----- jadwal -----
@app.route("/admin/jadwal", methods=["GET", "POST"])
@admin_only
def admin_sched():
    if request.method == "POST":
        f = request.form
        try:
            s = datetime.strptime(f.get("start", "").replace("T", " ")[:16], DTMIN)
            e = datetime.strptime(f.get("end", "").replace("T", " ")[:16], DTMIN)
        except ValueError:
            flash("Isi waktu mulai dan selesai dengan benar.")
            return redirect(url_for("admin_sched"))
        if e <= s:
            flash("Waktu selesai harus setelah waktu mulai.")
            return redirect(url_for("admin_sched"))
        pids = ([r["id"] for r in q("SELECT id FROM people")] if f.get("all")
                else [int(x) for x in f.getlist("pids") if x.isdigit()])
        if not pids:
            flash("Pilih minimal satu peserta.")
            return redirect(url_for("admin_sched"))
        did = div_or_none(f.get("div"))
        for p in pids:
            q("INSERT INTO schedules(pid,div_id,t_start,t_end,created) VALUES(?,?,?,?,?)",
              (p, did, s.strftime(DTMIN), e.strftime(DTMIN), now()))
        flash(f"Jadwal ditambahkan untuk {len(pids)} peserta.")
        return redirect(url_for("admin_sched"))
    s = request.args.get("q", "").strip()
    sql, a = ("SELECT s.*, p.name, p.nik, d.name dname FROM schedules s JOIN people p ON p.id=s.pid "
              "LEFT JOIN divisions d ON d.id=s.div_id WHERE 1=1"), []
    if s:
        sql += " AND (p.name LIKE ? OR p.nik LIKE ?)"
        a += [f"%{s}%"] * 2
    rows = [dict(r, st=win_status(r)) for r in q(sql + " ORDER BY s.t_start DESC LIMIT 500", a)]
    d0 = now_dt().replace(minute=0, second=0) + timedelta(hours=1)
    return render_template("admin_sched.html", rows=rows, s=s,
                           nosched=q("SELECT COUNT(*) c FROM people p WHERE NOT EXISTS (SELECT 1 FROM schedules s WHERE s.pid=p.id AND s.t_end>=?)", (now_min(),), True)["c"], people=q("SELECT p.*, d.name dname FROM people p LEFT JOIN divisions d ON d.id=p.div_id ORDER BY p.name COLLATE NOCASE"),
                           divs=q("SELECT * FROM divisions ORDER BY name COLLATE NOCASE"),
                           ds=d0.strftime("%Y-%m-%dT%H:%M"), de=(d0 + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M"))


@app.post("/admin/jadwal/hapus/<int:sid>")
@admin_only
def sched_del(sid):
    q("DELETE FROM schedules WHERE id=?", (sid,))
    return redirect(url_for("admin_sched"))


@app.post("/admin/jadwal/bersih")
@admin_only
def sched_clean():
    q("DELETE FROM schedules WHERE t_end<?", (now_min(),))
    flash("Jadwal yang sudah berakhir dihapus.")
    return redirect(url_for("admin_sched"))


# ----- soal -----
@app.route("/admin/soal/<phase>", methods=["GET", "POST"])
@admin_only
def soal(phase):
    if phase not in PHASE:
        abort(404)
    if request.method == "POST":
        f = request.form
        res, err = parse_opts(f)
        if err:
            flash(err)
        elif not f.get("text", "").strip():
            flash("Pertanyaan tidak boleh kosong.")
        elif bad_image(request.files.get("image")) or bad_image(request.files.get("explain_img")):
            flash(IMG_MSG)
        else:
            opts, am, ak = res
            q("INSERT INTO questions(phase,text,image,opts,ans_main,ans_kid,explain,div_id,explain_img) VALUES(?,?,?,?,?,?,?,?,?)",
              (phase, f["text"].strip(), save_image(request.files.get("image")), json.dumps(opts), am, ak,
               f.get("explain", "").strip(), div_or_none(f.get("div")), save_image(request.files.get("explain_img"))))
            flash("Soal ditambahkan.")
        return redirect(url_for("soal", phase=phase, div=f.get("div", "") or None))
    flt = request.args.get("div", "")
    sql, a = "SELECT * FROM questions WHERE phase=?", [phase]
    if flt == "u":
        sql += " AND div_id IS NULL"
    elif flt.isdigit():
        sql += " AND div_id=?"; a.append(int(flt))
    qs = []
    for r in q(sql + " ORDER BY id", a):
        d = dict(r)
        d["opts"] = json.loads(d["opts"])
        qs.append(d)
    src = []
    for p in PHASE:
        if p != phase:
            for x in load_qs(p):
                x["plabel"] = PHASE[p]
                src.append(x)
    return render_template("admin_soal.html", phase=phase, title=PHASE[phase], qs=qs, flt=flt, src=src,
                           others=[(p, PHASE[p]) for p in PHASE if p != phase],
                           divs=q("SELECT * FROM divisions ORDER BY name COLLATE NOCASE"))


def copy_q(r, phase):
    # Salin satu soal (beserta foto soal, foto penjelasan, dan divisinya)
    q("INSERT INTO questions(phase,text,image,opts,ans_main,ans_kid,explain,div_id,explain_img) VALUES(?,?,?,?,?,?,?,?,?)",
      (phase, r["text"], clone_file(r["image"]), r["opts"], r["ans_main"], r["ans_kid"], r["explain"], r["div_id"],
       clone_file(r["explain_img"])))


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


@app.post("/admin/soal/divisi/<int:qid>")
@admin_only
def soal_set_div(qid):
    r = q("SELECT * FROM questions WHERE id=?", (qid,), True)
    if r:
        q("UPDATE questions SET div_id=? WHERE id=?", (div_or_none(request.form.get("div")), qid))
        flash("Divisi soal diubah.")
    return redirect(url_for("soal", phase=r["phase"] if r else "pre", div=request.form.get("flt") or None))


@app.post("/admin/soal/hapus/<int:qid>")
@admin_only
def soal_del(qid):
    r = q("SELECT * FROM questions WHERE id=?", (qid,), True)
    if r:
        drop_file(r["image"])
        drop_file(r["explain_img"])
        q("DELETE FROM questions WHERE id=?", (qid,))
    return redirect(url_for("soal", phase=r["phase"] if r else "pre"))


@app.post("/admin/soal/<phase>/hapus-semua")
@admin_only
def soal_del_all(phase):
    if phase not in PHASE:
        abort(404)
    flt = request.form.get("flt", "")
    sql, a = "SELECT * FROM questions WHERE phase=?", [phase]
    if flt == "u":
        sql += " AND div_id IS NULL"
    elif flt.isdigit():
        sql += " AND div_id=?"; a.append(int(flt))
    rows = q(sql, a)
    for r in rows:
        drop_file(r["image"])
        drop_file(r["explain_img"])
        q("DELETE FROM questions WHERE id=?", (r["id"],))
    flash(f"{len(rows)} soal {PHASE[phase]} dihapus.")
    return redirect(url_for("soal", phase=phase, div=flt or None))


@app.route("/admin/soal/edit/<int:qid>", methods=["GET", "POST"])
@admin_only
def soal_edit(qid):
    r = q("SELECT * FROM questions WHERE id=?", (qid,), True)
    if not r:
        abort(404)
    if request.method == "POST":
        f, fl = request.form, request.files
        res, err = parse_opts(f)
        if err:
            flash(err)
        elif not f.get("text", "").strip():
            flash("Pertanyaan tidak boleh kosong.")
        elif bad_image(fl.get("image")) or bad_image(fl.get("explain_img")):
            flash(IMG_MSG)
        else:
            opts, am, ak = res
            img, eimg = r["image"], r["explain_img"]
            new = save_image(fl.get("image"))
            if new:
                drop_file(img)
                img = new
            elif f.get("rm_image"):
                drop_file(img)
                img = None
            new = save_image(fl.get("explain_img"))
            if new:
                drop_file(eimg)
                eimg = new
            elif f.get("rm_explain_img"):
                drop_file(eimg)
                eimg = None
            q("UPDATE questions SET text=?,image=?,opts=?,ans_main=?,ans_kid=?,explain=?,div_id=?,explain_img=? WHERE id=?",
              (f["text"].strip(), img, json.dumps(opts), am, ak, f.get("explain", "").strip(),
               div_or_none(f.get("div")), eimg, qid))
            flash("Soal diperbarui.")
            return redirect(url_for("soal", phase=r["phase"]))
        return redirect(url_for("soal_edit", qid=qid))
    d = dict(r)
    d["opts"] = json.loads(d["opts"])
    slots = []
    for i in range(4):
        if i < len(d["opts"]):
            o = d["opts"][i]
            kids = [("*" if (i == d["ans_main"] and k == d["ans_kid"]) else "") + t for k, t in enumerate(o["kids"])]
            slots.append({"label": o["label"], "kids": "\n".join(kids)})
        else:
            slots.append({"label": "", "kids": ""})
    return render_template("admin_soal_edit.html", x=d, slots=slots, title=PHASE[r["phase"]],
                           divs=q("SELECT * FROM divisions ORDER BY name COLLATE NOCASE"))


# ----- materi -----
@app.route("/admin/materi", methods=["GET", "POST"])
@admin_only
def admin_materi():
    if request.method == "POST":
        name = save_file(request.files.get("file"))
        if name:
            q("INSERT INTO materials(title,filename,created,div_id) VALUES(?,?,?,?)",
              (request.form.get("title", "").strip() or name, name, now(), div_or_none(request.form.get("div"))))
            flash("Materi diunggah.")
        return redirect(url_for("admin_materi"))
    flt = request.args.get("div", "")
    sql, a = "SELECT * FROM materials WHERE 1=1", []
    if flt == "u":
        sql += " AND div_id IS NULL"
    elif flt.isdigit():
        sql += " AND div_id=?"; a.append(int(flt))
    return render_template("admin_materi.html", mats=q(sql + " ORDER BY id DESC", a), flt=flt,
                           divs=q("SELECT * FROM divisions ORDER BY name COLLATE NOCASE"))


@app.post("/admin/materi/hapus-semua")
@admin_only
def materi_del_all():
    flt = request.form.get("flt", "")
    sql, a = "SELECT * FROM materials WHERE 1=1", []
    if flt == "u":
        sql += " AND div_id IS NULL"
    elif flt.isdigit():
        sql += " AND div_id=?"; a.append(int(flt))
    rows = q(sql, a)
    for r in rows:
        drop_file(r["filename"])
        q("DELETE FROM materials WHERE id=?", (r["id"],))
    flash(f"{len(rows)} materi dihapus.")
    return redirect(url_for("admin_materi", div=flt or None))


@app.post("/admin/materi/divisi/<int:mid>")
@admin_only
def materi_set_div(mid):
    q("UPDATE materials SET div_id=? WHERE id=?", (div_or_none(request.form.get("div")), mid))
    flash("Divisi materi diubah.")
    return redirect(url_for("admin_materi"))


@app.post("/admin/materi/hapus/<int:mid>")
@admin_only
def materi_del(mid):
    r = q("SELECT * FROM materials WHERE id=?", (mid,), True)
    if r:
        drop_file(r["filename"])
        q("DELETE FROM materials WHERE id=?", (mid,))
    return redirect(url_for("admin_materi"))


# ----- peserta & aktivitas -----
@app.route("/admin/peserta", methods=["GET", "POST"])
@admin_only
def admin_people():
    if request.method == "POST":
        dm = {d["name"].lower(): d["id"] for d in q("SELECT id,name FROM divisions")}
        sel = div_or_none(request.form.get("div"))
        rows = []
        if request.form.get("bulk", "").strip():
            for line in request.form["bulk"].splitlines():
                p = [x.strip() for x in re.split(r"[\t;,]", line) if x.strip()]
                if len(p) >= 2:
                    rows.append((p[0], p[1], dm.get(p[2].lower(), sel) if len(p) >= 3 else sel))
        elif request.form.get("name", "").strip() and request.form.get("nik", "").strip():
            rows.append((request.form["name"].strip(), request.form["nik"].strip(), sel))
        add = skip = 0
        for n, k, dv in rows:
            if q("SELECT id FROM people WHERE LOWER(nik)=LOWER(?)", (k,), True):
                skip += 1
            else:
                q("INSERT INTO people(name,nik,div_id) VALUES(?,?,?)", (n, k, dv))
                add += 1
        flash(f"{add} peserta ditambahkan" + (f", {skip} dilewati (NIK sudah ada)." if skip else ".")
              if rows else "Isi nama dan NIK.")
        return redirect(url_for("admin_people"))
    flt = request.args.get("div", "")
    divs = q("SELECT * FROM divisions ORDER BY name COLLATE NOCASE")
    people = q("SELECT * FROM people ORDER BY name COLLATE NOCASE")
    ids = {d["id"] for d in divs}
    groups = [{"id": d["id"], "name": d["name"], "people": [p for p in people if p["div_id"] == d["id"]]} for d in divs]
    free = [p for p in people if p["div_id"] not in ids]
    if flt == "u":
        groups = [{"id": "", "name": "Tanpa divisi", "people": free}]
    elif flt.isdigit():
        groups = [g for g in groups if g["id"] == int(flt)]
    elif free:
        groups.append({"id": "", "name": "Tanpa divisi", "people": free})
    return render_template("admin_people.html", groups=groups, flt=flt, divs=divs, total=len(people))


@app.post("/admin/peserta/edit/<int:pid>")
@admin_only
def people_edit(pid):
    p = q("SELECT * FROM people WHERE id=?", (pid,), True)
    name, nik = request.form.get("name", "").strip(), request.form.get("nik", "").strip()
    if not p or not name or not nik:
        flash("Nama dan NIK tidak boleh kosong.")
    elif q("SELECT id FROM people WHERE LOWER(nik)=LOWER(?) AND id<>?", (nik, pid), True):
        flash("NIK sudah dipakai peserta lain.")
    else:
        if nik != p["nik"]:                      # riwayat ikut berpindah ke NIK baru
            for t in ("attempts", "events", "resets"):
                q(f"UPDATE {t} SET nik=? WHERE nik=?", (nik, p["nik"]))
        q("UPDATE attempts SET name=? WHERE nik=?", (name, nik))
        q("UPDATE events SET name=? WHERE nik=?", (name, nik))
        q("UPDATE people SET name=?, nik=?, div_id=? WHERE id=?", (name, nik, div_or_none(request.form.get("div")), pid))
        flash("Data peserta disimpan.")
    return redirect(url_for("admin_people", div=request.form.get("flt") or None))


@app.post("/admin/peserta/hapus/<int:pid>")
@admin_only
def people_del(pid):
    q("DELETE FROM schedules WHERE pid=?", (pid,))
    q("DELETE FROM people WHERE id=?", (pid,))
    return redirect(url_for("admin_people"))


@app.post("/admin/aktivitas/hapus-semua")
@admin_only
def events_del_all():
    s, k = request.form.get("q", "").strip(), request.form.get("kind", "")
    sql, a = "FROM events WHERE 1=1", []
    if s:
        sql += " AND (name LIKE ? OR nik LIKE ?)"
        a += [f"%{s}%"] * 2
    if k in EVENT:
        sql += " AND kind=?"
        a.append(k)
    n = to_trash("event", q("SELECT * " + sql, a), "Hapus log aktivitas")
    q("DELETE " + sql, a)
    flash(f"{n} log aktivitas dipindahkan ke Sampah (tersimpan {TRASH_DAYS} hari, bisa dipulihkan).")
    return redirect(url_for("activity"))


@app.get("/admin/sampah")
@admin_only
def trash_page():
    purge_trash()
    k, s = request.args.get("kind", ""), request.args.get("q", "").strip()
    dm = {d["id"]: d["name"] for d in q("SELECT id,name FROM divisions")}
    sql, a = "SELECT * FROM trash WHERE 1=1", []
    if k in ("attempt", "event"):
        sql += " AND kind=?"
        a.append(k)
    n, items = now_dt(), []
    for t in q(sql + " ORDER BY id DESC LIMIT 1000", a):
        d = json.loads(t["data"])
        if t["kind"] == "attempt":
            summ = f"{d.get('name')} · {dm.get(d.get('div_id'), '-')} · {PHASE.get(d.get('phase'), '?')} · skor {d.get('score')}%"
            when = d.get("created")
        else:
            summ = f"{d.get('name')} · {EVENT.get(d.get('kind'), d.get('kind'))} · {d.get('detail') or ''}"
            when = d.get("at")
        if s and s.lower() not in summ.lower():
            continue
        left = max(0, -(-int((datetime.strptime(t["expires"], DTF) - n).total_seconds()) // 86400))
        items.append({"id": t["id"], "kind": t["kind"], "summ": summ, "when": when, "deleted": t["deleted_at"][:16],
                      "reason": t["reason"], "left": left})
    batches = q("SELECT batch, reason, kind, MAX(deleted_at) at, COUNT(*) c FROM trash GROUP BY batch ORDER BY MAX(id) DESC LIMIT 15")
    return render_template("admin_trash.html", items=items[:300], more=max(0, len(items) - 300), batches=batches, k=k, s=s,
                           days=TRASH_DAYS, total=q("SELECT COUNT(*) c FROM trash", (), True)["c"])


@app.post("/admin/sampah/pulihkan/<int:tid>")
@admin_only
def trash_restore(tid):
    t = q("SELECT * FROM trash WHERE id=?", (tid,), True)
    if t:
        restore_item(t)
        flash("Data dipulihkan ke tempat semula.")
    return redirect(url_for("trash_page"))


@app.post("/admin/sampah/pulihkan-batch/<batch>")
@admin_only
def trash_restore_batch(batch):
    rows = q("SELECT * FROM trash WHERE batch=? ORDER BY id", (batch,))
    for t in rows:
        restore_item(t)
    flash(f"{len(rows)} data dipulihkan.")
    return redirect(url_for("trash_page"))


@app.post("/admin/sampah/hapus/<int:tid>")
@admin_only
def trash_delete(tid):
    q("DELETE FROM trash WHERE id=?", (tid,))
    flash("Data dihapus permanen.")
    return redirect(url_for("trash_page"))


@app.post("/admin/sampah/kosongkan")
@admin_only
def trash_empty():
    if request.form.get("ok") == "HAPUS":
        q("DELETE FROM trash")
        flash("Sampah dikosongkan. Data dihapus permanen.")
    else:
        flash("Dibatalkan: konfirmasi tidak sesuai.")
    return redirect(url_for("trash_page"))


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
<html lang="id" class="h-full scroll-smooth">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Pelatihan{% endblock %} · Training Quality</title>
  <link rel="icon" href="{{ logo_url or 'data:,' }}">
  <style>
    .hidden{display:none}
    @media (max-width:640px){input,select,textarea{font-size:16px!important}}
    .sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}
    #twwarn{display:none;margin:0;padding:10px 16px;background:#fef3c7;color:#92400e;font:14px system-ui,sans-serif;text-align:center}
    .blob{position:fixed;z-index:-1;border-radius:9999px;filter:blur(80px);opacity:.5;pointer-events:none;animation:floaty 16s ease-in-out infinite}
    @keyframes floaty{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(36px,-44px) scale(1.1)}}
    @keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
    .fade-up{animation:fadeUp .65s cubic-bezier(.2,.8,.2,1) backwards;animation-delay:var(--d,0s)}
    .qpop{animation:fadeUp .25s ease-out backwards}
    @keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
    .bar-grow{transform-origin:left;animation:grow 1.1s cubic-bezier(.2,.8,.2,1) backwards;animation-delay:var(--d,.2s)}
    @keyframes pulsering{0%{box-shadow:0 0 0 0 rgba(139,92,246,.5)}70%{box-shadow:0 0 0 16px rgba(139,92,246,0)}100%{box-shadow:0 0 0 0 rgba(139,92,246,0)}}
    .pulse-ring{animation:pulsering 2.2s infinite}
    @keyframes bob{0%,100%{transform:translateY(0) rotate(-2deg)}50%{transform:translateY(-10px) rotate(2deg)}}
    .bob{animation:bob 4.5s ease-in-out infinite}
    @keyframes ringfill{from{stroke-dashoffset:326.7}}
    .ring-fill{animation:ringfill 1.4s cubic-bezier(.2,.8,.2,1) backwards .2s}
    @keyframes spinin{from{transform:rotate(-120deg) scale(.8);opacity:0}to{transform:none;opacity:1}}
    .donut{animation:spinin 1.1s cubic-bezier(.2,.8,.2,1) backwards .2s}
    @keyframes fall{0%{transform:translateY(-10vh) rotate(0);opacity:1}100%{transform:translateY(105vh) rotate(720deg);opacity:.9}}
    .confetti{position:fixed;top:0;z-index:60;width:9px;height:14px;border-radius:2px;pointer-events:none;animation:fall linear forwards}
    .text-logo{color:#d6001a}
    .text-grad{background:linear-gradient(90deg,#7c3aed,#d946ef,#ec4899);-webkit-background-clip:text;background-clip:text;color:transparent}
    @media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
  </style>
  {% if local_tw %}<script src="{{ url_for('static', filename='tailwind.js') }}"></script>
  {% else %}<script src="https://cdn.tailwindcss.com"></script>{% endif %}
  <script>
    tailwind.config = {theme: {extend: {
      fontFamily: {sans: ['ui-sans-serif','system-ui','Segoe UI','Roboto','Helvetica Neue','sans-serif']},
      colors: {brand: {50:'#f5f3ff',100:'#ede9fe',200:'#ddd6fe',300:'#c4b5fd',400:'#a78bfa',500:'#8b5cf6',600:'#7c3aed',700:'#6d28d9',800:'#5b21b6',900:'#4c1d95'}}
    }}}
  </script>
  <style type="text/tailwindcss">
    @layer components {
      .card {@apply bg-white/90 backdrop-blur rounded-2xl border border-brand-100/70 shadow-lg shadow-brand-500/5}
      .btn {@apply inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold transition duration-200 focus:outline-none focus:ring-4 focus:ring-brand-200/60 disabled:opacity-50 active:scale-[.98]}
      .btn-primary {@apply bg-gradient-to-r from-brand-600 to-pink-500 text-white shadow-lg shadow-brand-500/30 hover:shadow-xl hover:shadow-brand-500/40 hover:-translate-y-0.5 hover:brightness-110}
      .btn-ghost {@apply bg-white text-brand-700 border border-brand-100 hover:bg-brand-50 hover:border-brand-200}
      .btn-danger {@apply bg-white text-rose-600 border border-rose-200 hover:bg-rose-50}
      .input {@apply w-full rounded-xl border border-brand-100 bg-white px-3.5 py-2.5 text-sm placeholder:text-slate-400 focus:border-brand-500 focus:outline-none focus:ring-4 focus:ring-brand-100}
      .label {@apply block text-xs font-semibold uppercase tracking-wide text-brand-600/80 mb-1.5}
      .th {@apply px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-brand-700/70 whitespace-nowrap}
      .td {@apply px-4 py-3 text-sm}
      .pill {@apply inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold}
    }
  </style>
</head>
<body class="min-h-full bg-gradient-to-br from-violet-50 via-white to-pink-50 bg-fixed font-sans text-slate-800 antialiased">
<div class="blob" style="top:-120px;left:-110px;width:380px;height:380px;background:#c4b5fd"></div>
<div class="blob" style="top:32%;right:-150px;width:420px;height:420px;background:#fbcfe8;animation-delay:-5s"></div>
<div class="blob" style="bottom:-170px;left:28%;width:380px;height:380px;background:#ddd6fe;animation-delay:-9s"></div>

<p id="twwarn">Tampilan gagal dimuat (Tailwind tidak terbaca). Pastikan perangkat punya internet, atau simpan https://cdn.tailwindcss.com sebagai static/tailwind.js lalu muat ulang.</p>
<script>window.addEventListener('load',function(){if(typeof window.tailwind==='undefined')document.getElementById('twwarn').style.display='block';});</script>

{% set is_admin_area = session.get('admin') and request.path.startswith('/admin') %}
{% set nav = [('/admin','Dashboard','📊'),('/admin/divisi','Divisi','🏭'),('/admin/peserta','Peserta','👥'),('/admin/jadwal','Jadwal','📅'),('/admin/soal/pre','Soal Pre','📝'),('/admin/soal/post','Soal Post','✅'),('/admin/soal/remed','Soal Remedial','🔁'),('/admin/materi','Materi','📚'),('/admin/aktivitas','Aktivitas','🕒'),('/admin/sampah','Sampah','🗑️')] %}
<header class="sticky top-0 z-30 border-b border-white/60 bg-white/70 backdrop-blur-xl">
  <div class="h-0.5 bg-gradient-to-r from-brand-500 via-fuchsia-500 to-pink-500"></div>
  <div class="mx-auto flex {{ 'max-w-[1560px]' if is_admin_area else 'max-w-6xl' }} items-center justify-between gap-4 px-4 py-3">
    <a href="/" class="group flex items-center gap-3">
      {% if logo_url %}<img src="{{ logo_url }}" alt="Logo" class="h-10 w-10 object-contain">
      {% else %}<span class="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-brand-600 to-pink-500 text-white shadow-lg shadow-brand-500/30 transition group-hover:rotate-6 group-hover:scale-105">
        <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"/></svg>
      </span>{% endif %}
      <span class="leading-tight"><span class="block text-[10px] font-bold uppercase tracking-[.25em] text-brand-500">Training</span>
      <span class="block text-sm font-extrabold text-grad">Quality</span></span>
    </a>
    <div class="flex items-center gap-3 text-sm">
      {% if session.get('name') and not is_admin_area %}
        <span class="hidden text-slate-500 sm:block">{{ session.name }}{% if session.get('div') %} · {{ divmap.get(session.div, '') }}{% endif %}</span>
        {% if session.get('div') and not all_done %}<a href="/divisi/ganti" class="btn btn-ghost !px-3 !py-1.5 !text-xs sm:!px-4 sm:!text-sm">Ganti divisi</a>{% endif %}
        <a href="/selesai" class="btn btn-ghost !px-3 !py-1.5 !text-xs sm:!px-4 sm:!text-sm">Keluar</a>
      {% endif %}
      {% if session.get('admin') %}
        <a href="/admin" class="btn btn-ghost !py-1.5">Admin</a>
      {% elif not session.get('name') %}
        <a href="/admin/login" class="text-slate-400 transition hover:text-brand-600">Admin</a>
      {% endif %}
    </div>
  </div>
  {% if is_admin_area %}
  <nav class="flex gap-1 overflow-x-auto px-4 pb-2 lg:hidden">
    {% for href, t, ic in nav %}
      {% set on = request.path == href or (href != '/admin' and request.path.startswith(href)) %}
      <a href="{{ href }}" class="whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition {{ 'bg-gradient-to-r from-brand-600 to-pink-500 text-white shadow shadow-brand-500/30' if on else 'text-slate-500 hover:bg-brand-50 hover:text-brand-700' }}">{{ ic }} {{ t }}</a>
    {% endfor %}
    <a href="/admin/logout" class="ml-auto whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium text-rose-600 transition hover:bg-rose-50">Logout</a>
  </nav>
  {% endif %}
</header>

<div class="{% if is_admin_area %}mx-auto flex max-w-[1560px] items-start gap-6 px-4 py-6{% endif %}">
{% if is_admin_area %}
<aside class="sticky top-24 hidden w-60 shrink-0 lg:block">
  <nav class="card space-y-1 p-3">
    <p class="px-3 pb-1 pt-2 text-[10px] font-bold uppercase tracking-[.2em] text-slate-400">Menu admin</p>
    {% for href, t, ic in nav %}
      {% set on = request.path == href or (href != '/admin' and request.path.startswith(href)) %}
      <a href="{{ href }}" class="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition {{ 'bg-gradient-to-r from-brand-600 to-pink-500 text-white shadow-md shadow-brand-500/30' if on else 'text-slate-600 hover:bg-brand-50 hover:text-brand-700' }}">
        <span class="grid h-7 w-7 place-items-center rounded-lg text-base {{ 'bg-white/25' if on else 'bg-brand-50' }}">{{ ic }}</span>{{ t }}
      </a>
    {% endfor %}
    <div class="my-2 border-t border-brand-100"></div>
    <a href="/admin/logout" class="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium text-rose-600 transition hover:bg-rose-50">
      <span class="grid h-7 w-7 place-items-center rounded-lg bg-rose-50 text-base">↩</span>Logout</a>
  </nav>
</aside>
{% endif %}
<main class="{% if is_admin_area %}min-w-0 flex-1{% else %}mx-auto w-full px-4 py-8 {% block width %}max-w-3xl{% endblock %}{% endif %}">
  {% with msgs = get_flashed_messages() %}
    {% for m in msgs %}
      <div class="fade-up mb-4 flex items-start gap-3 rounded-xl border border-brand-200 bg-gradient-to-r from-brand-50 to-pink-50 px-4 py-3 text-sm text-brand-800">
        <svg class="mt-0.5 h-4 w-4 shrink-0 text-brand-500" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Zm-7-4a1 1 0 1 1-2 0 1 1 0 0 1 2 0ZM9 9a1 1 0 0 0 0 2v3a1 1 0 0 0 1 1h1a1 1 0 1 0 0-2v-3a1 1 0 0 0-1-1H9Z" clip-rule="evenodd"/></svg>
        <span>{{ m }}</span>
      </div>
    {% endfor %}
  {% endwith %}
  <div class="{% if is_admin_area %}{% block awidth %}max-w-6xl{% endblock %}{% endif %}">
  {% block content %}{% endblock %}
  </div>
</main>
</div>

<footer class="pb-8 text-center text-xs text-slate-400">Training Quality</footer>

<script>
(function () {
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.querySelectorAll('[data-count]').forEach(function (e) {
    var to = parseFloat(e.getAttribute('data-count')) || 0, dec = parseInt(e.getAttribute('data-dec') || '0', 10), t0 = null;
    if (reduce) { e.textContent = to.toFixed(dec); return; }
    function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / 900, 1), v = to * (1 - Math.pow(1 - p, 3));
      e.textContent = v.toFixed(dec);
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });
})();
</script>
</body>
</html>
'''

TEMPLATES["home.html"] = r'''{% extends "base.html" %}
{% block title %}Beranda{% endblock %}
{% block content %}
{% if not session.get('name') %}
  <div class="mx-auto max-w-md pt-2">
    <div class="fade-up mb-8 text-center">
      {% if logo_url %}
      <img src="{{ logo_url }}" alt="Logo" class="mx-auto mb-6 h-32 w-auto max-w-[260px] object-contain">
      {% else %}
      <div class="relative mx-auto mb-6 grid h-24 w-24 place-items-center rounded-[1.75rem] bg-gradient-to-br from-brand-600 via-fuchsia-500 to-pink-500 text-white shadow-2xl shadow-brand-500/40">
        <svg class="h-12 w-12" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z"/></svg>
      </div>
      {% endif %}
      <p class="text-[11px] font-bold uppercase tracking-[.35em] text-brand-500">Pelatihan Operator</p>
      <h1 class="mt-2 text-3xl font-extrabold leading-tight tracking-tight sm:text-4xl"><span class="text-logo">Training Quality</span></h1>
      <p class="mt-3 text-slate-500">Pilih nama dan masukkan NIK untuk memulai.</p>
    </div>
    <form method="post" class="card fade-up space-y-4 p-6" style="--d:.15s">
      <div>
        <label class="label">Nama</label>
        <select name="pid" required class="input">
          <option value="">Pilih nama…</option>
          {% for g, items in people|groupby('g') %}<optgroup label="{{ g }}">{% for p in items %}<option value="{{ p.id }}">{{ p.name }}</option>{% endfor %}</optgroup>{% endfor %}
        </select>
      </div>
      <div>
        <label class="label">NIK</label>
        <input name="nik" required autocomplete="off" placeholder="Masukkan NIK" class="input">
      </div>
      <button class="btn btn-primary w-full !py-3">Masuk</button>
      <p class="text-center text-xs text-slate-400">Masuk hanya bisa saat jadwal training kamu aktif.</p>
    </form>
  </div>
{% else %}
  <div class="fade-up mb-8 flex items-center gap-4">
    <div class="grid h-16 w-16 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-brand-600 to-pink-500 text-2xl font-extrabold text-white shadow-lg shadow-brand-500/30">{{ session.name[:1]|upper }}</div>
    <div class="min-w-0">
      <p class="text-sm text-slate-500">Halo,</p>
      <h1 class="truncate text-2xl font-extrabold tracking-tight sm:text-3xl">{{ session.name }}</h1>
      <div class="mt-1 flex flex-wrap gap-1.5">
        <span class="pill bg-brand-50 text-brand-700 ring-1 ring-brand-200">NIK {{ session.nik }}</span>
        <span class="pill bg-pink-50 text-pink-700 ring-1 ring-pink-200">Divisi {{ dname }}</span>
        {% if until %}<span class="pill bg-slate-100 text-slate-600">Jadwal sampai {{ until }}</span>{% endif %}
      </div>
    </div>
  </div>

  {% if alert %}
  <div class="fade-up mb-4 rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">⚠️ {{ alert }}</div>
  {% endif %}

  <div class="space-y-3">
    {% for c in cards %}
      {% set tag = 'a' if c.on else 'div' %}
      <{{ tag }} {% if c.on %}href="{{ c.href }}"{% endif %} style="--d:{{ loop.index0 * 0.1 + 0.15 }}s"
         class="card fade-up group relative flex items-center gap-4 overflow-hidden p-5 transition duration-300 {{ 'hover:-translate-y-1 hover:border-brand-300 hover:shadow-xl hover:shadow-brand-500/15' if c.on else '' }} {{ 'opacity-60' if c.lock else '' }}">
        {% if c.on and not c.done %}<span class="absolute inset-y-0 left-0 w-1.5 bg-gradient-to-b from-brand-500 to-pink-500"></span>{% endif %}
        <div class="grid h-12 w-12 shrink-0 place-items-center rounded-xl text-lg font-bold text-white
             {{ 'bg-gradient-to-br from-emerald-400 to-teal-500' if c.done else ('bg-gradient-to-br from-brand-600 to-pink-500 pulse-ring' if c.on else 'bg-slate-200 !text-slate-400') }}">
          {% if c.done %}✓{% else %}{{ loop.index }}{% endif %}
        </div>
        <div class="min-w-0 flex-1">
          <p class="text-xs font-semibold uppercase tracking-wide text-brand-400">{{ c.step }} · {{ c.meta }}</p>
          <h2 class="text-lg font-bold">{{ c.title }}</h2>
          <p class="text-sm text-slate-500">{{ c.desc }}{% if c.lock and c.note %} <span class="text-amber-600">🔒 {{ c.note }}</span>{% endif %}</p>
        </div>
        {% if c.done %}<span class="pill bg-emerald-100 text-emerald-700">Selesai</span>
        {% elif c.on %}<span class="grid h-9 w-9 place-items-center rounded-full bg-brand-50 text-brand-600 transition group-hover:translate-x-1 group-hover:bg-brand-600 group-hover:text-white">→</span>{% endif %}
      </{{ tag }}>
    {% endfor %}
  </div>

  {% if all_done %}
    {% if cc %}{% include "_concl.html" %}{% endif %}
    <div class="card fade-up mt-6 p-6 text-center" style="--d:.4s">
      <p class="text-3xl">🎉</p>
      <h3 class="mt-1 text-lg font-bold">Semua tahap selesai</h3>
      <p class="mt-1 text-sm text-slate-500">Terima kasih sudah mengikuti pelatihan divisi {{ dname }}.</p>
      <a href="/selesai" class="btn btn-primary mt-4 !px-10 !py-3">Selesai</a>
    </div>
  {% endif %}
{% endif %}
{% endblock %}
'''

TEMPLATES["divisi.html"] = r'''{% extends "base.html" %}
{% block title %}Pilih Divisi{% endblock %}
{% block content %}
<div class="fade-up">
  <p class="text-sm text-slate-500">Halo, {{ session.name }}</p>
  <h1 class="text-3xl font-extrabold tracking-tight">Pilih <span class="text-grad">Divisi Quality</span></h1>
  <p class="mt-1 text-sm text-slate-500">Divisi aktif jika sudah dibuka admin dan sesuai jadwal training kamu.</p>
</div>

<div class="mt-6 grid gap-3 sm:grid-cols-2">
  {% for d in items %}
    {% set tag = 'a' if d.open else 'div' %}
    <{{ tag }} {% if d.open %}href="/divisi/pilih/{{ d.id }}"{% endif %} style="--d:{{ loop.index0 * 0.07 + 0.1 }}s"
       class="card fade-up group flex items-center gap-4 p-5 transition duration-300 {{ 'hover:-translate-y-1 hover:border-brand-300 hover:shadow-xl hover:shadow-brand-500/15' if d.open else 'opacity-60' }}">
      <div class="grid h-12 w-12 shrink-0 place-items-center rounded-xl text-lg font-bold text-white {{ 'bg-gradient-to-br from-brand-600 to-pink-500' if d.open else 'bg-slate-200 !text-slate-400' }}">{{ d.name[:1]|upper }}</div>
      <div class="min-w-0 flex-1">
        <h2 class="truncate text-lg font-bold">{{ d.name }}</h2>
        <p class="text-sm {{ 'text-emerald-600' if d.open else 'text-amber-600' }}">{{ ('Lihat hasil training' if d.state == 'selesai' else 'Tersedia, klik untuk mulai') if d.open else '🔒 ' ~ d.why }}</p>
        {% if d.state == 'selesai' %}<span class="pill mt-1 bg-emerald-100 text-emerald-700">✓ Sudah selesai</span>
        {% elif d.state == 'baru' %}<span class="pill mt-1 bg-sky-100 text-sky-700">Jadwal baru tersedia</span>
        {% elif d.state == 'jalan' %}<span class="pill mt-1 bg-amber-100 text-amber-700">Sedang berjalan</span>{% endif %}
      </div>
      {% if d.open %}<span class="grid h-9 w-9 place-items-center rounded-full bg-brand-50 text-brand-600 transition group-hover:translate-x-1 group-hover:bg-brand-600 group-hover:text-white">→</span>{% endif %}
    </{{ tag }}>
  {% else %}
    <div class="card p-8 text-center text-slate-500 sm:col-span-2">Belum ada divisi. Hubungi admin.</div>
  {% endfor %}
</div>

{% if ws %}
<div class="card fade-up mt-6 p-5" style="--d:.3s">
  <h2 class="font-bold">Jadwal training kamu</h2>
  <ul class="mt-3 space-y-2 text-sm">
    {% for w in ws %}
    <li class="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-brand-50/60 px-3 py-2">
      <span>{{ w.t_start }} s/d {{ w.t_end }} <span class="text-slate-400">· {{ w.dname }}</span></span>
      <span class="pill {{ 'bg-emerald-100 text-emerald-700' if w.st == 'aktif' else ('bg-sky-100 text-sky-700' if w.st == 'akan' else 'bg-slate-100 text-slate-500') }}">{{ 'Sedang berlangsung' if w.st == 'aktif' else ('Akan datang' if w.st == 'akan' else 'Berakhir') }}</span>
    </li>
    {% endfor %}
  </ul>
</div>
{% endif %}
{% endblock %}
'''

TEMPLATES["materi.html"] = r'''{% extends "base.html" %}
{% block title %}Materi{% endblock %}
{% block content %}
<a href="/" class="text-sm text-slate-500 transition hover:text-brand-600">← Kembali</a>
<div class="fade-up mt-2">
  <h1 class="text-3xl font-extrabold tracking-tight">Materi <span class="text-grad">Pelatihan</span></h1>
  <p class="mt-1 text-sm text-slate-500">Divisi {{ dname }}. Buka (Lihat atau Unduh) minimal satu materi sebelum {{ 'Post Remedial' if remed_mode else 'Post Test' }}.</p>
</div>

<div class="mt-6 space-y-3">
  {% for m in mats %}
  <div class="card fade-up flex flex-wrap items-center gap-4 p-4 transition hover:-translate-y-0.5 hover:shadow-xl hover:shadow-brand-500/10" style="--d:{{ loop.index0 * 0.07 + 0.1 }}s">
    <div class="grid h-11 w-11 place-items-center rounded-xl bg-gradient-to-br from-brand-100 to-pink-100 text-brand-600">
      <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z"/></svg>
    </div>
    <div class="min-w-0 flex-1">
      <p class="truncate font-semibold">{{ m.title }}</p>
      <p class="text-xs text-slate-400">{{ m.created }}</p>
    </div>
    <div class="flex gap-2">
      <a href="/materi/buka/{{ m.id }}" target="_blank" class="mat-link btn btn-primary !py-2">Lihat</a>
      <a href="/materi/buka/{{ m.id }}?dl=1" class="mat-link btn btn-ghost !py-2">Unduh</a>
    </div>
  </div>
  {% else %}
  <div class="card p-8 text-center text-slate-500">Belum ada materi untuk divisi ini.</div>
  {% endfor %}
</div>

<div class="card fade-up mt-6 flex flex-wrap items-center justify-between gap-4 border-brand-200 bg-gradient-to-r from-brand-50 to-pink-50 p-5" style="--d:.3s">
  <div>
    <p class="font-bold text-brand-800">Sudah selesai belajar?</p>
    <p id="nexthint" class="text-sm text-brand-700/80">
      {% if nx.done %}{{ nx.hint }}
      {% elif not nx.has %}{{ nx.no }}
      {% elif nx.ok %}{{ nx.hint_ok }}
      {% else %}{{ nx.hint_lock }}{% endif %}
    </p>
  </div>
  {% if nx.done %}
    <a href="/" class="btn btn-primary">Kembali ke Beranda</a>
  {% elif nx.has %}
    <a id="nextbtn" href="{{ nx.href }}" class="btn btn-primary !px-6 !py-3 {{ '' if nx.ok else 'pointer-events-none opacity-40' }}">{{ nx.label }}</a>
  {% endif %}
</div>

{% if nx.has %}
<script>
(function () {
  var btn = document.getElementById('nextbtn');
  if (!btn) return;
  document.querySelectorAll('.mat-link').forEach(function (a) {
    a.addEventListener('click', function () {
      setTimeout(function () {
        btn.classList.remove('pointer-events-none', 'opacity-40');
        document.getElementById('nexthint').textContent = {{ nx.hint_ok|tojson }};
      }, 700);
    });
  });
})();
</script>
{% endif %}
{% endblock %}
'''

TEMPLATES["quiz.html"] = r'''{% extends "base.html" %}
{% block title %}{{ title }}{% endblock %}
{% block content %}
<div class="mx-auto max-w-lg">
  <a href="/" class="text-sm text-slate-500 hover:text-slate-800">&larr; Kembali</a>
  <h1 class="mt-4 text-3xl font-semibold tracking-tight">{{ title }}</h1>
  <p class="mt-1 text-sm font-medium text-brand-600">Divisi {{ dname }}</p>
  <p class="mt-2 text-slate-500">{{ data|length }} soal, urutan diacak. Soal muncul satu per satu dengan batas waktu.</p>

  <div id="qerr" style="display:none" class="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700"></div>

  {% if not data %}
    <div class="card mt-8 p-6 text-slate-500">Belum ada soal.</div>
  {% else %}
  <div class="card mt-8 p-6">
    <p class="text-sm font-medium text-slate-700">Pilih level waktu per soal</p>
    <div class="mt-3 grid grid-cols-3 gap-2.5">
      {% for k, v in levels.items() %}
      <div>
        <input type="radio" id="lv-{{ k }}" name="lv" value="{{ k }}" {{ 'checked' if k == 'normal' }} class="peer sr-only">
        <label for="lv-{{ k }}" class="block cursor-pointer rounded-xl border border-slate-200 px-3 py-4 text-center transition hover:bg-slate-50 peer-checked:border-brand-600 peer-checked:bg-brand-50 peer-checked:ring-2 peer-checked:ring-brand-100">
          <span class="block font-semibold">{{ v[0] }}</span>
          <span class="mt-0.5 block text-xs text-slate-500">{{ v[1] }} detik</span>
        </label>
      </div>
      {% endfor %}
    </div>
    <p class="mt-4 hidden text-xs text-slate-400 sm:block">Tips: tekan angka 1 sampai 4 di keyboard untuk menjawab.</p>
    <button id="go" type="button" class="btn btn-primary mt-5 w-full !py-3.5">Mulai {{ title }}</button>
  </div>
  {% endif %}
</div>

<div id="ov" style="display:none;position:fixed;inset:0;z-index:50;padding:1rem;overflow-y:auto;background:rgba(46,16,101,.55);backdrop-filter:blur(6px)">
  <div id="card" class="m-auto w-full max-w-xl overflow-hidden rounded-3xl bg-white shadow-2xl">
    <div class="h-1.5 bg-slate-100"><div id="tb" class="h-full bg-gradient-to-r from-brand-500 to-pink-500" style="width:100%"></div></div>
    <div class="p-6 sm:p-8">
      <div class="flex items-center justify-between text-xs">
        <span id="no" class="font-medium text-brand-600"></span>
        <span id="tl" class="font-mono tabular-nums text-slate-500"></span>
      </div>
      <h2 id="qt" class="mt-3 text-xl font-semibold leading-snug"></h2>
      <img id="qi" alt="" style="display:none" class="mt-4 max-h-64 w-full rounded-2xl border border-slate-200 bg-slate-50 object-contain">
      <p id="hint" class="label mt-5"></p>
      <div id="op" class="grid gap-2.5 sm:grid-cols-2"></div>
    </div>
  </div>
  <div id="sending" style="display:none" class="m-auto w-full max-w-sm rounded-3xl bg-white p-10 text-center shadow-2xl">
    <div class="mx-auto h-8 w-8 animate-spin rounded-full border-4 border-brand-100 border-t-brand-600"></div>
    <p class="mt-4 font-semibold">Menyimpan hasil...</p>
    <button id="retry" type="button" style="display:none" class="btn btn-primary mt-4">Coba kirim lagi</button>
  </div>
</div>

<script>
/* Semua kode dibungkus fungsi: tidak bentrok dengan script lain di halaman. */
(function () {
  'use strict';
  var DATA = {{ data|tojson }};
  var LEVELS = {{ levels|tojson }};
  var SUBMIT = "/tes/{{ phase }}/kirim";
  var IMG = "/file/";
  var OPT_CLASS = "flex w-full items-center gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3.5 text-left text-sm font-medium transition hover:border-brand-500 hover:bg-brand-50 active:scale-[.99]";

  function el(id) { return document.getElementById(id); }
  function show(id, on, disp) { el(id).style.display = on ? (disp || 'block') : 'none'; }

  window.addEventListener('error', function (e) {
    var b = el('qerr'); if (!b) return;
    b.style.display = 'block';
    b.textContent = 'Terjadi error di halaman: ' + e.message + ' (kirim tulisan ini ke admin)';
  });

  var goBtn = el('go');
  if (!goBtn) return;

  var level, limit, idx = 0, answers = [], t0 = 0, timer = null, cur = null, pickedMain = -1, locked = true, playing = false;

  goBtn.addEventListener('click', function () {
    var r = document.querySelector('input[name=lv]:checked');
    level = r ? r.value : 'normal';
    limit = LEVELS[level][1];
    idx = 0; answers = []; playing = true;
    show('ov', true, 'flex');
    document.body.style.overflow = 'hidden';
    showQuestion();
  });

  function renderOpts(labels, hint, onPick) {
    el('hint').textContent = hint;
    var box = el('op'); box.innerHTML = '';
    labels.forEach(function (label, n) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = OPT_CLASS;
      var k = document.createElement('span');
      k.className = 'grid h-6 w-6 shrink-0 place-items-center rounded-md bg-slate-100 text-xs font-bold text-slate-500';
      k.textContent = n + 1;
      var t = document.createElement('span'); t.textContent = label;
      b.appendChild(k); b.appendChild(t);
      b.addEventListener('click', function () { onPick(n); });
      box.appendChild(b);
    });
  }

  function showQuestion() {
    if (idx >= DATA.length) return send();
    cur = DATA[idx]; pickedMain = -1; locked = true;
    var cd = el('card'); cd.classList.remove('qpop'); void cd.offsetWidth; cd.classList.add('qpop');
    clearInterval(timer);
    el('no').textContent = 'Soal ' + (idx + 1) + ' dari ' + DATA.length;
    el('qt').textContent = cur.text;
    el('tb').style.width = '100%';
    el('tl').textContent = limit + ' dtk';
    el('op').innerHTML = ''; el('hint').textContent = 'Memuat...';

    var my = idx, started = false;
    function begin() {
      if (started || my !== idx) return;
      started = true;
      renderOpts(cur.opts.map(function (o) { return o.label; }), 'Pilih jawaban', pickMain);
      startTimer();
    }
    var im = el('qi');
    if (cur.image) {
      show('qi', true);
      im.onload = im.onerror = begin;
      im.src = IMG + cur.image;
      if (im.complete && im.naturalWidth) begin();
      setTimeout(begin, 4000);
    } else { show('qi', false); begin(); }
  }

  function pickMain(n) {
    if (locked) return;
    pickedMain = n;
    var o = cur.opts[n];
    if (o.kids && o.kids.length) {
      renderOpts(o.kids, 'Pilih jenis: ' + o.label, function (k) { finish(n, k); });
    } else finish(n, -1);
  }

  function startTimer() {
    locked = false; t0 = performance.now();
    timer = setInterval(function () {
      var left = Math.max(limit - (performance.now() - t0) / 1000, 0);
      var bar = el('tb');
      bar.style.width = (left / limit * 100) + '%';
      bar.className = 'h-full ' + (left / limit < 0.3 ? 'bg-rose-500' : 'bg-gradient-to-r from-brand-500 to-pink-500');
      el('tl').textContent = Math.ceil(left) + ' dtk';
      if (left <= 0) finish(pickedMain, -1);
    }, 100);
  }

  function finish(m, k) {
    if (locked) return;
    locked = true; clearInterval(timer);
    var t = Math.min((performance.now() - t0) / 1000, limit);
    answers.push({ id: cur.id, m: m, k: k, t: Math.round(t * 100) / 100 });
    idx++;
    showQuestion();
  }

  function send() {
    playing = false;
    show('card', false); show('sending', true); show('retry', false);
    fetch(SUBMIT, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ level: level, answers: answers }) })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) { location.href = j.url; })
      .catch(function () { show('retry', true, 'inline-flex'); });
  }
  el('retry').addEventListener('click', send);

  document.addEventListener('keydown', function (e) {
    if (!playing || locked) return;
    var n = parseInt(e.key, 10), btns = el('op').children;
    if (n >= 1 && n <= btns.length) btns[n - 1].click();
  });
  window.addEventListener('beforeunload', function (e) {
    if (playing) { e.preventDefault(); e.returnValue = ''; }
  });
})();
</script>
{% endblock %}
'''

TEMPLATES["result.html"] = r'''{% extends "base.html" %}
{% block title %}Hasil {{ label }}{% endblock %}
{% block content %}
<div class="card fade-up overflow-hidden text-center">
  <div class="h-1.5 bg-gradient-to-r from-brand-500 via-fuchsia-500 to-pink-500"></div>
  <div class="p-6 sm:p-8">
    <p class="text-xs font-bold uppercase tracking-[.25em] text-brand-500">{{ label }}</p>
    <p class="mt-1 text-sm text-slate-500">{{ a['name'] }} · {{ divmap.get(a['div_id'], '-') }}</p>

    <div class="relative mx-auto mt-5 h-44 w-44">
      <svg viewBox="0 0 120 120" class="h-full w-full -rotate-90">
        <defs><linearGradient id="rg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7c3aed"/><stop offset="1" stop-color="#ec4899"/></linearGradient></defs>
        <circle cx="60" cy="60" r="52" fill="none" stroke="#ede9fe" stroke-width="10"/>
        <circle class="ring-fill" cx="60" cy="60" r="52" fill="none" stroke="{{ 'url(#rg)' if passed else '#f43f5e' }}" stroke-width="10" stroke-linecap="round" stroke-dasharray="326.7" stroke-dashoffset="{{ (326.7 * (1 - a['score'] / 100))|round(1) }}"/>
      </svg>
      <div class="absolute inset-0 grid place-items-center">
        <p class="text-5xl font-extrabold {{ 'text-grad' if passed else 'text-rose-600' }}"><span data-count="{{ a['score'] }}">{{ a['score'] }}</span><span class="text-2xl">%</span></p>
      </div>
    </div>

    <span class="pill mt-4 !px-5 !py-1.5 text-sm {{ 'bg-emerald-100 text-emerald-700' if passed else 'bg-rose-100 text-rose-700' }}">
      {{ 'LULUS' if passed else 'BELUM LULUS' }}
    </span>
    <p class="mt-3 text-xs text-slate-400">Syarat lulus: skor ≥ {{ PASS }}% dan tidak ada NG yang lolos.</p>

    {% if need and not session.get('admin') %}
    <div class="mx-auto mt-4 max-w-md rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
      Kamu perlu mengikuti <b>Post Test Remedial</b>. Buka materi lagi, lalu kerjakan ulang. Nilai sebelumnya tetap tersimpan sebagai pembanding.
    </div>
    {% endif %}

    <div class="mt-6 grid grid-cols-2 gap-3 text-left sm:grid-cols-3">
      {% set stats = [
        ('Benar', a['correct'] ~ ' / ' ~ a['total'], ''),
        ('Rata-rata waktu', (a['avgt'] or 0) ~ ' dtk', ''),
        ('Level', levels[a['level']][0] if a['level'] in levels else '-', ''),
        ('NG lolos', a['esc'] or 0, 'text-rose-600' if (a['esc'] or 0) else ''),
        ('OK dianggap NG', a['rej'] or 0, 'text-amber-600' if (a['rej'] or 0) else ''),
        ('Jenis NG salah', a['kw'] or 0, 'text-amber-600' if (a['kw'] or 0) else '')] %}
      {% for t, v, c in stats %}
      <div class="fade-up rounded-xl bg-gradient-to-br from-brand-50 to-pink-50 p-3 ring-1 ring-brand-100" style="--d:{{ loop.index0 * 0.06 + 0.3 }}s">
        <p class="text-xs text-slate-500">{{ t }}</p>
        <p class="text-lg font-bold {{ c }}">{{ v }}</p>
      </div>
      {% endfor %}
    </div>

    <div class="mt-6 flex flex-wrap justify-center gap-3">
      {% if session.get('admin') %}<a href="/admin" class="btn btn-ghost">← Dashboard</a>{% endif %}
      {% if a['phase'] == 'pre' %}<a href="/materi" class="btn btn-primary !px-6 !py-3">Lanjut ke Materi →</a>
      {% elif need %}<a href="/materi" class="btn btn-primary !px-6 !py-3">Buka Materi &amp; Ikut Remedial →</a>
      {% else %}<a href="/" class="btn btn-primary !px-6 !py-3">Kembali ke Beranda</a>{% endif %}
    </div>
  </div>
</div>

{% if cc %}{% include "_concl.html" %}{% endif %}

{% if review %}
<h2 class="mb-3 mt-8 text-lg font-bold">Pembahasan</h2>
<div class="space-y-3">
  {% for d in det %}
  <div class="card fade-up overflow-hidden {{ 'border-emerald-200' if d.good else 'border-rose-200' }}" style="--d:{{ [loop.index0 * 0.05, 0.6]|min }}s">
    <div class="flex items-center justify-between px-4 py-2 text-xs font-semibold {{ 'bg-emerald-50 text-emerald-700' if d.good else 'bg-rose-50 text-rose-700' }}">
      <span>Soal {{ loop.index }} · {{ 'Benar' if d.good else 'Salah' }}</span><span>{{ d.t }} dtk</span>
    </div>
    <div class="flex flex-col gap-4 p-4 sm:flex-row">
      {% if d.image %}<img src="/file/{{ d.image }}" class="h-32 w-full rounded-xl bg-slate-100 object-contain sm:w-44" alt="">{% endif %}
      <div class="min-w-0 flex-1 space-y-1.5 text-sm">
        <p class="font-medium">{{ d.text }}</p>
        <p><span class="text-slate-400">Jawabanmu:</span> <span class="font-medium {{ 'text-emerald-700' if d.good else 'text-rose-700' }}">{{ d.yours }}</span></p>
        {% if not d.good %}<p><span class="text-slate-400">Kunci:</span> <span class="font-medium text-emerald-700">{{ d.key }}</span></p>{% endif %}
        {% if d.explain or d.explain_img %}<div class="space-y-2 rounded-lg bg-brand-50 p-2.5 text-slate-600">{% if d.explain %}<p>{{ d.explain }}</p>{% endif %}{% if d.explain_img %}<img src="/file/{{ d.explain_img }}" class="max-h-56 w-full rounded-lg border border-brand-100 bg-white object-contain" alt="">{% endif %}</div>{% endif %}
      </div>
    </div>
  </div>
  {% endfor %}
</div>
{% endif %}

{% if passed %}
<script>
(function () {
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var colors = ['#7c3aed', '#a78bfa', '#ec4899', '#f9a8d4', '#d946ef', '#fbbf24'];
  for (var i = 0; i < 46; i++) {
    var d = document.createElement('i');
    d.className = 'confetti';
    d.style.left = (Math.random() * 100) + 'vw';
    d.style.background = colors[i % colors.length];
    d.style.animationDuration = (2.2 + Math.random() * 2.2) + 's';
    d.style.animationDelay = (Math.random() * 0.8) + 's';
    document.body.appendChild(d);
    (function (x) { setTimeout(function () { x.remove(); }, 6500); })(d);
  }
})();
</script>
{% endif %}
{% endblock %}
'''

TEMPLATES["_concl.html"] = r'''<div class="card fade-up mt-6 overflow-hidden" style="--d:.25s">
  <div class="px-6 py-4 text-white {{ 'bg-gradient-to-r from-emerald-500 to-teal-500' if cc.verdict == 'naik' else ('bg-gradient-to-r from-amber-400 to-orange-400' if cc.verdict == 'tetap' else 'bg-gradient-to-r from-rose-500 to-pink-500') }}">
    <p class="text-[11px] font-bold uppercase tracking-[.25em] opacity-80">Kesimpulan Training</p>
    <h3 class="mt-1 text-xl font-extrabold">{{ cc.title }}</h3>
  </div>
  <div class="p-6">
    <div class="overflow-x-auto">
      <div class="grid min-w-[300px] items-start gap-3 text-center" style="grid-template-columns:repeat({{ cc.steps|length }},minmax(0,1fr))">
        {% for s in cc.steps %}
        <div>
          <p class="text-[11px] font-semibold uppercase leading-tight tracking-wide {{ 'text-slate-400' if s.kind == 'pre' else ('text-brand-500' if s.kind == 'post' else 'text-emerald-600') }}">{{ s.label }}</p>
          <p class="mt-1 text-2xl font-extrabold sm:text-4xl {{ 'text-slate-500' if s.kind == 'pre' else ('text-brand-700' if s.kind == 'post' else 'text-emerald-600') }}"><span data-count="{{ s.score }}">{{ s.score }}</span>%</p>
          {% if s.delta is not none %}
          <span class="pill mt-1 {{ 'bg-emerald-100 text-emerald-700' if s.delta > 0 else ('bg-rose-100 text-rose-700' if s.delta < 0 else 'bg-amber-100 text-amber-700') }}">{{ ('▲ +' ~ s.delta) if s.delta > 0 else (('▼ ' ~ s.delta) if s.delta < 0 else '＝ 0') }}</span>
          {% endif %}
        </div>
        {% endfor %}
      </div>
    </div>

    <div class="mt-5 space-y-3">
      {% for s in cc.steps %}
      <div>
        <div class="mb-1 flex justify-between text-xs text-slate-500"><span>{{ s.label }}</span><span>{{ s.score }}%</span></div>
        <div class="h-2.5 overflow-hidden rounded-full bg-slate-100"><div class="bar-grow h-full rounded-full {{ 'bg-brand-300' if s.kind == 'pre' else ('bg-gradient-to-r from-brand-600 to-pink-500' if s.kind == 'post' else 'bg-gradient-to-r from-emerald-500 to-teal-400') }}" style="width:{{ s.score }}%;--d:{{ loop.index0 * 0.25 + 0.2 }}s"></div></div>
      </div>
      {% endfor %}
    </div>

    <p class="mt-5 text-sm leading-relaxed text-slate-600">{{ cc.text }}</p>
    <ul class="mt-3 space-y-1.5 text-sm text-slate-600">
      {% for n in cc.notes %}<li class="flex gap-2"><span class="text-pink-500">●</span><span>{{ n }}</span></li>{% endfor %}
    </ul>
  </div>
</div>
'''

TEMPLATES["login.html"] = r'''{% extends "base.html" %}
{% block title %}Login Admin{% endblock %}
{% block content %}
<div class="mx-auto max-w-sm pt-8">
  <div class="fade-up mb-6 text-center">
    {% if logo_url %}<img src="{{ logo_url }}" alt="Logo" class="mx-auto mb-4 h-20 w-auto object-contain">
    {% else %}<div class="mx-auto mb-4 grid h-16 w-16 place-items-center rounded-2xl bg-gradient-to-br from-brand-600 to-pink-500 text-white shadow-xl shadow-brand-500/30">
      <svg class="h-8 w-8" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 1 0-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 0 0 2.25-2.25v-6.75a2.25 2.25 0 0 0-2.25-2.25H6.75a2.25 2.25 0 0 0-2.25 2.25v6.75a2.25 2.25 0 0 0 2.25 2.25Z"/></svg>
    </div>{% endif %}
    <h1 class="text-2xl font-extrabold">Login <span class="text-grad">Admin</span></h1>
  </div>
  <form method="post" class="card fade-up space-y-4 p-6" style="--d:.12s">
    <div>
      <label class="label">Password</label>
      <input type="password" name="password" required autofocus class="input">
    </div>
    <button class="btn btn-primary w-full !py-3">Masuk</button>
  </form>
</div>
{% endblock %}
'''

TEMPLATES["admin_dash.html"] = r'''{% extends "base.html" %}
{% block title %}Dashboard{% endblock %}
{% block awidth %}max-w-7xl{% endblock %}
{% block content %}
{% macro sect(title, id, hint='') %}
<div id="{{ id }}" class="mb-3 mt-10 flex scroll-mt-28 items-center gap-3">
  <h2 class="whitespace-nowrap text-sm font-bold uppercase tracking-[.18em] text-slate-500">{{ title }}</h2>
  <span class="h-px flex-1 bg-brand-100"></span>
  {% if hint %}<span class="hidden text-xs text-slate-400 sm:block">{{ hint }}</span>{% endif %}
</div>
{% endmacro %}
{% set pre = st.get('pre') %}{% set post = st.get('post') %}{% set rm = st.get('remed') %}

<div class="flex flex-wrap items-end justify-between gap-3">
  <div>
    <h1 class="text-2xl font-extrabold tracking-tight">Dashboard</h1>
    <p class="mt-0.5 text-sm text-slate-500">
      {% if f.df or f.dt %}{{ f.df or '…' }} s/d {{ f.dt or '…' }}{% else %}Semua waktu{% endif %}
      · {{ divmap.get(f.dv|int, 'Semua divisi') if f.dv else 'Semua divisi' }}
    </p>
  </div>
  <div class="flex flex-wrap gap-2">
    <a href="/admin/backup.db" class="btn btn-ghost">Backup database</a>
    <a href="/admin/riwayat.xlsx?{{ qs }}" class="btn btn-primary">Unduh Excel</a>
  </div>
</div>
<nav class="mt-4 flex flex-wrap gap-2 text-xs font-semibold">
  {% for href, t in [('#ringkasan','Ringkasan'),('#progres','Grafik progres'),('#tren','Tren peserta'),('#daftar','Daftar progres'),('#riwayat','Riwayat tes')] %}
  <a href="{{ href }}" class="rounded-full bg-white px-3 py-1.5 text-brand-700 ring-1 ring-brand-100 transition hover:bg-brand-50">{{ t }}</a>
  {% endfor %}
</nav>

<form method="get" class="card fade-up mt-5 space-y-3 p-5">
  <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
    <div class="lg:col-span-2"><label class="label">Divisi</label>
      <select name="div" class="input"><option value="">Semua divisi</option>
        {% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if f.dv == d.id|string }}>{{ d.name }}</option>{% endfor %}</select></div>
    <div><label class="label">Dari tanggal</label><input type="date" name="from" value="{{ f.df }}" class="input"></div>
    <div><label class="label">Sampai tanggal</label><input type="date" name="to" value="{{ f.dt }}" class="input"></div>
    <div><label class="label">Tipe tes</label>
      <select name="phase" class="input"><option value="">Semua tipe</option>
        {% for k, v in PHASE.items() %}<option value="{{ k }}" {{ 'selected' if f.ph == k }}>{{ v }}</option>{% endfor %}</select></div>
    <div><label class="label">Nama</label><input name="q" value="{{ f.s }}" placeholder="Cari nama…" class="input"></div>
  </div>
  <div class="flex flex-wrap items-center gap-2">
    <button class="btn btn-primary">Terapkan filter</button>
    <a href="/admin" class="btn btn-ghost">Reset</a>
    <span class="ml-2 text-xs text-slate-400">Cepat:</span>
    {% for p in presets %}<a href="{{ p.href }}" class="pill {{ 'bg-gradient-to-r from-brand-600 to-pink-500 text-white' if p.on else 'bg-brand-50 text-brand-700 hover:bg-brand-100' }}">{{ p.label }}</a>{% endfor %}
  </div>
  <p class="text-xs text-slate-400">Filter berlaku untuk kartu, grafik, daftar progres, tabel riwayat, dan file Excel.</p>
</form>

<p id="chartwarn" style="display:none" class="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">Grafik gagal dimuat (Chart.js tidak terbaca). Pastikan ada internet, atau simpan https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js sebagai static/chart.js lalu muat ulang.</p>

{{ sect('Ringkasan', 'ringkasan', 'Sesuai filter di atas') }}
<div class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
  {% set cards = [
    ('Total percobaan', total, '', 'from-brand-500 to-fuchsia-500'),
    ('Peserta', people, '', 'from-fuchsia-500 to-pink-500'),
    ('Rata-rata Pre', (pre.m|int) if pre else none, '%', 'from-sky-400 to-brand-500'),
    ('Rata-rata Post', (post.m|int) if post else none, '%', 'from-pink-500 to-rose-400'),
    ('Rata-rata Remedial', (rm.m|int) if rm else none, '%', 'from-emerald-400 to-teal-500')] %}
  {% for t, v, suf, g in cards %}
  <div class="card fade-up relative overflow-hidden p-5 {{ 'col-span-2 md:col-span-1' if loop.last else '' }}" style="--d:{{ loop.index0 * 0.06 }}s">
    <div class="absolute -right-6 -top-6 h-24 w-24 rounded-full bg-gradient-to-br {{ g }} opacity-25"></div>
    <p class="text-xs font-semibold uppercase tracking-wide text-slate-400">{{ t }}</p>
    <p class="mt-1 text-3xl font-extrabold">{% if v is none %}-{% else %}<span data-count="{{ v }}">{{ v }}</span>{{ suf }}{% endif %}</p>
  </div>
  {% endfor %}
</div>

{{ sect('Grafik progres', 'progres', 'Pre · Post · Remedial') }}
<div class="grid gap-4 lg:grid-cols-12">
  <div class="card fade-up p-6 lg:col-span-4">
    <h3 class="font-bold">Ringkasan progres</h3>
    <p class="text-xs text-slate-400">Nilai akhir (Post atau Remedial terakhir) dibanding Pre</p>
    <div class="mt-5 flex flex-wrap items-center justify-center gap-6 sm:justify-start">
      <div class="donut relative h-36 w-36 shrink-0 rounded-full" style="background: {{ donut }}">
        <div class="absolute inset-4 grid place-items-center rounded-full bg-white text-center">
          <div><p class="text-3xl font-extrabold"><span data-count="{{ cnt.total }}">{{ cnt.total }}</span></p><p class="text-[11px] text-slate-400">peserta</p></div>
        </div>
      </div>
      <ul class="space-y-2 text-sm">
        <li class="flex items-center gap-2"><span class="h-3 w-3 rounded-full bg-emerald-500"></span>Naik <b>{{ cnt.naik }}</b></li>
        <li class="flex items-center gap-2"><span class="h-3 w-3 rounded-full bg-amber-400"></span>Tetap <b>{{ cnt.tetap }}</b></li>
        <li class="flex items-center gap-2"><span class="h-3 w-3 rounded-full bg-rose-500"></span>Turun <b>{{ cnt.turun }}</b></li>
        <li class="flex items-center gap-2 text-slate-400"><span class="h-3 w-3 rounded-full bg-slate-200"></span>Belum Post <b>{{ cnt.belum }}</b></li>
        <li class="flex items-center gap-2 text-emerald-700"><span class="h-3 w-3 rounded-full bg-teal-400"></span>Ikut remedial <b>{{ cnt.remed }}</b></li>
      </ul>
    </div>
    <div class="mt-6 grid grid-cols-3 gap-2 text-center">
      <div class="rounded-xl bg-brand-50 p-2.5"><p class="text-[11px] text-slate-500">Rata2 Pre</p><p class="font-bold">{{ (cnt.avg_pre ~ '%') if cnt.avg_pre is not none else '-' }}</p></div>
      <div class="rounded-xl bg-pink-50 p-2.5"><p class="text-[11px] text-slate-500">Rata2 Akhir</p><p class="font-bold">{{ (cnt.avg_post ~ '%') if cnt.avg_post is not none else '-' }}</p></div>
      <div class="rounded-xl bg-gradient-to-br from-brand-50 to-pink-50 p-2.5"><p class="text-[11px] text-slate-500">Rata2 selisih</p>
        <p class="font-bold {{ 'text-emerald-600' if (cnt.avg_diff or 0) > 0 else ('text-rose-600' if (cnt.avg_diff or 0) < 0 else '') }}">{% if cnt.avg_diff is none %}-{% else %}{{ '+' if cnt.avg_diff > 0 }}{{ cnt.avg_diff }}{% endif %}</p></div>
    </div>
  </div>

  <div class="card fade-up p-6 lg:col-span-8">
    <h3 class="font-bold">Progres per peserta</h3>
    <p class="text-xs text-slate-400">Kolom = nilai Pre, Post, Remedial. Garis = selisih nilai akhir dan Pre (sumbu kanan). Titik hijau naik, merah turun, kuning tetap.</p>
    {% if prog %}
    <div class="mt-4 overflow-x-auto"><div id="wrapA" style="height:360px;position:relative"><canvas id="chA"></canvas></div></div>
    {% else %}<p class="py-16 text-center text-sm text-slate-400">Belum ada data pada filter ini.</p>{% endif %}
  </div>
</div>

{{ sect('Tren peserta', 'tren', 'Dari waktu ke waktu') }}
<div class="card fade-up p-6">
  <div class="flex flex-wrap items-end justify-between gap-3">
    <div>
      <h3 class="font-bold">Tren nilai satu peserta</h3>
      <p class="text-xs text-slate-400">Setiap tes pada rentang waktu terpilih: Pre, Post, lalu Remedial. Atur rentang di filter.</p>
    </div>
    <div class="min-w-[240px]"><label class="label">Pilih peserta</label><select id="tlSel" class="input"></select></div>
  </div>
  <p id="tlSum" class="mt-3 text-sm text-slate-600"></p>
  <div class="mt-3 overflow-x-auto"><div id="wrapB" style="height:340px;position:relative"><canvas id="chB"></canvas></div></div>
  <p id="tlEmpty" style="display:none" class="py-12 text-center text-sm text-slate-400">Belum ada tes pada filter ini.</p>
</div>

{{ sect('Daftar progres', 'daftar', 'Klik untuk membuka') }}
<details class="card fade-up p-4">
  <summary class="cursor-pointer font-semibold">Daftar progres per peserta ({{ prog|length }})</summary>
  <div class="mt-4 grid max-h-[520px] gap-2.5 overflow-y-auto pr-1 xl:grid-cols-2">
    {% for r in prog %}
    <div class="flex flex-col gap-3 rounded-2xl border-l-4 bg-white/70 p-3.5 ring-1 ring-brand-50
         {{ 'border-emerald-400' if r.status == 'naik' else ('border-rose-400' if r.status == 'turun' else ('border-amber-300' if r.status == 'tetap' else 'border-slate-200')) }}">
      <div class="flex items-start justify-between gap-2">
        <div class="min-w-0"><p class="truncate text-sm font-semibold">{{ r.name }}</p><p class="text-xs text-slate-400">NIK {{ r.nik }} · {{ r.div }}</p></div>
        <div class="shrink-0 text-right">
          {% if r.status == 'naik' %}<span class="pill bg-emerald-100 text-emerald-700">▲ +{{ r.diff }} naik</span>
          {% elif r.status == 'turun' %}<span class="pill bg-rose-100 text-rose-700">▼ {{ r.diff }} turun</span>
          {% elif r.status == 'tetap' %}<span class="pill bg-amber-100 text-amber-700">＝ tetap</span>
          {% else %}<span class="pill bg-slate-100 text-slate-500">Belum Post</span>{% endif %}
          {% if r.nrem %}<p class="mt-1 text-[11px] text-slate-400">remedial {{ r.nrem }}×</p>{% endif %}
        </div>
      </div>
      <div class="space-y-1.5">
        <div class="flex items-center gap-2"><span class="w-10 text-[11px] text-slate-400">Pre</span>
          <div class="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100"><div class="h-full rounded-full bg-brand-300" style="width:{{ r.pre or 0 }}%"></div></div>
          <span class="w-10 text-right text-xs font-semibold text-slate-500">{{ (r.pre ~ '%') if r.pre is not none else '-' }}</span></div>
        <div class="flex items-center gap-2"><span class="w-10 text-[11px] text-slate-400">Post</span>
          <div class="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100"><div class="h-full rounded-full bg-gradient-to-r from-brand-600 to-pink-500" style="width:{{ r.post or 0 }}%"></div></div>
          <span class="w-10 text-right text-xs font-semibold">{{ (r.post ~ '%') if r.post is not none else '-' }}</span></div>
        {% if r.remed is not none %}
        <div class="flex items-center gap-2"><span class="w-10 text-[11px] text-emerald-600">Remed</span>
          <div class="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100"><div class="h-full rounded-full bg-gradient-to-r from-emerald-500 to-teal-400" style="width:{{ r.remed }}%"></div></div>
          <span class="w-10 text-right text-xs font-semibold text-emerald-700">{{ r.remed }}%</span></div>
        {% endif %}
      </div>
      {% if r.rnik %}
      <form method="post" action="/admin/reset" onsubmit="return confirm('Reset siklus peserta ini? Peserta bisa mengulang dari Pre Test. Riwayat lama tetap tersimpan.')">
        <input type="hidden" name="nik" value="{{ r.rnik }}"><input type="hidden" name="div_id" value="{{ r.div_id if r.div_id is not none else '' }}">
        <button class="text-[11px] text-slate-400 underline hover:text-rose-600">Reset siklus peserta ini</button></form>
      {% endif %}
    </div>
    {% else %}
    <p class="py-10 text-center text-sm text-slate-400 xl:col-span-2">Belum ada data untuk ditampilkan.</p>
    {% endfor %}
  </div>
</details>

{{ sect('Riwayat tes', 'riwayat', pager.total ~ ' data') }}
<div class="mb-3 flex flex-wrap items-center justify-between gap-2">
  <p class="text-sm text-slate-500">Tes yang sudah dikerjakan peserta. Riwayat yang dihapus masuk <a href="/admin/sampah" class="font-semibold text-brand-600 underline">🗑️ Sampah ({{ trash_count }})</a> dan bisa dipulihkan selama 31 hari. Menghapus riwayat juga mengubah progres peserta.</p>
  <div class="flex flex-wrap gap-2">
    {% if filtered and pager.total %}
    <form method="post" action="/admin/riwayat/hapus-semua" onsubmit="return confirm('Hapus {{ pager.total }} riwayat tes hasil filter ini? Tidak bisa dibatalkan.')">
      <input type="hidden" name="mode" value="filter"><input type="hidden" name="next" value="{{ nxt }}">
      <input type="hidden" name="div" value="{{ f.dv }}"><input type="hidden" name="phase" value="{{ f.ph }}"><input type="hidden" name="q" value="{{ f.s }}">
      <input type="hidden" name="from" value="{{ f.df }}"><input type="hidden" name="to" value="{{ f.dt }}">
      <button class="btn btn-danger !py-2">Hapus hasil filter ({{ pager.total }})</button>
    </form>
    {% endif %}
    {% if all_count %}
    <form method="post" action="/admin/riwayat/hapus-semua" onsubmit="var v = prompt('Ini menghapus SEMUA {{ all_count }} riwayat tes (semua divisi dan tanggal). Ketik HAPUS untuk melanjutkan:'); if (v !== 'HAPUS') return false; this.ok.value = v; return true;">
      <input type="hidden" name="mode" value="all"><input type="hidden" name="ok" value=""><input type="hidden" name="next" value="/admin">
      <button class="btn btn-danger !py-2">Hapus SEMUA riwayat ({{ all_count }})</button>
    </form>
    {% endif %}
  </div>
</div>

<div class="card overflow-hidden">
  <div class="overflow-x-auto">
  <table class="min-w-full divide-y divide-brand-50">
    <thead class="bg-brand-50/60"><tr>
      <th class="th">Tanggal</th><th class="th">Nama</th><th class="th">Divisi</th><th class="th">Tipe</th><th class="th">Level</th>
      <th class="th">Skor</th><th class="th">NG lolos</th><th class="th">Status</th><th class="th text-right">Aksi</th>
    </tr></thead>
    <tbody class="divide-y divide-brand-50">
      {% for r in rows %}
      {% set ok = r['score'] >= PASS and (r['esc'] or 0) == 0 %}
      <tr class="transition hover:bg-brand-50/50">
        <td class="td whitespace-nowrap text-slate-500">{{ r['created'] }}</td>
        <td class="td font-medium">{{ r['name'] }}<span class="block text-xs font-normal text-slate-400">{{ r['nik'] }}</span></td>
        <td class="td">{{ divmap.get(r['div_id'], '-') }}</td>
        <td class="td whitespace-nowrap">{{ PHASE[r['phase']] }}</td>
        <td class="td">{{ levels[r['level']][0] if r['level'] in levels else '-' }}</td>
        <td class="td font-semibold">{{ r['score'] }}%</td>
        <td class="td {{ 'font-semibold text-rose-600' if (r['esc'] or 0) }}">{{ r['esc'] or 0 }}</td>
        <td class="td"><span class="pill {{ 'bg-emerald-100 text-emerald-700' if ok else 'bg-rose-100 text-rose-700' }}">{{ 'Lulus' if ok else 'Belum' }}</span></td>
        <td class="td whitespace-nowrap text-right">
          <a href="/hasil/{{ r['id'] }}" class="font-semibold text-brand-600 hover:underline">Detail</a>
          <form method="post" action="/admin/riwayat/hapus/{{ r['id'] }}" class="ml-3 inline" onsubmit="return confirm('Hapus riwayat tes ini?')">
            <input type="hidden" name="next" value="{{ nxt }}"><button class="font-semibold text-rose-600 hover:underline">Hapus</button></form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="9" class="p-8 text-center text-slate-400">Belum ada data.</td></tr>
      {% endfor %}
    </tbody>
  </table>
  </div>
  {% if pager.total %}
  <div class="flex flex-wrap items-center justify-between gap-2 border-t border-brand-50 px-4 py-3 text-sm">
    <span class="text-slate-500">Menampilkan {{ pager.first }}–{{ pager.last }} dari {{ pager.total }}</span>
    <div class="flex items-center gap-1">
      {% if pager.prev %}<a href="{{ pager.prev }}" class="btn btn-ghost !py-1.5">←</a>{% endif %}
      {% for n, href in pager.nums %}<a href="{{ href }}" class="pill !px-3 !py-1.5 {{ 'bg-gradient-to-r from-brand-600 to-pink-500 text-white' if n == pager.page else 'bg-brand-50 text-brand-700 hover:bg-brand-100' }}">{{ n }}</a>{% endfor %}
      {% if pager.next %}<a href="{{ pager.next }}" class="btn btn-ghost !py-1.5">→</a>{% endif %}
    </div>
  </div>
  {% endif %}
</div>

<script src="{% if local_chart %}{{ url_for('static', filename='chart.js') }}{% else %}https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js{% endif %}"></script>
<script>
(function () {
  var A = {{ chart|tojson }}, TL = {{ tl|tojson }};
  if (typeof Chart === 'undefined') { document.getElementById('chartwarn').style.display = 'block'; return; }
  var C = {pre: '#c4b5fd', post: '#8b5cf6', remed: '#10b981', up: '#10b981', down: '#f43f5e', same: '#f59e0b', none: '#94a3b8'};
  Chart.defaults.font.family = 'ui-sans-serif, system-ui, "Segoe UI", sans-serif';

  /* ---- Grafik A: kolom (Pre/Post/Remedial) + garis (selisih), semua peserta ---- */
  if (A.labels.length) {
    document.getElementById('wrapA').style.minWidth = Math.max(320, A.labels.length * 80) + 'px';
    var dot = A.status.map(function (s) { return s === 'naik' ? C.up : s === 'turun' ? C.down : s === 'tetap' ? C.same : C.none; });
    new Chart(document.getElementById('chA'), {
      type: 'bar',
      data: {labels: A.labels, datasets: [
        {type: 'bar', label: 'Pre', data: A.pre, backgroundColor: C.pre, borderRadius: 4, yAxisID: 'y', order: 2},
        {type: 'bar', label: 'Post (sebelum remedial)', data: A.post, backgroundColor: C.post, borderRadius: 4, yAxisID: 'y', order: 2},
        {type: 'bar', label: 'Remedial', data: A.remed, backgroundColor: C.remed, borderRadius: 4, yAxisID: 'y', order: 2},
        {type: 'line', label: 'Selisih akhir − Pre (poin)', data: A.diff, yAxisID: 'y1', borderColor: '#334155', borderWidth: 2,
         tension: .25, pointRadius: 6, pointBackgroundColor: dot, pointBorderColor: '#fff', pointBorderWidth: 2, order: 1}
      ]},
      options: {responsive: true, maintainAspectRatio: false, interaction: {mode: 'index', intersect: false},
        scales: {
          y: {min: 0, max: 100, title: {display: true, text: 'Nilai (%)'}},
          y1: {position: 'right', min: -100, max: 100, grid: {drawOnChartArea: false}, title: {display: true, text: 'Selisih (poin)'}}
        },
        plugins: {legend: {position: 'bottom'}}}
    });
  }

  /* ---- Grafik B: tren satu peserta dari waktu ke waktu ---- */
  var sel = document.getElementById('tlSel'), sum = document.getElementById('tlSum'), chB = null;
  if (!TL.length) {
    document.getElementById('wrapB').style.display = 'none';
    document.getElementById('tlEmpty').style.display = 'block';
    sel.disabled = true;
    return;
  }
  TL.forEach(function (d, i) {
    var o = document.createElement('option');
    o.value = i; o.textContent = d.name + ' · ' + d.div + ' (' + d.pts.length + ' tes)';
    sel.appendChild(o);
  });
  function drawB(i) {
    var d = TL[i], pts = d.pts;
    function only(k) { return pts.map(function (p) { return p.p === k ? p.s : null; }); }
    var scores = pts.map(function (p) { return p.s; });
    var dots = pts.map(function (p, j) {
      if (!j) return '#64748b';
      var x = p.s - pts[j - 1].s;
      return x > 0 ? C.up : x < 0 ? C.down : C.same;
    });
    document.getElementById('wrapB').style.minWidth = Math.max(320, pts.length * 100) + 'px';
    if (chB) chB.destroy();
    chB = new Chart(document.getElementById('chB'), {
      type: 'bar',
      data: {labels: pts.map(function (p) { return p.l; }), datasets: [
        {type: 'bar', label: 'Pre', data: only('pre'), backgroundColor: C.pre, borderRadius: 6, grouped: false, maxBarThickness: 56, order: 2},
        {type: 'bar', label: 'Post', data: only('post'), backgroundColor: C.post, borderRadius: 6, grouped: false, maxBarThickness: 56, order: 2},
        {type: 'bar', label: 'Remedial', data: only('remed'), backgroundColor: C.remed, borderRadius: 6, grouped: false, maxBarThickness: 56, order: 2},
        {type: 'line', label: 'Tren nilai', data: scores, borderColor: '#334155', borderWidth: 2, tension: .15, pointRadius: 7,
         pointBackgroundColor: dots, pointBorderColor: '#fff', pointBorderWidth: 2, order: 1,
         segment: {borderColor: function (c) { var a = c.p0.parsed.y, b = c.p1.parsed.y; return b > a ? C.up : b < a ? C.down : C.same; }}}
      ]},
      options: {responsive: true, maintainAspectRatio: false,
        scales: {y: {min: 0, max: 100, title: {display: true, text: 'Nilai (%)'}}},
        plugins: {legend: {position: 'bottom'},
          tooltip: {callbacks: {title: function (it) { var p = pts[it[0].dataIndex]; return p.k + ' · ' + p.d; }}}}}
    });
    var diff = scores[scores.length - 1] - scores[0], badge = '';
    if (scores.length > 1) {
      badge = diff > 0 ? '<span class="pill bg-emerald-100 text-emerald-700">▲ naik +' + diff + ' poin</span>'
            : diff < 0 ? '<span class="pill bg-rose-100 text-rose-700">▼ turun ' + diff + ' poin</span>'
            : '<span class="pill bg-amber-100 text-amber-700">＝ tetap</span>';
    }
    sum.innerHTML = pts.map(function (p) { return p.k + ' <b>' + p.s + '%</b>'; }).join(' → ') + ' &nbsp; ' + badge;
  }
  sel.addEventListener('change', function () { drawB(sel.value); });
  drawB(0);
})();
</script>
{% endblock %}
'''

TEMPLATES["admin_divs.html"] = r'''{% extends "base.html" %}
{% block title %}Divisi{% endblock %}
{% block awidth %}max-w-4xl{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold">Divisi Quality <span class="text-base font-normal text-slate-400">({{ rows|length }})</span></h1>
<p class="mt-1 text-sm text-slate-500">Peserta hanya bisa memilih divisi yang <b>aktif</b> dan sesuai jadwal. Soal & materi tanpa divisi berlaku untuk semua divisi.</p>

<form method="post" class="card mt-6 flex flex-wrap items-end gap-3 p-5">
  <div class="min-w-[200px] flex-1"><label class="label">Nama divisi baru</label><input name="name" required class="input" placeholder="Contoh: Quality Sewing"></div>
  <button class="btn btn-primary">Tambah divisi</button>
</form>

<div class="mt-6 space-y-3">
  {% for d in rows %}
  <div class="card flex flex-wrap items-center gap-4 p-4">
    <div class="min-w-0 flex-1">
      <form method="post" action="/admin/divisi/rename/{{ d.id }}" class="flex gap-1"><input name="name" value="{{ d.name }}" class="input !py-1.5 font-semibold"><button class="btn btn-ghost !py-1.5 !text-xs">Ubah nama</button></form>
      <p class="mt-1 text-xs text-slate-500">Soal Pre {{ d.pre }} · Post {{ d.post }} · Remedial {{ d.remed }} · Materi {{ d.mats }} · Jadwal {{ d.scheds }}</p>
    </div>
    <span class="pill {{ 'bg-emerald-100 text-emerald-700' if d.active else 'bg-slate-100 text-slate-500' }}">{{ 'Aktif' if d.active else 'Nonaktif' }}</span>
    <form method="post" action="/admin/divisi/aktif/{{ d.id }}"><button class="btn {{ 'btn-ghost' if d.active else 'btn-primary' }} !py-1.5">{{ 'Nonaktifkan' if d.active else 'Aktifkan' }}</button></form>
    <form method="post" action="/admin/divisi/hapus/{{ d.id }}" onsubmit="return confirm('Hapus divisi ini?')"><button class="btn btn-danger !py-1.5">Hapus</button></form>
  </div>
  {% else %}
  <div class="card p-8 text-center text-slate-400">Belum ada divisi.</div>
  {% endfor %}
</div>
<p class="mt-4 text-xs text-slate-400">Soal tanpa divisi (berlaku semua divisi): Pre {{ free.get('pre', 0) }} · Post {{ free.get('post', 0) }} · Remedial {{ free.get('remed', 0) }}</p>
{% endblock %}
'''

TEMPLATES["admin_sched.html"] = r'''{% extends "base.html" %}
{% block title %}Jadwal{% endblock %}
{% block awidth %}max-w-5xl{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold">Jadwal Training</h1>
<p class="mt-1 text-sm text-slate-500">Peserta hanya bisa masuk saat jadwalnya aktif. Satu peserta boleh punya beberapa jadwal. Peserta yang sudah menyelesaikan training bisa mengerjakannya lagi saat ada <b>jadwal baru</b> yang aktif (jadwal yang dimulai atau diinput setelah ia selesai).</p>
{% if nosched %}<p class="mt-3 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">⚠️ {{ nosched }} peserta belum punya jadwal aktif atau yang akan datang, sehingga belum bisa masuk.</p>{% endif %}

<form method="post" class="card mt-6 space-y-4 p-5">
  <h2 class="font-semibold">Buat jadwal</h2>
  <div class="grid gap-4 sm:grid-cols-3">
    <div><label class="label">Mulai</label><input type="datetime-local" name="start" value="{{ ds }}" required class="input"></div>
    <div><label class="label">Selesai</label><input type="datetime-local" name="end" value="{{ de }}" required class="input"></div>
    <div><label class="label">Untuk divisi</label>
      <select name="div" class="input"><option value="">Semua divisi</option>{% for d in divs %}<option value="{{ d.id }}">{{ d.name }}</option>{% endfor %}</select></div>
  </div>
  <div>
    <div class="mb-2 flex flex-wrap items-center gap-3">
      <label class="label !mb-0">Peserta</label>
      <input id="psearch" placeholder="Cari nama…" class="input !w-48 !py-1.5">
      <select id="pdiv" class="input !w-auto !py-1.5"><option value="">Semua divisi</option>{% for d in divs %}<option value="{{ d.id }}">{{ d.name }}</option>{% endfor %}<option value="0">Tanpa divisi</option></select>
      <label class="flex items-center gap-1.5 text-sm"><input type="checkbox" id="pall" class="accent-violet-600"> Pilih yang tampil</label>
      <label class="flex items-center gap-1.5 text-sm font-semibold text-brand-700"><input type="checkbox" name="all" value="1" class="accent-violet-600"> Semua peserta</label>
    </div>
    <div id="plist" class="grid max-h-64 gap-1 overflow-y-auto rounded-xl border border-brand-100 bg-white p-2 sm:grid-cols-2">
      {% for p in people %}
      <label class="pitem flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-brand-50" data-n="{{ p.name|lower }} {{ p.nik|lower }}" data-d="{{ p.div_id or 0 }}">
        <input type="checkbox" name="pids" value="{{ p.id }}" class="accent-violet-600"> <span>{{ p.name }} <span class="text-xs text-slate-400">{{ p.nik }}{% if p.dname %} · {{ p.dname }}{% endif %}</span></span>
      </label>
      {% else %}<p class="p-3 text-sm text-slate-400">Belum ada peserta.</p>{% endfor %}
    </div>
  </div>
  <button class="btn btn-primary">Simpan jadwal</button>
</form>

<div class="mt-8 flex flex-wrap items-end justify-between gap-3">
  <form method="get" class="flex gap-2"><input name="q" value="{{ s }}" placeholder="Cari nama / NIK…" class="input !w-64"><button class="btn btn-ghost">Cari</button></form>
  <form method="post" action="/admin/jadwal/bersih" onsubmit="return confirm('Hapus semua jadwal yang sudah berakhir?')"><button class="btn btn-danger">Hapus jadwal berakhir</button></form>
</div>

<div class="card mt-4 overflow-x-auto">
  <table class="min-w-full divide-y divide-brand-50">
    <thead class="bg-brand-50/60"><tr><th class="th">Peserta</th><th class="th">Divisi</th><th class="th">Mulai</th><th class="th">Selesai</th><th class="th">Status</th><th class="th"></th></tr></thead>
    <tbody class="divide-y divide-brand-50">
      {% for r in rows %}
      <tr class="hover:bg-brand-50/50">
        <td class="td font-medium">{{ r.name }}<span class="block text-xs font-normal text-slate-400">{{ r.nik }}</span></td>
        <td class="td">{{ r.dname or 'Semua divisi' }}</td>
        <td class="td whitespace-nowrap">{{ r.t_start }}</td><td class="td whitespace-nowrap">{{ r.t_end }}</td>
        <td class="td"><span class="pill {{ 'bg-emerald-100 text-emerald-700' if r.st == 'aktif' else ('bg-sky-100 text-sky-700' if r.st == 'akan' else 'bg-slate-100 text-slate-500') }}">{{ 'Berlangsung' if r.st == 'aktif' else ('Akan datang' if r.st == 'akan' else 'Berakhir') }}</span></td>
        <td class="td text-right"><form method="post" action="/admin/jadwal/hapus/{{ r.id }}" onsubmit="return confirm('Hapus jadwal ini?')"><button class="text-sm font-medium text-rose-600 hover:underline">Hapus</button></form></td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="p-8 text-center text-slate-400">Belum ada jadwal.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<script>
(function () {
  var items = document.querySelectorAll('.pitem'), box = document.getElementById('psearch'), dv = document.getElementById('pdiv');
  function apply() {
    var v = box.value.toLowerCase(), d = dv.value;
    items.forEach(function (i) {
      var ok = i.getAttribute('data-n').indexOf(v) >= 0 && (d === '' || i.getAttribute('data-d') === d);
      i.style.display = ok ? '' : 'none';
    });
  }
  box.addEventListener('input', apply); dv.addEventListener('change', apply);
  document.getElementById('pall').addEventListener('change', function () {
    var on = this.checked;
    items.forEach(function (i) { if (i.style.display !== 'none') i.querySelector('input').checked = on; });
  });
})();
</script>
{% endblock %}
'''

TEMPLATES["admin_soal.html"] = r'''{% extends "base.html" %}
{% block title %}Soal {{ title }}{% endblock %}
{% block awidth %}max-w-4xl{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold">Soal {{ title }} <span class="text-base font-normal text-slate-400">({{ qs|length }})</span></h1>
{% if phase == 'remed' %}<p class="mt-1 text-sm text-slate-500">Opsional. Jika divisi belum punya soal Remedial, peserta mengerjakan soal Post Test divisinya.</p>{% endif %}

<div class="mt-4 flex flex-wrap items-center justify-between gap-2">
  <form method="get" class="flex flex-wrap items-center gap-2">
    <span class="text-sm font-semibold text-slate-500">Filter divisi:</span>
    <select name="div" class="input !w-auto" onchange="this.form.submit()">
      <option value="">Semua</option><option value="u" {{ 'selected' if flt == 'u' }}>Tanpa divisi (semua divisi)</option>
      {% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if flt == d.id|string }}>{{ d.name }}</option>{% endfor %}
    </select>
  </form>
  {% if qs %}
  <form method="post" action="/admin/soal/{{ phase }}/hapus-semua" onsubmit="return confirm('Hapus SEMUA {{ qs|length }} soal {{ title }}{{ ' pada filter ini' if flt else '' }}? Tindakan ini tidak bisa dibatalkan.')">
    <input type="hidden" name="flt" value="{{ flt }}">
    <button class="btn btn-danger">Hapus semua soal ({{ qs|length }})</button>
  </form>
  {% endif %}
</div>

<form method="post" enctype="multipart/form-data" class="card mt-4 space-y-5 p-5 sm:p-6">
  <h2 class="font-semibold">Tambah soal</h2>
  <div class="grid gap-4 sm:grid-cols-3">
    <div class="sm:col-span-2">
      <label class="label">Pertanyaan</label>
      <input name="text" required class="input" placeholder="Contoh: Apakah headrest ini OK?">
    </div>
    <div>
      <label class="label">Untuk divisi</label>
      <select name="div" class="input"><option value="">Semua divisi</option>
        {% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if flt == d.id|string }}>{{ d.name }}</option>{% endfor %}</select>
    </div>
  </div>
  <div>
    <label class="label">Foto soal (opsional)</label>
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

  <div class="grid gap-4 sm:grid-cols-2">
    <div>
      <label class="label">Penjelasan (muncul di pembahasan)</label>
      <textarea name="explain" rows="3" class="input"></textarea>
    </div>
    <div>
      <label class="label">Gambar penjelasan (opsional)</label>
      <input type="file" name="explain_img" accept="image/*" class="block w-full text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:font-semibold file:text-brand-700 hover:file:bg-brand-100">
      <p class="mt-1 text-xs text-slate-400">Tampil di pembahasan setelah tes. PNG, JPG, GIF, atau WEBP.</p>
    </div>
  </div>
  <button class="btn btn-primary">Simpan soal</button>
</form>

{% if src %}
<details class="card mt-4 p-4">
  <summary class="cursor-pointer text-sm font-semibold">Salin soal dari tes lain</summary>
  <form method="post" action="/admin/soal/{{ phase }}/salin" class="mt-3 space-y-2">
    {% for s in src %}
    <label class="flex items-start gap-3 rounded-lg p-2 text-sm hover:bg-slate-50">
      <input type="checkbox" name="ids" value="{{ s.id }}" class="mt-1 accent-violet-600">
      <span>{{ s.text }} <span class="pill bg-slate-100 text-slate-500">{{ s.plabel }}</span> <span class="pill bg-brand-50 text-brand-700">{{ divmap.get(s.div_id, 'Semua divisi') }}</span></span>
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
        <p class="font-medium"><span class="text-slate-400">#{{ loop.index }}</span> {{ x.text }}
          <span class="pill ml-1 bg-brand-50 text-brand-700">{{ divmap.get(x.div_id, 'Semua divisi') }}</span></p>
        <div class="mt-2 flex flex-wrap gap-1.5">
          {% for o in x.opts %}
            {% set right = loop.index0 == x.ans_main %}
            <span class="pill {{ 'bg-emerald-100 text-emerald-700' if right else 'bg-slate-100 text-slate-600' }}">
              {{ o.label }}{% if o.kids %} ▸ {{ o.kids|join(' / ') }}{% endif %}
            </span>
          {% endfor %}
        </div>
        {% if x.explain or x.explain_img %}
        <div class="mt-2 flex items-start gap-3 text-xs text-slate-500">
          {% if x.explain_img %}<img src="/file/{{ x.explain_img }}" class="h-16 w-24 shrink-0 rounded-lg border border-slate-200 bg-white object-contain" alt="">{% endif %}
          <p>{{ x.explain }}</p>
        </div>
        {% endif %}
      </div>
    </div>
    <div class="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
      <a href="/admin/soal/edit/{{ x.id }}" class="btn btn-primary !py-1.5 !text-xs">✎ Edit</a>
      {% for k, lab in others %}
      <form method="post" action="/admin/soal/satu/{{ x.id }}/{{ k }}"><button class="btn btn-ghost !py-1.5 !text-xs">Salin ke {{ lab }}</button></form>
      {% endfor %}
      <form method="post" action="/admin/soal/satu/{{ x.id }}/{{ phase }}"><button class="btn btn-ghost !py-1.5 !text-xs">Duplikat</button></form>
      <form method="post" action="/admin/soal/divisi/{{ x.id }}" class="flex gap-1">
        <input type="hidden" name="flt" value="{{ flt }}">
        <select name="div" class="input !w-auto !py-1 !text-xs"><option value="">Semua divisi</option>
          {% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if x.div_id == d.id }}>{{ d.name }}</option>{% endfor %}</select>
        <button class="btn btn-ghost !py-1.5 !text-xs">Pindah divisi</button>
      </form>
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
<h1 class="text-2xl font-bold">Materi <span class="text-base font-normal text-slate-400">({{ mats|length }})</span></h1>
<form method="post" enctype="multipart/form-data" class="card mt-6 space-y-4 p-5">
  <div class="grid gap-4 sm:grid-cols-2">
    <div><label class="label">Judul</label><input name="title" class="input" placeholder="Contoh: Panduan Cacat Headrest"></div>
    <div><label class="label">Untuk divisi</label>
      <select name="div" class="input"><option value="">Semua divisi</option>{% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if flt == d.id|string }}>{{ d.name }}</option>{% endfor %}</select></div>
  </div>
  <div><label class="label">File (PDF, gambar, video, dll.)</label>
    <input type="file" name="file" required class="block w-full text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:font-semibold file:text-brand-700 hover:file:bg-brand-100"></div>
  <button class="btn btn-primary">Unggah</button>
</form>

<div class="mt-6 flex flex-wrap items-center justify-between gap-2">
  <form method="get" class="flex flex-wrap items-center gap-2">
    <span class="text-sm font-semibold text-slate-500">Filter divisi:</span>
    <select name="div" class="input !w-auto" onchange="this.form.submit()">
      <option value="">Semua</option><option value="u" {{ 'selected' if flt == 'u' }}>Tanpa divisi (semua divisi)</option>
      {% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if flt == d.id|string }}>{{ d.name }}</option>{% endfor %}
    </select>
  </form>
  {% if mats %}
  <form method="post" action="/admin/materi/hapus-semua" onsubmit="return confirm('Hapus SEMUA {{ mats|length }} materi{{ ' pada filter ini' if flt else '' }}? File ikut terhapus dan tidak bisa dikembalikan.')">
    <input type="hidden" name="flt" value="{{ flt }}">
    <button class="btn btn-danger">Hapus semua materi ({{ mats|length }})</button>
  </form>
  {% endif %}
</div>

<div class="mt-4 space-y-2">
  {% for m in mats %}
  <div class="card flex flex-wrap items-center gap-3 p-4">
    <div class="min-w-0 flex-1"><p class="truncate font-medium">{{ m.title }}</p>
      <p class="text-xs text-slate-400">{{ m.created }} · <span class="font-semibold text-brand-600">{{ divmap.get(m.div_id, 'Semua divisi') }}</span></p></div>
    <form method="post" action="/admin/materi/divisi/{{ m.id }}" class="flex gap-1">
      <select name="div" class="input !w-auto !py-1.5 !text-xs"><option value="">Semua divisi</option>
        {% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if m.div_id == d.id }}>{{ d.name }}</option>{% endfor %}</select>
      <button class="btn btn-ghost !py-1.5 !text-xs">Pindah</button>
    </form>
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
{% block awidth %}max-w-5xl{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold">Peserta <span class="text-base font-normal text-slate-400">({{ total }})</span></h1>
<p class="mt-1 text-sm text-slate-500">Peserta dikelompokkan per divisi. Atur waktu training di menu <a href="/admin/jadwal" class="font-semibold text-brand-600 underline">Jadwal</a>.</p>

<div class="mt-6 grid gap-4 md:grid-cols-2">
  <form method="post" class="card space-y-4 p-5">
    <h2 class="font-semibold">Tambah satu</h2>
    <div><label class="label">Nama</label><input name="name" class="input"></div>
    <div class="grid grid-cols-2 gap-3">
      <div><label class="label">NIK</label><input name="nik" class="input"></div>
      <div><label class="label">Divisi</label><select name="div" class="input"><option value="">Tanpa divisi</option>{% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if flt == d.id|string }}>{{ d.name }}</option>{% endfor %}</select></div>
    </div>
    <button class="btn btn-primary">Tambah</button>
  </form>
  <form method="post" class="card space-y-4 p-5">
    <h2 class="font-semibold">Tambah banyak (paste dari Excel)</h2>
    <textarea name="bulk" rows="4" class="input font-mono" placeholder="Budi Santoso;12345&#10;Siti Aminah;12346;Sewing"></textarea>
    <div><label class="label">Divisi untuk semua baris</label><select name="div" class="input"><option value="">Tanpa divisi</option>{% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if flt == d.id|string }}>{{ d.name }}</option>{% endfor %}</select></div>
    <p class="text-xs text-slate-400">Satu baris satu orang: Nama, NIK, dan (opsional) nama divisi, dipisah tab, titik koma, atau koma. Jika kolom divisi diisi dan cocok, itu yang dipakai.</p>
    <button class="btn btn-primary">Tambah semua</button>
  </form>
</div>

<form method="get" class="mt-6 flex flex-wrap items-center gap-2">
  <span class="text-sm font-semibold text-slate-500">Tampilkan:</span>
  <select name="div" class="input !w-auto" onchange="this.form.submit()">
    <option value="">Semua divisi</option>
    {% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if flt == d.id|string }}>{{ d.name }}</option>{% endfor %}
    <option value="u" {{ 'selected' if flt == 'u' }}>Tanpa divisi</option>
  </select>
</form>

{% for g in groups %}
<div class="card mt-4 overflow-hidden">
  <div class="bg-brand-50/60 px-4 py-3">
    <h2 class="font-bold">{{ g.name }} <span class="text-sm font-normal text-slate-400">({{ g.people|length }} peserta)</span></h2>
  </div>
  <div class="hidden gap-2 border-b border-slate-100 bg-slate-50 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-brand-700/70 sm:grid sm:grid-cols-[minmax(0,1.4fr)_8rem_minmax(0,1fr)_11rem]">
    <span>Nama</span><span>NIK</span><span>Divisi</span><span></span>
  </div>
  {% for p in g.people %}
  <div class="grid gap-2 border-t border-slate-100 p-3 first:border-t-0 hover:bg-slate-50 sm:grid-cols-[minmax(0,1.4fr)_8rem_minmax(0,1fr)_11rem] sm:items-center">
    <div><label class="label sm:hidden">Nama</label><input form="pf{{ p.id }}" name="name" value="{{ p.name }}" class="input !py-1.5 font-medium"></div>
    <div><label class="label sm:hidden">NIK</label><input form="pf{{ p.id }}" name="nik" value="{{ p.nik }}" class="input !py-1.5"></div>
    <div><label class="label sm:hidden">Divisi</label><select form="pf{{ p.id }}" name="div" class="input !py-1.5"><option value="">Tanpa divisi</option>{% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if p.div_id == d.id }}>{{ d.name }}</option>{% endfor %}</select></div>
    <div class="flex gap-2 sm:justify-end">
      <form id="pf{{ p.id }}" method="post" action="/admin/peserta/edit/{{ p.id }}"><input type="hidden" name="flt" value="{{ flt }}"><button class="btn btn-ghost !py-1.5 !text-xs">Simpan</button></form>
      <form method="post" action="/admin/peserta/hapus/{{ p.id }}" onsubmit="return confirm('Hapus peserta ini beserta jadwalnya?')"><button class="btn btn-danger !py-1.5 !text-xs">Hapus</button></form>
    </div>
  </div>
  {% else %}
  <p class="p-6 text-center text-sm text-slate-400">Belum ada peserta di divisi ini.</p>
  {% endfor %}
</div>
{% else %}
<div class="card mt-4 p-8 text-center text-slate-400">Belum ada divisi atau peserta.</div>
{% endfor %}
{% endblock %}
'''

TEMPLATES["admin_activity.html"] = r'''{% extends "base.html" %}
{% block title %}Aktivitas{% endblock %}
{% block awidth %}max-w-6xl{% endblock %}
{% block content %}
<div class="flex flex-wrap items-end justify-between gap-3">
  <div><h1 class="text-2xl font-bold">Log Aktivitas</h1><p class="text-sm text-slate-500">Pre/Post, membuka materi, dan lama membaca materi.</p></div>
  {% if rows %}
  <form method="post" action="/admin/aktivitas/hapus-semua" onsubmit="return confirm('Hapus SEMUA log aktivitas yang cocok dengan filter{{ ' ini' if s or k else ' (tanpa filter = semua log)' }}? Tidak bisa dibatalkan.')">
    <input type="hidden" name="q" value="{{ s }}"><input type="hidden" name="kind" value="{{ k }}">
    <button class="btn btn-danger">Hapus log {{ 'sesuai filter' if s or k else 'semua' }}</button>
  </form>
  {% endif %}
</div>
<form method="get" class="card mt-4 flex flex-wrap gap-2 p-4">
  <input name="q" value="{{ s }}" placeholder="Cari nama / NIK…" class="input !w-64">
  <select name="kind" class="input !w-auto">
    <option value="">Semua aktivitas</option>
    {% for key, label in EVENT.items() %}<option value="{{ key }}" {{ 'selected' if k == key }}>{{ label }}</option>{% endfor %}
  </select>
  <button class="btn btn-ghost">Filter</button>
</form>

<div class="card mt-4 overflow-x-auto">
  <table class="min-w-full divide-y divide-slate-100">
    <thead class="bg-slate-50"><tr><th class="th">Waktu</th><th class="th">Nama</th><th class="th">NIK</th><th class="th">Divisi</th><th class="th">Aktivitas</th><th class="th">Detail</th><th class="th">Durasi</th></tr></thead>
    <tbody class="divide-y divide-slate-100">
      {% for r in rows %}
      <tr class="hover:bg-slate-50">
        <td class="td whitespace-nowrap text-slate-500">{{ r['at'] }}</td>
        <td class="td font-medium">{{ r['name'] }}</td>
        <td class="td text-slate-500">{{ r['nik'] }}</td>
        <td class="td">{{ divmap.get(r['div_id'], '-') }}</td>
        <td class="td"><span class="pill bg-brand-50 text-brand-700">{{ EVENT.get(r['kind'], r['kind']) }}</span></td>
        <td class="td text-slate-600">{{ r['detail'] }}</td>
        <td class="td whitespace-nowrap">{{ r['secs']|dur }}</td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="p-8 text-center text-slate-400">Belum ada aktivitas.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
<p class="mt-2 text-xs text-slate-400">Menampilkan maksimal 500 aktivitas terbaru.</p>
{% endblock %}
'''

TEMPLATES["admin_soal_edit.html"] = r'''{% extends "base.html" %}
{% block title %}Edit Soal{% endblock %}
{% block awidth %}max-w-4xl{% endblock %}
{% block content %}
<a href="/admin/soal/{{ x.phase }}" class="text-sm text-slate-500 transition hover:text-brand-600">← Kembali ke Soal {{ title }}</a>
<h1 class="mt-2 text-2xl font-bold">Edit soal <span class="text-grad">{{ title }}</span></h1>

<form method="post" enctype="multipart/form-data" class="card mt-6 space-y-5 p-5 sm:p-6">
  <div class="grid gap-4 sm:grid-cols-3">
    <div class="sm:col-span-2">
      <label class="label">Pertanyaan</label>
      <input name="text" required value="{{ x.text }}" class="input">
    </div>
    <div>
      <label class="label">Untuk divisi</label>
      <select name="div" class="input"><option value="">Semua divisi</option>
        {% for d in divs %}<option value="{{ d.id }}" {{ 'selected' if x.div_id == d.id }}>{{ d.name }}</option>{% endfor %}</select>
    </div>
  </div>

  <div>
    <label class="label">Foto soal</label>
    {% if x.image %}
    <div class="mb-2 flex items-center gap-3">
      <img src="/file/{{ x.image }}" class="h-24 w-36 rounded-xl border border-slate-200 bg-slate-50 object-contain" alt="">
      <label class="flex items-center gap-2 text-sm text-rose-600"><input type="checkbox" name="rm_image" value="1" class="accent-rose-600"> Hapus foto ini</label>
    </div>
    {% endif %}
    <input type="file" name="image" accept="image/*" class="block w-full text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:font-semibold file:text-brand-700 hover:file:bg-brand-100">
    <p class="mt-1 text-xs text-slate-400">{{ 'Pilih file baru untuk mengganti foto.' if x.image else 'Belum ada foto.' }}</p>
  </div>

  <div>
    <label class="label">Opsi jawaban (minimal 2, pilih 1 yang benar)</label>
    <div class="grid gap-3 sm:grid-cols-2">
      {% for sl in slots %}
      <div class="rounded-xl border border-slate-200 p-3">
        <div class="flex items-center gap-2">
          <input type="radio" name="correct" value="{{ loop.index0 }}" {{ 'checked' if loop.index0 == x.ans_main }} class="h-4 w-4 accent-emerald-600" title="Jawaban benar">
          <input name="label{{ loop.index0 }}" value="{{ sl.label }}" class="input" placeholder="Opsi {{ loop.index }} (kosongkan jika tidak dipakai)">
        </div>
        <textarea name="kids{{ loop.index0 }}" rows="3" class="input mt-2" placeholder="Jawaban lanjutan (opsional), satu per baris. Awali * untuk yang benar">{{ sl.kids }}</textarea>
      </div>
      {% endfor %}
    </div>
    <p class="mt-2 text-xs text-slate-400">Label "OK" dianggap OK, berawalan "NG" dianggap NG. Tanda * menandai jawaban lanjutan yang benar.</p>
  </div>

  <div class="grid gap-4 sm:grid-cols-2">
    <div>
      <label class="label">Penjelasan (muncul di pembahasan)</label>
      <textarea name="explain" rows="4" class="input">{{ x.explain or '' }}</textarea>
    </div>
    <div>
      <label class="label">Gambar penjelasan</label>
      {% if x.explain_img %}
      <div class="mb-2 flex items-center gap-3">
        <img src="/file/{{ x.explain_img }}" class="h-24 w-36 rounded-xl border border-slate-200 bg-slate-50 object-contain" alt="">
        <label class="flex items-center gap-2 text-sm text-rose-600"><input type="checkbox" name="rm_explain_img" value="1" class="accent-rose-600"> Hapus gambar ini</label>
      </div>
      {% endif %}
      <input type="file" name="explain_img" accept="image/*" class="block w-full text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:font-semibold file:text-brand-700 hover:file:bg-brand-100">
      <p class="mt-1 text-xs text-slate-400">{{ 'Pilih file baru untuk mengganti gambar.' if x.explain_img else 'Belum ada gambar penjelasan.' }}</p>
    </div>
  </div>

  <p class="rounded-xl bg-amber-50 p-3 text-xs text-amber-800">Perubahan hanya berlaku untuk tes berikutnya. Hasil tes yang sudah dikerjakan peserta tidak berubah.</p>
  <div class="flex flex-wrap gap-2">
    <button class="btn btn-primary">Simpan perubahan</button>
    <a href="/admin/soal/{{ x.phase }}" class="btn btn-ghost">Batal</a>
  </div>
</form>
{% endblock %}
'''

TEMPLATES["admin_trash.html"] = r'''{% extends "base.html" %}
{% block title %}Sampah Riwayat{% endblock %}
{% block awidth %}max-w-6xl{% endblock %}
{% block content %}
<div class="flex flex-wrap items-end justify-between gap-3">
  <div>
    <h1 class="text-2xl font-bold">🗑️ Sampah Riwayat <span class="text-base font-normal text-slate-400">({{ total }})</span></h1>
    <p class="mt-1 text-sm text-slate-500">Riwayat tes dan log aktivitas yang dihapus admin disimpan di sini selama <b>{{ days }} hari</b>, lalu hilang otomatis. Sebelum itu bisa dipulihkan.</p>
  </div>
  {% if total %}
  <form method="post" action="/admin/sampah/kosongkan" onsubmit="var v = prompt('Ini menghapus PERMANEN semua isi Sampah ({{ total }} data). Ketik HAPUS untuk melanjutkan:'); if (v !== 'HAPUS') return false; this.ok.value = v; return true;">
    <input type="hidden" name="ok" value=""><button class="btn btn-danger">Kosongkan Sampah</button>
  </form>
  {% endif %}
</div>

{% if batches %}
<h2 class="mb-2 mt-6 text-sm font-bold uppercase tracking-[.18em] text-slate-500">Penghapusan terakhir</h2>
<div class="grid gap-3 md:grid-cols-2">
  {% for b in batches %}
  <div class="card flex flex-wrap items-center justify-between gap-3 p-4">
    <div class="min-w-0">
      <p class="font-semibold">{{ b.reason }}</p>
      <p class="text-xs text-slate-400">{{ b.at[:16] }} · {{ b.c }} {{ 'tes' if b.kind == 'attempt' else 'log aktivitas' }}</p>
    </div>
    <form method="post" action="/admin/sampah/pulihkan-batch/{{ b.batch }}" onsubmit="return confirm('Pulihkan {{ b.c }} data dari penghapusan ini?')">
      <button class="btn btn-primary !py-2">Pulihkan semua</button></form>
  </div>
  {% endfor %}
</div>
{% endif %}

<form method="get" class="card mt-6 flex flex-wrap gap-2 p-4">
  <input name="q" value="{{ s }}" placeholder="Cari nama…" class="input !w-56">
  <select name="kind" class="input !w-auto">
    <option value="">Semua jenis</option>
    <option value="attempt" {{ 'selected' if k == 'attempt' }}>Riwayat tes</option>
    <option value="event" {{ 'selected' if k == 'event' }}>Log aktivitas</option>
  </select>
  <button class="btn btn-ghost">Filter</button>
</form>

<div class="card mt-4 overflow-hidden">
  <div class="overflow-x-auto">
  <table class="min-w-full divide-y divide-brand-50">
    <thead class="bg-brand-50/60"><tr>
      <th class="th">Dihapus</th><th class="th">Jenis</th><th class="th">Data</th><th class="th">Alasan</th><th class="th">Sisa waktu</th><th class="th text-right">Aksi</th>
    </tr></thead>
    <tbody class="divide-y divide-brand-50">
      {% for t in items %}
      <tr class="hover:bg-brand-50/50">
        <td class="td whitespace-nowrap text-slate-500">{{ t.deleted }}</td>
        <td class="td"><span class="pill {{ 'bg-brand-50 text-brand-700' if t.kind == 'attempt' else 'bg-slate-100 text-slate-600' }}">{{ 'Tes' if t.kind == 'attempt' else 'Aktivitas' }}</span></td>
        <td class="td min-w-[240px]">{{ t.summ }}<span class="block text-xs text-slate-400">tanggal asli {{ t.when }}</span></td>
        <td class="td text-slate-500">{{ t.reason }}</td>
        <td class="td whitespace-nowrap"><span class="pill {{ 'bg-rose-100 text-rose-700' if t.left <= 3 else 'bg-slate-100 text-slate-600' }}">{{ t.left }} hari lagi</span></td>
        <td class="td whitespace-nowrap text-right">
          <form method="post" action="/admin/sampah/pulihkan/{{ t.id }}" class="inline"><button class="font-semibold text-brand-600 hover:underline">Pulihkan</button></form>
          <form method="post" action="/admin/sampah/hapus/{{ t.id }}" class="ml-3 inline" onsubmit="return confirm('Hapus permanen data ini?')"><button class="font-semibold text-rose-600 hover:underline">Hapus permanen</button></form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="p-10 text-center text-slate-400">Sampah kosong.</td></tr>
      {% endfor %}
    </tbody>
  </table>
  </div>
</div>
{% if more %}<p class="mt-2 text-xs text-slate-400">Menampilkan 300 data terbaru, {{ more }} lainnya tidak ditampilkan. Persempit dengan filter.</p>{% endif %}
{% endblock %}
'''

app.jinja_loader = DictLoader(TEMPLATES)


if __name__ == "__main__":
    from waitress import serve
    purge_trash()
    if ADMIN_PASSWORD == "admin123":
        print("PERINGATAN: password admin masih default (admin123). Ganti segera, terutama jika diakses dari internet!")
    print(f"Aplikasi jalan. PC ini: http://localhost:{PORT}  |  Jaringan kantor: http://IP-PC-INI:{PORT}")
    serve(app, host="0.0.0.0", port=PORT, threads=8)
