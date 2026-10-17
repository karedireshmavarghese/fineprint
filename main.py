import base64, hashlib, hmac, json, os, secrets, sqlite3, time
from collections import Counter
import httpx
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rules import analyze

DB = os.getenv("DB_PATH", "fineprint.db")
SECRET = (os.getenv("SECRET_KEY") or secrets.token_hex(32)).encode()
app = FastAPI(title="Fineprint")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as _c:
    _c.executescript("""
    create table if not exists users(id integer primary key, name text, email text unique, pw text, salt text);
    create table if not exists bills(id integer primary key, user_id integer, hospital text, addr text,
                                     billed int, pays int, flags text, created real);""")


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120_000).hex()


def make_token(uid):
    body = f"{uid}.{int(time.time()) + 7 * 86400}"
    return body + "." + hmac.new(SECRET, body.encode(), "sha256").hexdigest()


def current_user(req: Request):
    tok = req.cookies.get("fp", "")
    try:
        uid, exp, sig = tok.split(".")
        good = hmac.new(SECRET, f"{uid}.{exp}".encode(), "sha256").hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            with db() as c:
                return c.execute("select id,name,email from users where id=?", (uid,)).fetchone()
    except ValueError:
        pass
    return None


def login_cookie(res: Response, uid):
    res.set_cookie("fp", make_token(uid), max_age=7 * 86400, httponly=True, samesite="lax")


class Signup(BaseModel):
    name: str
    email: str
    password: str


class Login(BaseModel):
    email: str
    password: str


@app.post("/api/signup")
def signup(b: Signup, res: Response):
    email = b.email.strip().lower()
    if not b.name.strip() or "@" not in email or len(b.password) < 6:
        raise HTTPException(422, "Enter your name, a valid email, and a password of 6+ characters.")
    salt = secrets.token_hex(16)
    try:
        with db() as c:
            cur = c.execute("insert into users(name,email,pw,salt) values(?,?,?,?)",
                            (b.name.strip(), email, hash_pw(b.password, salt), salt))
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This email already has an account. Sign in instead.")
    login_cookie(res, uid)
    return {"user": {"name": b.name.strip(), "email": email}}


@app.post("/api/login")
def login(b: Login, res: Response):
    with db() as c:
        u = c.execute("select * from users where email=?", (b.email.strip().lower(),)).fetchone()
    if not u or not hmac.compare_digest(u["pw"], hash_pw(b.password, u["salt"])):
        raise HTTPException(401, "Wrong email or password.")
    login_cookie(res, u["id"])
    return {"user": {"name": u["name"], "email": u["email"]}}


@app.post("/api/logout")
def logout(res: Response):
    res.delete_cookie("fp")
    return {"ok": True}


@app.get("/api/me")
def me(req: Request):
    u = current_user(req)
    return {"user": {"name": u["name"], "email": u["email"]} if u else None}


def hospital_stats(name):
    with db() as c:
        rows = c.execute("select billed,pays,flags from bills where hospital=?", (name,)).fetchall()
    if not rows:
        return {"bills": 0}
    cut = [100 * (r["billed"] - r["pays"]) / r["billed"] for r in rows if r["billed"]]
    flags = Counter(f for r in rows for f in json.loads(r["flags"]))
    return {"bills": len(rows), "avg_cut_pct": round(sum(cut) / max(len(cut), 1)),
            "common": [k for k, _ in flags.most_common(2)]}


@app.get("/api/hospitals")
def hospitals(q: str):
    if len(q.strip()) < 3:
        raise HTTPException(422, "Type at least 3 letters of the hospital name.")
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search", timeout=15,
                      headers={"User-Agent": "Fineprint-hackathon/1.0"},
                      params={"q": q.strip() + " hospital", "countrycodes": "in", "format": "jsonv2",
                              "addressdetails": 1, "extratags": 1, "limit": 10})
        r.raise_for_status()
        raw = r.json()
    except Exception:
        raise HTTPException(502, "Hospital search is busy. Wait a few seconds and try again.")
    out = []
    for h in raw:
        if h.get("type") not in ("hospital", "clinic", "doctors"):
            continue
        name = h.get("name") or h["display_name"].split(",")[0]
        tags = h.get("extratags") or {}
        out.append({"name": name, "addr": h["display_name"], "lat": h["lat"], "lon": h["lon"],
                    "phone": tags.get("phone") or tags.get("contact:phone"),
                    "website": tags.get("website") or tags.get("contact:website"),
                    "stats": hospital_stats(name)})
    return {"results": out}


class Item(BaseModel):
    n: str
    b: int
    cat: str = "other"


class Check(BaseModel):
    items: list[Item]
    policy: dict
    hospital: dict | None = None


@app.post("/api/check")
def check(b: Check, req: Request):
    cap, days = b.policy.get("room_cap_per_day", 0), b.policy.get("room_days", 0)
    if not b.items or cap <= 0 or days < 1:
        raise HTTPException(422, "Add at least one bill line, a room-rent cap, and number of days.")
    result = analyze([i.model_dump() for i in b.items], b.policy)
    result["saved"] = False
    u = current_user(req)
    if u and result["billed"] > 0:
        h = b.hospital or {}
        flags = sorted({r["t"] for r in result["items"] if r["t"] != "ok"})
        with db() as c:
            c.execute("insert into bills(user_id,hospital,addr,billed,pays,flags,created) values(?,?,?,?,?,?,?)",
                      (u["id"], h.get("name", "Not selected"), h.get("addr", ""), result["billed"],
                       result["insurer_pays"], json.dumps(flags), time.time()))
        result["saved"] = True
    return result


@app.get("/api/bills")
def bills(req: Request):
    u = current_user(req)
    if not u:
        raise HTTPException(401, "Sign in to see your bills.")
    with db() as c:
        rows = c.execute("select hospital,billed,pays,created from bills where user_id=? order by id desc limit 20",
                         (u["id"],)).fetchall()
    return {"bills": [dict(r) for r in rows]}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(503, "Set ANTHROPIC_API_KEY to enable bill photo reading.")
    from anthropic import Anthropic
    data = base64.b64encode(await file.read()).decode()
    prompt = ('Read this hospital bill. Return ONLY JSON: {"items":[{"n":"item name","b":amount_rupees_int,'
              '"cat":"room|prop|other"}]}. room=room rent, prop=surgeon/OT/anaesthesia/nursing fees. Skip totals.')
    msg = Anthropic(api_key=key).messages.create(
        model=os.getenv("MODEL", "claude-sonnet-5-5"), max_tokens=1500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": file.content_type or "image/jpeg", "data": data}},
            {"type": "text", "text": prompt}]}])
    text = msg.content[0].text.strip().strip("`").removeprefix("json").strip()
    try:
        return {"items": json.loads(text)["items"]}
    except Exception:
        raise HTTPException(422, "Could not read the bill. Try a clearer photo.")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
