import os, re, tempfile
os.environ.update(COOKIE_SECURE="0", ADMIN_PASSWORD="test-admin-pass-123", DATABASE_PATH=tempfile.mktemp(suffix=".db"), SECRET_KEY="t")
import app as A

def token(c, path):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

def test_flow():
    m = A.app.test_client(); a = A.app.test_client()
    # csrf required
    assert m.post("/register", data={}).status_code == 400
    t = token(m, "/register")
    r = m.post("/register", data={"csrf": t, "name": "Test User", "phone": "0712345678", "password": "password123"})
    assert r.status_code == 302
    page = m.get("/member").get_data(as_text=True)
    assert "pending" in page
    t = token(m, "/member")
    m.post("/pay", data={"csrf": t, "code": "abc1d2efgh"})
    assert "ABC1D2EFGH" in m.get("/member").get_data(as_text=True)
    # duplicate code rejected
    m2 = A.app.test_client(); t2 = token(m2, "/register")
    m2.post("/register", data={"csrf": t2, "name": "Second", "phone": "0722222222", "password": "password123"})
    t2 = token(m2, "/member"); m2.post("/pay", data={"csrf": t2, "code": "ABC1D2EFGH"})
    assert "already been submitted" in m2.get("/member").get_data(as_text=True)
    # admin pages locked
    assert a.get("/admin").status_code == 302
    assert a.get("/admin/payments.csv").status_code == 302
    # wrong then right admin login
    t = token(a, "/admin/login")
    a.post("/admin/login", data={"csrf": t, "email": A.ADMIN_EMAIL, "password": "bad"})
    assert a.get("/admin").status_code == 302
    a.post("/admin/login", data={"csrf": t, "email": A.ADMIN_EMAIL, "password": "test-admin-pass-123"})
    assert a.get("/admin").status_code == 200
    html = a.get("/admin/payments").get_data(as_text=True)
    pid = int(re.search(r"/admin/payments/(\d+)/approve", html).group(1))
    t = token(a, "/admin/payments")
    a.post(f"/admin/payments/{pid}/approve", data={"csrf": t})
    assert "active" in m.get("/member").get_data(as_text=True)
    assert a.get("/admin/payments.csv").status_code == 200
    # member cannot reach admin
    assert m.get("/admin").status_code == 302
    # sql-ish input is inert
    assert a.get("/admin/members?q=%27%3B+DROP+TABLE+members%3B--").status_code == 200
    assert a.get("/admin/members").status_code == 200
    # callback needs the secret
    assert m.post("/mpesa/callback/nope", json={}).status_code == 404
