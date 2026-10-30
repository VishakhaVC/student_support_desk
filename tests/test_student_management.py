import pytest
from werkzeug.security import check_password_hash

from app.extensions import db
from app.models import AuditLog, FeeRecord, StudentProfile, User
from app.security.authz import PermissionDenied
from app.services import student_admin
from app.services.validators import ValidationError
from tests.conftest import Browser

NEW = {"student_id": "S2026100", "username": "new.student", "full_name": "Meera Nair", "email": "meera.nair@students.example.edu",
       "phone": "+91 90000 20001", "programme": "PGDM (Big Data Analytics)", "trimester": "Trimester I", "cgpa": "3.2", "status": "active",
       "password": "Valid#Passw0rd1", "total_fee": "450000"}


def add(admin, **over):
    return admin.post("/admin/students/new", data={**NEW, **over})


def test_admin_creates_student_with_all_related_records(app, admin):
    before = StudentProfile.query.filter_by(status="active").count()
    r = add(admin)
    assert r.status_code == 200 and b"Meera Nair" in r.data
    p = StudentProfile.query.filter_by(student_id="S2026100").one()
    u = User.query.filter_by(username="new.student").one()
    assert u.role == "student" and u.student_id == "S2026100" and u.active and check_password_hash(u.password_hash, "Valid#Passw0rd1")
    assert u.password_hash != "Valid#Passw0rd1"
    assert FeeRecord.query.filter_by(student_id="S2026100").count() == 1
    assert StudentProfile.query.filter_by(status="active").count() == before + 1
    row = AuditLog.query.filter_by(action="create_student", decision="allowed").one()
    assert row.target_id == "S2026100" and "Valid#Passw0rd1" not in (row.details or "") + (row.reason or "")


def test_new_student_can_sign_in_and_use_portal(app, admin):
    add(admin)
    b = Browser.__new__(Browser)
    b.c = app.test_client()
    assert b.post("/login", data={"username": "new.student", "password": "Valid#Passw0rd1"}).status_code == 302
    page = b.get("/dashboard")
    assert page.status_code == 200 and b"Meera" in page.data and b"450,000" in page.data


def test_generated_password_is_shown_once_and_works(app, admin):
    r = add(admin, password="")
    assert b"Copy the password now" in r.data
    import re
    pw = re.search(rb'class="secret"><span>([^<]+)</span>', r.data).group(1).decode()
    u = User.query.filter_by(username="new.student").one()
    assert check_password_hash(u.password_hash, pw)
    assert pw not in (AuditLog.query.filter_by(action="create_student").one().details or "")
    assert b"Copy the password now" not in admin.get("/admin/students/S2026100").data     # not shown again


@pytest.mark.parametrize("over,field", [
    ({"student_id": "S2026001"}, "student_id"), ({"username": "student_a"}, "username"), ({"email": "not-an-email"}, "email"),
    ({"phone": "abc"}, "phone"), ({"cgpa": "7"}, "cgpa"), ({"cgpa": "abc"}, "cgpa"), ({"password": "short"}, "password"),
    ({"full_name": ""}, "full_name"), ({"trimester": "Trimester IX"}, "trimester"), ({"student_id": "bad id!"}, "student_id"),
    ({"email": "aarav.mehta@students.example.edu"}, "email"),
])
def test_invalid_student_is_rejected_and_nothing_saved(app, admin, over, field):
    users, profiles = User.query.count(), StudentProfile.query.count()
    r = add(admin, **over)
    assert r.status_code == 200 and b"field-err" in r.data
    assert (User.query.count(), StudentProfile.query.count()) == (users, profiles)
    assert AuditLog.query.filter_by(action="create_student", decision="denied").count() == 1


def test_admin_updates_student_details(app, admin):
    r = admin.post("/admin/students/S2026003/edit", data={"full_name": "Kabir S. Shah", "email": "kabir.new@students.example.edu", "phone": "+91 90000 10003",
                                                          "programme": "PGDM (Big Data Analytics)", "trimester": "Trimester III", "cgpa": "3.10"})
    assert r.status_code == 302
    p = StudentProfile.query.filter_by(student_id="S2026003").one()
    assert (p.full_name, p.email, p.trimester, p.cgpa) == ("Kabir S. Shah", "kabir.new@students.example.edu", "Trimester III", 3.1)
    assert User.query.filter_by(student_id="S2026003").one().display_name == "Kabir S. Shah"
    assert AuditLog.query.filter_by(action="update_student", decision="allowed").count() == 1


