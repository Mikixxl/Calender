from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class BookingCreate(BaseModel):
    event_slug: str
    start_utc: datetime                     # the chosen slot, UTC
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    booker_timezone: str                    # IANA, auto-detected in the browser
    answers: dict = {}


class AttendanceMark(BaseModel):
    status: str                             # 'completed' | 'no_show'
