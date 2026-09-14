from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, HTTPException, Request as FastAPIRequest
from pydantic import BaseModel, Field

from . import patched as patched_module
from .conversation import Intent
from .models import ConversationCaseLink, ConversationMessage, ConversationThread, ExternalCase, FollowupTask

app = patched_module.app

try:
    _CAIRO = ZoneInfo("Africa/Cairo")
except ZoneInfoNotFoundError:
    _CAIRO = timezone(timedelta(hours=3))

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_REMINDER_CUE_RE = re.compile(r"(?:فكرني|فكّرني|ذكرني|ذكّرني|تذكير|remind\s+me)", re.IGNORECASE)
_WEEKDAYS = {
    "الاثنين": 0, "الإثنين": 0, "الثلاثاء": 1,
    "الاربعاء": 2, "الأربعاء": 2, "الخميس": 3,
    "الجمعة": 4, "السبت": 5, "الاحد": 6, "الأحد": 6,
}


def _normal_digits(text: str) -> str:
    return (text or "").translate(_AR_DIGITS)


def _parse_reminder_due(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(_CAIRO)
    value = _normal_digits(" ".join((text or "").split()))
    lower = value.casefold()

    if re.search(r"بعد\s+(?:نص|نصف)\s+ساع", lower):
        return now + timedelta(minutes=30)

    relative = re.search(
        r"بعد\s+(\d{1,4})\s*(دقيقه|دقيقة|دقايق|دقائق|ساعه|ساعة|ساعات|يوم|ايام|أيام)",
        lower,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if "د" in unit:
            return now + timedelta(minutes=amount)
        if "ساع" in unit:
            return now + timedelta(hours=amount)
        return now + timedelta(days=amount)

    if re.search(r"بعد\s+ساعتين", lower):
        return now + timedelta(hours=2)
    if re.search(r"بعد\s+ساع", lower):
        return now + timedelta(hours=1)

    base_date = now.date()
    explicit_future_date = False
    if any(word in lower for word in ("بكره", "بكرة", "غدا", "غداً")):
        base_date = (now + timedelta(days=1)).date()
        explicit_future_date = True
    else:
        for word, weekday in _WEEKDAYS.items():
            if word in value:
                delta = (weekday - now.weekday()) % 7
                if delta == 0:
                    delta = 7
                base_date = (now + timedelta(days=delta)).date()
                explicit_future_date = True
                break

    time_match = re.search(
        r"(?:الساعه|الساعة|at)\s*(\d{1,2})(?::(\d{2}))?\s*(ص|م|am|pm)?",
        lower,
        re.IGNORECASE,
    )
    if not time_match:
        time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))\s*(ص|م|am|pm)\b", lower, re.IGNORECASE)
    if not time_match:
        return None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    meridiem = (time_match.group(3) or "").casefold()
    if meridiem in {"م", "pm"} or any(x in lower for x in ("مساء", "بالليل", "ليل")):
        if hour < 12:
            hour += 12
    elif meridiem in {"ص", "am"} or any(x in lower for x in ("صباح", "الصبح")):
        if hour == 12:
            hour = 0
    elif hour <= 12 and not explicit_future_date:
        morning_hour = 0 if hour == 12 else hour
        evening_hour = 12 if hour == 12 else hour + 12
        candidates = [
            datetime.combine(base_date, datetime.min.time(), tzinfo=_CAIRO).replace(hour=morning_hour, minute=minute),
            datetime.combine(base_date, datetime.min.time(), tzinfo=_CAIRO).replace(hour=evening_hour, minute=minute),
            datetime.combine(base_date + timedelta(days=1), datetime.min.time(), tzinfo=_CAIRO).replace(hour=morning_hour, minute=minute),
        ]
        future = [candidate for candidate in candidates if candidate > now]
        return min(future) if future else None

    target = datetime.combine(base_date, datetime.min.time(), tzinfo=_CAIRO).replace(hour=hour, minute=minute)
    if not explicit_future_date and target <= now:
        target += timedelta(days=1)
    return target


def _reminder_action(text: str) -> str:
    value = " ".join((text or "").split())
    value = _REMINDER_CUE_RE.sub("", value)
    value = re.sub(
        r"(?:النهارده|اليوم|بكره|بكرة|غدا|غداً|بعد\s+(?:نص|نصف)?\s*\d*\s*(?:دقيقه|دقيقة|دقايق|دقائق|ساعه|ساعة|ساعتين|ساعات|يوم|ايام|أيام)|(?:الساعه|الساعة)\s+\d{1,2}(?::\d{2})?\s*(?:ص|م|am|pm)?)",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value).strip(" ،.-")
    for prefix in ("اني ", "إني ", "ان ", "إن "):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    return value[:240] or "التذكير اللي طلبته"


def _format_due_ar(target: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(_CAIRO)
    local = target.astimezone(_CAIRO)
    suffix = "ص" if local.hour < 12 else "م"
    display_hour = local.hour % 12 or 12
    time_text = f"{display_hour}:{local.minute:02d} {suffix}"
    if local.date() == now.date():
        return f"النهارده الساعة {time_text}"
    if local.date() == (now + timedelta(days=1)).date():
        return f"بكرة الساعة {time_text}"
    return f"{local.day:02d}/{local.month:02d}/{local.year} الساعة {time_text}"


class ReminderChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=1600)
    display_message: str | None = Field(default=None, max_length=800)
    thread_id: str | None = None
    locale: str = Field(default="ar-EG", max_length=20)