def test_update_rejects_duplicate_email_and_bad_values(app, admin):
    admin.post("/admin/students/S2026003/edit", data={"full_name": "Kabir Shah", "email": "diya.patel@students.example.edu", "phone": "+91 90000 10003",
                                                      "programme": "PGDM", "trimester": "Trimester II", "cgpa": "2.7"})
    assert StudentProfile.query.filter_by(student_id="S2026003").one().email == "kabir.shah@students.example.edu"


def test_deactivation_blocks_login_and_ends_open_session(app, admin):
    st = Browser(app, "student_b")
    assert st.get("/dashboard").status_code == 200
    r = admin.post("/admin/students/S2026002/status", data={"status": "inactive"})
    assert r.status_code == 302
    assert User.query.filter_by(username="student_b").one().active is False
    assert st.get("/dashboard").status_code == 302                       # open session is ended on the next request
    assert st.get("/api/conversations").status_code == 401
    fresh = Browser.__new__(Browser); fresh.c = app.test_client()
    blocked = fresh.post("/login", data={"username": "student_b", "password": "Tst-Pass#B-9x1"})
    assert blocked.status_code == 403 and b"not active" in blocked.data
    assert AuditLog.query.filter_by(action="deactivate_student").count() == 1
    assert AuditLog.query.filter_by(action="login", result="account_inactive").count() == 1


def test_reactivation_restores_access(app, admin):
    admin.post("/admin/students/S2026002/status", data={"status": "inactive"})
    admin.post("/admin/students/S2026002/status", data={"status": "active"})
    assert Browser(app, "student_b").get("/dashboard").status_code == 200


def test_remove_is_soft_hides_student_and_keeps_records(app, admin, student):
    student.post("/api/chat", json={"message": "What is my current fee status?"})
    admin.post("/admin/students/S2026001/status", data={"status": "removed"})
    p = StudentProfile.query.filter_by(student_id="S2026001").one()
    assert p.status == "removed" and User.query.filter_by(student_id="S2026001").one().active is False
    assert b"Aarav Mehta" not in admin.get("/admin/students").data
    assert b"Aarav Mehta" in admin.get("/admin/students?status=removed").data
    assert AuditLog.query.filter_by(actor_role="student").count() >= 1                    # audit history preserved
    assert AuditLog.query.filter_by(action="remove_student").count() == 1


def test_admin_can_reset_password(app, admin):
    r = admin.post("/admin/students/S2026002/reset-password")
    assert r.status_code == 200 and b"Copy the password now" in r.data
    fresh = Browser.__new__(Browser); fresh.c = app.test_client()
    assert fresh.post("/login", data={"username": "student_b", "password": "Tst-Pass#B-9x1"}).status_code == 401   # old password stops working
    assert not any("password" in (a.details or "").lower() for a in AuditLog.query.filter_by(action="reset_password"))


def test_students_cannot_use_student_management(app, student):
    for method, url in (("get", "/admin/students"), ("get", "/admin/students/new"), ("get", "/admin/students/S2026002"),
                        ("post", "/admin/students/new"), ("post", "/admin/students/S2026002/status"), ("post", "/admin/students/S2026002/reset-password")):
        assert getattr(student, method)(url).status_code == 403
    assert User.query.filter_by(username="student_b").one().active is True


def test_service_functions_require_admin_role(app, student_a):
    with pytest.raises(PermissionDenied):
        student_admin.create_student(student_a, NEW)
    with pytest.raises(PermissionDenied):
        student_admin.set_status(student_a, "S2026002", "inactive")
    assert AuditLog.query.filter_by(decision="denied", actor_role="student").count() == 2


def test_search_and_filters(app, admin):
    assert b"Diya Patel" in admin.get("/admin/students?q=diya").data and b"Aarav Mehta" not in admin.get("/admin/students?q=diya").data
    page = admin.get("/admin/students?programme=PGDM+(Big+Data+Analytics)").data
    assert b"Aarav Mehta" in page and b"Diya Patel" not in page
    admin.post("/admin/students/S2026003/status", data={"status": "inactive"})
    assert b"Kabir Shah" in admin.get("/admin/students?status=inactive").data and b"Kabir Shah" not in admin.get("/admin/students?status=active").data


def test_dashboard_student_count_follows_changes(app, admin):
    def active():
        import re
        return int(re.search(rb'Active students</div><div class="value num">(\d+)', admin.get("/admin/").data).group(1))
    assert active() == 3
    add(admin)
    assert active() == 4
    admin.post("/admin/students/S2026100/status", data={"status": "inactive"})
    assert active() == 3
