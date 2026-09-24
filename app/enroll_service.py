"""Enrollment orchestration built on the tgbot2 enroller core.

- enroll_single(): one course for one account
- enroll_all(): batch run for a user's accounts with live progress
- run_auto_enroll_for_user() / run_auto_enroll_all_users(): background engine
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import config, store
from .feed import get_free_courses
from .udemy_enroller import UdemyAutoEnroller

log = logging.getLogger(__name__)

_validator = ThreadPoolExecutor(max_workers=4, thread_name_prefix="validate")

# ── Batch-run registry (Enroll-All progress) ────────────────────────────────
_batch_lock = threading.Lock()
_batches: dict = {}   # user_id -> progress dict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_batch(user_id: int) -> dict:
    b = _batches.get(user_id)
    if not b:
        b = {
            "status": "idle", "total": 0, "done": 0,
            "enrolled": 0, "failed": 0, "already": 0, "expired": 0,
            "errors": [], "started_at": None, "finished_at": None,
        }
        _batches[user_id] = b
    return b


def get_batch_status(user_id: int) -> dict:
    with _batch_lock:
        return dict(_get_batch(user_id))


def _update_batch(user_id: int, **fields) -> dict:
    with _batch_lock:
        b = _get_batch(user_id)
        b.update(fields)
        return dict(b)


def reset_batch(user_id: int) -> None:
    with _batch_lock:
        _batches.pop(user_id, None)


# ── Core: enroll a list of offers for one account ───────────────────────────

def _enroll_account_in_courses(account: dict, offers: list, local_slugs: set, source: str) -> dict:
    """Port of tgbot2's _enroll_account_in_courses, adapted to feed offers + local dedup."""
    enroller = UdemyAutoEnroller(
        access_token=account["access_token"],
        client_id=account.get("client_id") or None,
    )

    if not enroller.verify_login():
        return {
            "enrolled": [], "already": 0, "expired": 0, "failed": len(offers),
            "error": "Udemy login invalid or expired — re-link your account.",
        }

    enroller._get_enrolled_courses()
    enrolled = []
    already = expired = failed = 0
    batch = []          # (course_id, coupon, title, slug, url)
    free_courses = []   # (offer, course_id, slug)

    # Step 1: dedup (Udemy's own enrolled list + local DB)
    to_process = []
    for offer in offers:
        slug = enroller._extract_slug(offer.url)
        if not slug:
            failed += 1
            continue
        if slug in enroller.enrolled_slugs or slug in local_slugs:
            already += 1
            continue
        to_process.append((offer, slug))

    # Step 2: validate in parallel (resolve course id / free / coupon check)
    def validate(item):
        offer, slug = item
        coupon = offer.coupon or enroller._extract_coupon(offer.url)
        course_id, is_free = enroller._get_course_id_from_page(slug)
        if not course_id:
            return ("failed", offer, slug, None, None)
        if is_free:
            return ("free", offer, slug, course_id, None)
        if not coupon:
            return ("failed", offer, slug, None, None)
        if not enroller._check_coupon(course_id, coupon):
            return ("expired", offer, slug, None, None)
        return ("valid", offer, slug, course_id, coupon)

    futures = [_validator.submit(validate, item) for item in to_process]
    for fut in futures:
        try:
            status, offer, slug, course_id, coupon = fut.result()
        except Exception as e:
            log.debug("validate error: %s", e)
            failed += 1
            continue
        if status == "failed":
            failed += 1
        elif status == "expired":
            expired += 1
        elif status == "free":
            free_courses.append((offer, course_id, slug))
        elif status == "valid":
            batch.append((course_id, coupon, offer.title, slug, offer.url))

    # Step 3: free courses — subscribe directly
    for offer, course_id, slug in free_courses:
        try:
            result = enroller._free_checkout(course_id)
        except Exception as e:
            log.debug("free checkout error: %s", e)
            failed += 1
            continue
        if result == "enrolled":
            if store.log_enrollment(account["user_id"], account["id"], course_id, slug, offer.title, offer.url, source):
                enrolled.append({"title": offer.title, "slug": slug})
            else:
                already += 1
        elif result == "already":
            already += 1
        else:
            failed += 1

    # Step 4: coupon courses — bulk checkout with one-by-one fallback
    if batch:
        was_enrolled_titles = enroller._bulk_checkout(
            [(cid, coup, title) for cid, coup, title, _, _ in batch]
        )
        for course_id, coupon, title, slug, url in batch:
            if title in was_enrolled_titles:
                if store.log_enrollment(account["user_id"], account["id"], course_id, slug, title, url, source):
                    enrolled.append({"title": title, "slug": slug})
                else:
                    already += 1
            else:
                failed += 1

    return {
        "enrolled": enrolled, "already": already, "expired": expired,
        "failed": failed, "error": None,
    }


