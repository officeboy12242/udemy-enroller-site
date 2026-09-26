"""Linked Udemy accounts (access tokens encrypted at rest)."""
from .. import security
from ..db import get_db, db_lock, now_iso


def _row_with_token(row) -> dict:
    d = dict(row)
    try:
        d["access_token"] = security.decrypt_secret(d.pop("access_token_enc"))
    except Exception:
        d["access_token"] = ""
        d.pop("access_token_enc", None)
    return d


def upsert_account(user_id: int, access_token: str, client_id: str,
                   udemy_user_id: int | None, udemy_name: str | None) -> int:
    """Create or refresh the account record for this Udemy identity. Returns account id."""
    now = now_iso()
    enc = security.encrypt_secret(access_token)
    with db_lock():
        db = get_db()
        row = db.execute(
            "SELECT id FROM accounts WHERE user_id=? AND udemy_user_id=?",
            (user_id, udemy_user_id),
        ).fetchone() if udemy_user_id is not None else None
        if row:
            db.execute(
                "UPDATE accounts SET access_token_enc=?, client_id=?, udemy_name=?, is_active=1, updated_at=? WHERE id=?",
                (enc, client_id, udemy_name, now, row["id"]),
            )
            acc_id = row["id"]
        else:
            dup = db.execute(
                "SELECT user_id FROM accounts WHERE udemy_user_id=? AND user_id<>?",
                (udemy_user_id, user_id),
            ).fetchone() if udemy_user_id is not None else None
            if dup:
                raise ValueError("This Udemy account is already linked to another site user.")
            cur = db.execute(
                """INSERT INTO accounts
                   (user_id, udemy_user_id, udemy_name, access_token_enc, client_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (user_id, udemy_user_id, udemy_name, enc, client_id, now, now),
            )
            acc_id = cur.lastrowid
        db.commit()
    return acc_id


def get_accounts(user_id: int, active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM accounts WHERE user_id=?"
    if active_only:
        q += " AND is_active=1"
    q += " ORDER BY id"
    return [_row_with_token(r) for r in get_db().execute(q, (user_id,)).fetchall()]


def get_account_by_id(account_id: int) -> dict | None:
    r = get_db().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return _row_with_token(r) if r else None


def get_all_active_auto_accounts() -> list[dict]:
    rows = get_db().execute(
        "SELECT a.*, u.email AS site_email FROM accounts a JOIN users u ON u.id=a.user_id "
        "WHERE a.is_active=1 AND a.auto_enroll=1 ORDER BY a.id"
    ).fetchall()
    return [_row_with_token(r) for r in rows]


def set_account_flags(account_id: int, is_active: bool = None, auto_enroll: bool = None) -> None:
    sets, vals = [], []
    if is_active is not None:
        sets.append("is_active=?"); vals.append(int(is_active))
    if auto_enroll is not None:
        sets.append("auto_enroll=?"); vals.append(int(auto_enroll))
    if not sets:
        return
    sets.append("updated_at=?"); vals.append(now_iso()); vals.append(account_id)
    with db_lock():
        db = get_db()
        db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)
        db.commit()


def set_account_total_courses(account_id: int, count: int) -> None:
    """Cache the Udemy account's real course-library size (all courses in the
    account, not just ones enrolled through this app). Refreshed opportunistically
    during auto/manual enroll runs - never fetched live on a page load."""
    with db_lock():
        db = get_db()
        db.execute(
            "UPDATE accounts SET total_courses=?, total_courses_updated_at=? WHERE id=?",
            (count, now_iso(), account_id),
        )
        db.commit()


def delete_account(account_id: int, user_id: int) -> bool:
    with db_lock():
        db = get_db()
        cur = db.execute("DELETE FROM accounts WHERE id=? AND user_id=?", (account_id, user_id))
        db.commit()
    return cur.rowcount > 0
