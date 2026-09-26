"""Enrollment orchestration.

One validated-checkout pipeline powers both single-course and batch/auto runs.
Batch runs publish live progress (current course, running tallies, recent wins)
so the dashboard can animate what the engine is doing in real time.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .. import config
from ..models import accounts as accounts_model
from ..models import auto_state as auto_model
from ..models import enrollments as enroll_model
from ..models import prefs as prefs_model
from .feed import get_free_courses
from .udemy_client import UdemyAutoEnroller

log = logging.getLogger(__name__)

_validator = ThreadPoolExecutor(max_workers=4, thread_name_prefix="validate")

# ── Batch-run registry (live progress) ──────────────────────────────────────
_batch_lock = threading.Lock()
_batches: dict = {}   # user_id -> progress dict
_cancel_events: dict = {}   # user_id -> threading.Event


def _cancel_event(user_id: int) -> threading.Event:
    ev = _cancel_events.get(user_id)
    if ev is None:
        ev = threading.Event()
        _cancel_events[user_id] = ev
    return ev


def stop_enroll(user_id: int) -> dict:
    """Request cancellation of the running batch for this user."""
    with _batch_lock:
        b = _get_batch(user_id)
        if b["status"] != "running":
            return {"ok": False, "error": "No run in progress."}
    _cancel_event(user_id).set()
    _update_batch(user_id, current="Stopping after the current course...")
    return {"ok": True}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _price_of(offer) -> tuple:
    """(amount, currency) parsed from a feed offer, or (None, None)."""
    raw = getattr(offer, "price", None)
    if raw in (None, ""):
        return None, None
    try:
        amount = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None, getattr(offer, "currency", None)
    if amount <= 0:
        return None, getattr(offer, "currency", None)
    return amount, getattr(offer, "currency", None) or "USD"


def _blank_batch() -> dict:
    return {
        "status": "idle", "total": 0, "done": 0,
        "enrolled": 0, "failed": 0, "already": 0, "expired": 0,
        "current": None, "recent": [], "errors": [],
        "filtered": 0, "filters": "All courses", "matching": 0,
        "started_at": None, "finished_at": None,
    }


def _get_batch(user_id: int) -> dict:
    b = _batches.get(user_id)
    if not b:
        b = _blank_batch()
        _batches[user_id] = b
    return b


def get_batch_status(user_id: int) -> dict:
    with _batch_lock:
        return dict(_get_batch(user_id))


def _update_batch(user_id: int, **fields) -> None:
    with _batch_lock:
        _get_batch(user_id).update(fields)


def _batch_add_recent(user_id: int, offer) -> None:
    item = {"title": offer.title, "at": _now_iso(),
            "category": getattr(offer, "category", None), "language": getattr(offer, "language", None)}
    with _batch_lock:
        b = _get_batch(user_id)
        b["recent"] = ([item] + b.get("recent", []))[:12]


def reset_batch(user_id: int) -> None:
    with _batch_lock:
        _batches.pop(user_id, None)


# ── Core pipeline ────────────────────────────────────────────────────────────

def _prepare_enroller(account: dict):
    enroller = UdemyAutoEnroller(
        access_token=account["access_token"],
        client_id=account.get("client_id") or None,
    )
    if not enroller.verify_login():
        return None
    enroller._get_enrolled_courses()
    # Opportunistically refresh the account's real Udemy library size (all
    # courses on the account, not just ones enrolled through this app). This
    # never runs on a page load - only alongside a run we're already making
    # network calls for - so the dashboard itself stays instant and offline-safe.
    try:
        total = enroller.get_total_courses_count()
        if isinstance(total, int) and total >= 0:
            accounts_model.set_account_total_courses(account["id"], total)
    except Exception:
        pass
    return enroller


def _validate_offers(enroller, offers, local_slugs, on_settled=None):
    """Return (free_list, coupon_list, tallies) after parallel validation.

    free_list:   [(offer, course_id, slug)]
    coupon_list: [(offer, course_id, slug, coupon)]
    """
    already = expired = failed = 0
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
    if on_settled:
        on_settled(len(offers) - len(to_process), already=already, failed=failed)

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

    free_list, coupon_list = [], []
    for fut in [_validator.submit(validate, it) for it in to_process]:
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
        if on_settled and status in ("failed", "expired"):
            on_settled(1, already=already, failed=failed, expired=expired)
        if status == "free":
            free_list.append((offer, course_id, slug))
        elif status == "valid":
            coupon_list.append((offer, course_id, slug, coupon))
    return free_list, coupon_list, {"already": already, "expired": expired, "failed": failed}


def _checkout_and_log(enroller, account, offer, course_id, slug, coupon, source) -> str:
    """Enroll one validated course and log it. Returns enrolled|already|failed."""
    try:
        if coupon is None:
            result = enroller._free_checkout(course_id)
        else:
            result = enroller._checkout_single(course_id, coupon, was_enrolled_before=False)
    except Exception as e:
        log.debug("checkout error: %s", e)
        return "failed"
    if result == "enrolled":
        price, currency = _price_of(offer)
        enroll_model.log_enrollment(
            account["user_id"], account["id"], course_id, slug, offer.title, offer.url,
            source, image=getattr(offer, "image", None), original_price=price, currency=currency,
            category=getattr(offer, "category", None), language=getattr(offer, "language", None),
        )
        return "enrolled"
    if result == "already":
        return "already"
    return "failed"


def _run_for_account(account: dict, offers: list, local_slugs: set, source: str,
                     user_id: int | None = None, live: bool = False,
                     should_stop=None) -> dict:
    """Full pipeline for one account. If live, publishes progress into the registry."""
    live = live and user_id is not None
    name = account.get("udemy_name") or "account"
    if live:
        _update_batch(user_id, phase=f"Signing in to {name}", current=None)
    enroller = _prepare_enroller(account)
    if enroller is None:
        if live:
            _bump(user_id, len(offers), failed=len(offers))
        return {"enrolled": [], "already": 0, "expired": 0, "failed": len(offers),
                "error": f"{name}: Udemy login invalid or expired - reconnect the account.",
                "stopped": False}

    if live:
        _update_batch(user_id, phase=f"Checking {len(offers)} courses for {name}")

    def settled(n, already=0, failed=0, expired=0):
        # Running totals for this account, added onto what earlier accounts did.
        with _batch_lock:
            b = _get_batch(user_id)
            base = b["_base"]
            b["done"] += n
            b["already"] = base["already"] + already
            b["failed"] = base["failed"] + failed
            b["expired"] = base["expired"] + expired

    free_list, coupon_list, tallies = _validate_offers(
        enroller, offers, local_slugs, on_settled=settled if live else None)
    enrolled = []
    already, expired, failed = tallies["already"], tallies["expired"], tallies["failed"]
    work = [(o, cid, slug, None) for (o, cid, slug) in free_list] + \
           [(o, cid, slug, coup) for (o, cid, slug, coup) in coupon_list]

    if live:
        _update_batch(user_id, phase=(f"Enrolling {len(work)} courses into {name}" if work
                                      else f"Nothing new for {name}"))

    stopped = False
    for offer, course_id, slug, coupon in work:
        if should_stop and should_stop():
            stopped = True
            break
        if live:
            _update_batch(user_id, current=offer.title)
        outcome = _checkout_and_log(enroller, account, offer, course_id, slug, coupon, source)
        if outcome == "enrolled":
            enrolled.append({"title": offer.title, "slug": slug})
            if live:
                _batch_add_recent(user_id, offer)
        elif outcome == "already":
            already += 1
        else:
            failed += 1
        if live:
            with _batch_lock:
                b = _get_batch(user_id)
                base = b["_base"]
                b["done"] += 1
                b["enrolled"] = base["enrolled"] + len(enrolled)
                b["already"] = base["already"] + already
                b["failed"] = base["failed"] + failed

    return {"enrolled": enrolled, "already": already, "expired": expired,
            "failed": failed, "error": None, "stopped": stopped}


def _bump(user_id: int, done: int, **add) -> None:
    with _batch_lock:
        b = _get_batch(user_id)
        b["done"] += done
        for k, v in add.items():
            b[k] = b.get(k, 0) + v


# ── Single course (kept for potential future direct-enroll actions) ─────────

def enroll_single(account: dict, offer) -> dict:
    enroller = _prepare_enroller(account)
    if enroller is None:
        return {"status": "failed", "message": "Udemy login invalid or expired."}
    slug = enroller._extract_slug(offer.url)
    if not slug:
        return {"status": "failed", "message": "Could not parse course URL."}
    if enroll_model.is_enrolled_by_slug(account["id"], slug) or slug in enroller.enrolled_slugs:
        return {"status": "already", "message": "Already in your Udemy account."}
    coupon = offer.coupon or enroller._extract_coupon(offer.url)
    course_id, is_free = enroller._get_course_id_from_page(slug)
    if not course_id:
        return {"status": "failed", "message": "Course not found (removed or region-locked)."}
    if not is_free and not (coupon and enroller._check_coupon(course_id, coupon)):
        return {"status": "expired", "message": "Offer no longer free (coupon expired)."}
    outcome = _checkout_and_log(enroller, account, offer, course_id, slug,
                                None if is_free else coupon, "manual")
    if outcome == "enrolled":
        return {"status": "enrolled", "message": "Enrolled! Check your Udemy account."}
    if outcome == "already":
        return {"status": "already", "message": "Already in your Udemy account."}
    detail = getattr(enroller, "last_checkout_error", "") or ""
    return {"status": "failed",
            "message": f"Udemy checkout failed - {detail}" if detail else "Udemy checkout did not succeed. Try again."}


# ── Unified live runner (manual Enroll Now and background auto-enroll) ──────

def _claim_run(user_id: int, kind: str) -> bool:
    """Mark a run as started for this user; False if one is already going."""
    with _batch_lock:
        b = _get_batch(user_id)
        if b["status"] == "running":
            return False
        b.clear()
        b.update(_blank_batch())
        b.update({"status": "running", "kind": kind, "started_at": _now_iso(),
                  "phase": "Fetching the latest free courses",
                  "_base": {"enrolled": 0, "already": 0, "failed": 0, "expired": 0}})
    _cancel_event(user_id).clear()
    return True


def _live_run(user_id: int, accts: list, source: str) -> dict:
    """Pipeline over every account with live progress. Caller must _claim_run first."""
    try:
        if not accts:
            _update_batch(user_id, status="done", finished_at=_now_iso(), current=None, phase=None,
                          errors=["No active Udemy account connected."])
            return {"ok": False, "reason": "no_active_accounts", "enrolled": 0}

        feed = get_free_courses()
        offers = feed["courses"]
        if not offers:
            err = feed.get("error") or "No courses available right now."
            _update_batch(user_id, status="done", finished_at=_now_iso(), current=None,
                          phase=None, errors=[err])
            return {"ok": False, "reason": "feed_unavailable", "enrolled": 0, "error": err}

        # Each account can have its own filters; otherwise it uses the default.
        plan, filtered_total = [], 0
        for account in accts:
            kept, skipped = prefs_model.filter_offers(offers, prefs_model.get_prefs(user_id, account["id"]))
            plan.append((account, kept))
            filtered_total += skipped
        _update_batch(user_id, total=sum(len(k) for _, k in plan), filtered=filtered_total,
                      filters=prefs_model.summary(prefs_model.get_prefs(user_id)),
                      matching=len(offers), accounts=len(accts))

        should_stop = lambda: _cancel_event(user_id).is_set()
        total_enrolled, stopped, errors = 0, False, []
        for i, (account, kept) in enumerate(plan, 1):
            if should_stop():
                stopped = True
                break
            _update_batch(user_id, account_idx=i, account_name=account.get("udemy_name") or "account")
            if not kept:
                continue
            local_slugs = enroll_model.get_enrolled_slugs(account["id"])
            try:
                res = _run_for_account(account, kept, local_slugs, source,
                                       user_id=user_id, live=True, should_stop=should_stop)
            except Exception as e:
                log.exception("run failed for account %s", account["id"])
                errors.append(str(e))
                continue
            total_enrolled += len(res.get("enrolled", []))
            with _batch_lock:
                b = _get_batch(user_id)
                b["_base"] = {"enrolled": b["enrolled"], "already": b["already"],
                              "failed": b["failed"], "expired": b["expired"]}
            if res.get("error"):
                errors.append(res["error"])
            if res.get("stopped"):
                stopped = True
                break

        _update_batch(user_id, status="stopped" if stopped else "done", finished_at=_now_iso(),
                      current=None, phase=None, enrolled=total_enrolled,
                      errors=_get_batch(user_id)["errors"] + errors)
        fb = _get_batch(user_id)
        return {"ok": True, "enrolled": total_enrolled, "filtered": filtered_total,
                "already": fb.get("already", 0), "expired": fb.get("expired", 0),
                "failed": fb.get("failed", 0),
                "error": errors[0] if errors else None, "stopped": stopped}
    except Exception as e:
        log.exception("live run crashed")
        _update_batch(user_id, status="done", finished_at=_now_iso(), current=None, phase=None,
                      errors=[f"Unexpected error: {e}"])
        return {"ok": False, "reason": "crash", "enrolled": 0, "error": str(e)}


def start_enroll_all(user_id: int) -> dict:
    if not _claim_run(user_id, "manual"):
        running_kind = get_batch_status(user_id).get("kind")
        return {"ok": False, "error": ("Auto-enroll is running right now - watch it live, or stop it first."
                                       if running_kind == "auto" else "A run is already in progress.")}
    accts = accounts_model.get_accounts(user_id, active_only=True)
    threading.Thread(target=_live_run, args=(user_id, accts, "manual_batch"), daemon=True).start()
    return {"ok": True}


def run_auto_enroll_for_user(user_id: int, source: str = "auto") -> dict:
    accts = [a for a in accounts_model.get_accounts(user_id, active_only=True) if a.get("auto_enroll")]
    if not accts:
        return {"ok": False, "reason": "no_active_accounts", "enrolled": 0}
    if not _claim_run(user_id, "auto"):
        return {"ok": False, "reason": "busy", "enrolled": 0,
                "error": "Skipped - a manual run was in progress."}
    return _live_run(user_id, accts, source)


def _next_run_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=config.AUTO_ENROLL_INTERVAL)).isoformat(timespec="seconds")


def run_auto_enroll_for_user_and_record(user_id: int) -> dict:
    auto_model.set_auto_running(user_id, True)
    try:
        res = run_auto_enroll_for_user(user_id, source="auto")
        if res.get("ok"):
            parts = [f"{res['enrolled']} new"]
            if res.get("already"):
                parts.append(f"{res['already']} already owned")
            if res.get("expired"):
                parts.append(f"{res['expired']} expired")
            if res.get("failed"):
                parts.append(f"{res['failed']} failed")
            if res.get("filtered"):
                parts.append(f"{res['filtered']} filtered out")
            msg = "OK - " + ", ".join(parts) + (" (stopped)" if res.get("stopped") else "")
            auto_model.update_auto_run(user_id, msg, res["enrolled"], _next_run_iso())
        else:
            auto_model.update_auto_run(user_id, res.get("error") or res.get("reason", "skipped"),
                                       0, _next_run_iso())
        return res
    finally:
        auto_model.set_auto_running(user_id, False)


def run_auto_enroll_all_users() -> int:
    total = 0
    # Recover from a run that crashed/hung with running=1 still set (else that
    # user would be skipped forever). Must outlast a healthy long run (feed +
    # large enroll catalog routinely exceeds 10 min) — 1h floor.
    auto_model.reset_stale_running(max(3600, config.AUTO_ENROLL_INTERVAL * 30))
    for user_id in auto_model.users_with_auto_enabled():
        try:
            total += run_auto_enroll_for_user_and_record(user_id).get("enrolled", 0)
        except Exception as e:
            log.exception("auto tick failed for user %s", user_id)
            auto_model.update_auto_run(user_id, f"error: {e}", 0, _next_run_iso())
    return total


def engine_status(user_id: int) -> dict:
    """Compact live status for the always-visible topbar indicator."""
    state = auto_model.get_auto_state(user_id)
    b = get_batch_status(user_id)
    running = b.get("status") == "running"
    next_in = None
    if state.get("next_run") and not running:
        try:
            nxt = datetime.fromisoformat(state["next_run"])
            next_in = max(0, int((nxt - datetime.now(timezone.utc)).total_seconds()))
        except ValueError:
            next_in = None
    return {
        "enabled": bool(state.get("enabled")),
        "running": running,
        "kind": b.get("kind") if running else None,
        "phase": b.get("phase") if running else None,
        "current": b.get("current") if running else None,
        "done": b.get("done", 0), "total": b.get("total", 0),
        "enrolled": b.get("enrolled", 0),
        "next_in": next_in,
        "interval": config.AUTO_ENROLL_INTERVAL,
        "last_result": state.get("last_result"),
    }