# ── Single course enroll (Courses page button) ──────────────────────────────

def enroll_single(account: dict, offer) -> dict:
    """Enroll one offer for one account. Returns {status, message}.

    status: enrolled | already | expired | failed
    """
    enroller = UdemyAutoEnroller(
        access_token=account["access_token"],
        client_id=account.get("client_id") or None,
    )
    if not enroller.verify_login():
        return {"status": "failed", "message": "Udemy login invalid or expired."}

    slug = enroller._extract_slug(offer.url)
    if not slug:
        return {"status": "failed", "message": "Could not parse course URL."}
    if store.is_enrolled_by_slug(account["id"], slug):
        return {"status": "already", "message": "Already in your Udemy account."}
    enroller._get_enrolled_courses()
    if slug in enroller.enrolled_slugs:
        return {"status": "already", "message": "Already in your Udemy account."}

    coupon = offer.coupon or enroller._extract_coupon(offer.url)
    course_id, is_free = enroller._get_course_id_from_page(slug)
    if not course_id:
        return {"status": "failed", "message": "Course not found (removed or region-locked)."}

    if is_free:
        result = enroller._free_checkout(course_id)
    elif coupon and enroller._check_coupon(course_id, coupon):
        result = enroller._checkout_single(course_id, coupon, was_enrolled_before=False)
    else:
        return {"status": "expired", "message": "Offer no longer free (coupon expired)."}

    if result == "enrolled":
        store.log_enrollment(account["user_id"], account["id"], course_id, slug, offer.title, offer.url, "manual")
        return {"status": "enrolled", "message": "Enrolled! Check your Udemy account."}
    if result == "already":
        return {"status": "already", "message": "Already in your Udemy account."}
    return {"status": "failed", "message": "Udemy checkout did not succeed. Try again."}


# ── Enroll All (batch with progress) ────────────────────────────────────────

def start_enroll_all(user_id: int) -> dict:
    with _batch_lock:
        b = _get_batch(user_id)
        if b["status"] == "running":
            return {"ok": False, "error": "A batch is already running."}
        b.update({
            "status": "running", "total": 0, "done": 0,
            "enrolled": 0, "failed": 0, "already": 0, "expired": 0,
            "errors": [], "started_at": _now_iso(), "finished_at": None,
        })
    import threading as _t
    _t.Thread(target=_enroll_all_worker, args=(user_id,), daemon=True).start()
    return {"ok": True}