def _reminder_thread(db, customer_ref: str, thread_id: str | None, locale_code: str):
    locale = patched_module.main_module.resolve_locale(locale_code)
    thread = None
    if thread_id:
        thread = db.query(ConversationThread).filter(
            ConversationThread.id == thread_id,
            ConversationThread.customer_ref == customer_ref,
        ).first()
        if not thread:
            raise HTTPException(404, "Conversation not found")
    if not thread:
        thread = ConversationThread(
            id=str(uuid.uuid4()),
            customer_ref=customer_ref,
            locale=locale.locale,
            language=locale.language,
            region=locale.region,
            currency=locale.currency,
        )
        db.add(thread)
        db.flush()
    return thread, locale


@app.post("/api/reminders/chat")
def reminder_chat(
    payload: ReminderChatInput,
    request: FastAPIRequest,
    db=Depends(patched_module.main_module.get_db),
):
    customer_ref = patched_module.main_module.customer_ref_from_request(request)
    thread, locale = _reminder_thread(db, customer_ref, payload.thread_id, payload.locale)
    display_message = (payload.display_message or payload.message).strip()
    user_message = ConversationMessage(thread_id=thread.id, role="USER", content=display_message)
    db.add(user_message)
    db.flush()

    due = _parse_reminder_due(payload.message)
    if due is None:
        reply_text = "تمام، أقدر أسجله. أفكرك إمتى بالظبط؟"
        assistant_message = ConversationMessage(
            thread_id=thread.id,
            role="ASSISTANT",
            content=reply_text,
            intent=Intent.CONTINUATION.value,
        )
        db.add(assistant_message)
        db.commit()
        return {
            "thread_id": thread.id,
            "message": {"id": assistant_message.id, "role": "assistant", "content": reply_text},
            "reminder": {"created": False, "needs_time": True},
        }

    action = _reminder_action(payload.message)
    due_utc_naive = due.astimezone(timezone.utc).replace(tzinfo=None)
    case = ExternalCase(
        customer_ref=customer_ref,
        locale=locale.locale,
        region=locale.region,
        currency=locale.currency,
        source_text=payload.message,
        title=f"تذكير: {action[:80]}",
        expected_at=due.isoformat(),
        status="FOLLOWING",
        outcome_status="OPEN",
    )
    db.add(case)
    db.flush()
    task = FollowupTask(
        external_case_id=case.id,
        trigger_event="TIME_REMINDER",
        action=action,
        due_at=due_utc_naive,
        status="PENDING",
    )
    db.add(task)
    db.add(ConversationCaseLink(thread_id=thread.id, case_type="EXTERNAL", case_id=case.id))
    reply_text = (
        f"تمام، سجلته: «{action}» — {_format_due_ar(due)}. "
        "لو الصفحة مفتوحة وقتها هطلعلك تنبيه، ولو كانت مقفولة هتشوفه أول ما ترجع."
    )
    assistant_message = ConversationMessage(
        thread_id=thread.id,
        role="ASSISTANT",
        content=reply_text,
        intent=Intent.CONTINUATION.value,
    )
    db.add(assistant_message)
    db.commit()
    db.refresh(task)
    return {
        "thread_id": thread.id,
        "message": {"id": assistant_message.id, "role": "assistant", "content": reply_text},
        "reminder": {
            "created": True,
            "needs_time": False,
            "id": task.id,
            "due_at": due.isoformat(),
            "text": action,
        },
    }


@app.get("/api/reminders/due")
def due_reminders(
    request: FastAPIRequest,
    db=Depends(patched_module.main_module.get_db),
):
    customer_ref = patched_module.main_module.customer_ref_from_request(request)
    now = datetime.utcnow()
    rows = (
        db.query(FollowupTask, ExternalCase)
        .join(ExternalCase, ExternalCase.id == FollowupTask.external_case_id)
        .filter(
            FollowupTask.status == "PENDING",
            FollowupTask.due_at <= now,
            ExternalCase.customer_ref == customer_ref,
        )
        .order_by(FollowupTask.due_at.asc())
        .limit(10)
        .all()
    )
    return {
        "items": [
            {
                "id": task.id,
                "text": task.action,
                "title": case.title,
                "due_at": task.due_at.isoformat() + "Z",
            }
            for task, case in rows
        ]
    }


@app.post("/api/reminders/{task_id}/ack")
def acknowledge_reminder(
    task_id: int,
    request: FastAPIRequest,
    db=Depends(patched_module.main_module.get_db),
):
    customer_ref = patched_module.main_module.customer_ref_from_request(request)
    row = (
        db.query(FollowupTask, ExternalCase)
        .join(ExternalCase, ExternalCase.id == FollowupTask.external_case_id)
        .filter(
            FollowupTask.id == task_id,
            ExternalCase.customer_ref == customer_ref,
        )
        .first()
    )
    if not row:
        raise HTTPException(404, "Reminder not found")
    task, case = row
    if task.status == "PENDING":
        link = (
            db.query(ConversationCaseLink)
            .filter(
                ConversationCaseLink.case_type == "EXTERNAL",
                ConversationCaseLink.case_id == case.id,
            )
            .order_by(ConversationCaseLink.id.desc())
            .first()
        )
        if link:
            db.add(
                ConversationMessage(
                    thread_id=link.thread_id,
                    role="ASSISTANT",
                    content=f"⏰ تذكير: {task.action}",
                    intent=Intent.CONTINUATION.value,
                )
            )
        task.status = "DONE"
        case.status = "VERIFIED_OUTCOME"
        case.outcome_status = "VERIFIED"
        db.commit()
    return {"ok": True}