def _enroll_all_worker(user_id: int) -> None:
    try:
        accounts = store.get_accounts(user_id, active_only=True)
        if not accounts:
            _update_batch(user_id, status="done", errors=["No active Udemy account linked."], finished_at=_now_iso())
            return
        feed = get_free_courses()
        if feed["error"] and not feed["courses"]:
            _update_batch(user_id, status="done", errors=[feed["error"]], finished_at=_now_iso())
            return
        offers = feed["courses"]
        _update_batch(user_id, total=len(offers) * len(accounts))

        grand = {"enrolled": [], "already": 0, "expired": 0, "failed": 0}
        for account in accounts:
            local_slugs = store.get_enrolled_slugs(account["id"])
            try:
                res = _enroll_account_in_courses(account, offers, local_slugs, source="manual_batch")
            except Exception as e:
                log.exception("enroll_all failed for account %s", account["id"])
                _update_batch(
                    user_id,
                    done=_get_batch(user_id)["total"],
                    errors=[f"{account.get('udemy_name') or 'Account'}: {e}"],
                    finished_at=_now_iso(),
                )
                continue

            grand["enrolled"].extend(res.get("enrolled", []))
            grand["already"] += res.get("already", 0)
            grand["expired"] += res.get("expired", 0)
            grand["failed"] += res.get("failed", 0)
            if res.get("error"):
                _update_batch(user_id, errors=_get_batch(user_id)["errors"] + [res["error"]])
            _update_batch(
                user_id,
                done=_get_batch(user_id)["done"] + len(offers),
                enrolled=len(grand["enrolled"]),
                already=grand["already"],
                expired=grand["expired"],
                failed=grand["failed"],
            )

        _update_batch(user_id, status="done", finished_at=_now_iso())
    except Exception as e:
        log.exception("enroll_all worker crashed")
        _update_batch(user_id, status="done", errors=[f"Unexpected error: {e}"], finished_at=_now_iso())


# ── Auto-enroll engine routines ─────────────────────────────────────────────

def run_auto_enroll_for_user(user_id: int, source: str = "auto") -> dict:
    """One auto-enroll pass for every active+auto account of a user."""
    accounts = [a for a in store.get_accounts(user_id, active_only=True) if a.get("auto_enroll")]
    if not accounts:
        return {"ok": False, "reason": "no_active_accounts", "enrolled": 0}
    feed = get_free_courses()
    if feed["error"] and not feed["courses"]:
        return {"ok": False, "reason": "feed_unavailable", "enrolled": 0, "error": feed["error"]}
    offers = feed["courses"]

    total_enrolled = 0
    per_account = []
    any_error = None
    for account in accounts:
        local_slugs = store.get_enrolled_slugs(account["id"])
        try:
            res = _enroll_account_in_courses(account, offers, local_slugs, source=source)
        except Exception as e:
            log.exception("auto enroll failed for account %s", account["id"])
            per_account.append({"account": account.get("udemy_name") or str(account["id"]), "error": str(e)})
            continue
        total_enrolled += len(res.get("enrolled", []))
        per_account.append({
            "account": account.get("udemy_name") or str(account["id"]),
            "enrolled": len(res.get("enrolled", [])),
            "already": res.get("already", 0),
            "expired": res.get("expired", 0),
            "failed": res.get("failed", 0),
            "error": res.get("error"),
        })
        if res.get("error") and not any_error:
            any_error = res["error"]

    return {"ok": True, "enrolled": total_enrolled, "accounts": per_account, "error": any_error}


def _next_run_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=config.AUTO_ENROLL_INTERVAL)).isoformat(timespec="seconds")


def run_auto_enroll_for_user_and_record(user_id: int) -> dict:
    """Run one pass and persist the result into auto_state (used by engine + Enroll Now)."""
    store.set_auto_running(user_id, True)
    try:
        res = run_auto_enroll_for_user(user_id, source="auto")
        if res.get("ok"):
            store.update_auto_run(
                user_id,
                last_result=f"OK — {res['enrolled']} new course(s) enrolled",
                enrolled_count=res["enrolled"],
                next_run_iso=_next_run_iso(),
            )
        else:
            store.update_auto_run(
                user_id,
                last_result=res.get("error") or res.get("reason", "skipped"),
                enrolled_count=0,
                next_run_iso=_next_run_iso(),
            )
        return res
    finally:
        store.set_auto_running(user_id, False)


def run_auto_enroll_all_users() -> int:
    """Engine tick: run for every user with auto enabled. Returns number enrolled."""
    total = 0
    rows = store._db().execute(
        "SELECT user_id FROM auto_state WHERE enabled=1 AND running=0"
    ).fetchall()
    for row in rows:
        try:
            res = run_auto_enroll_for_user_and_record(row["user_id"])
            total += res.get("enrolled", 0)
        except Exception as e:
            log.exception("auto tick failed for user %s", row["user_id"])
            store.update_auto_run(row["user_id"], f"error: {e}", 0, _next_run_iso())
    return total
