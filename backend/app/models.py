from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class Participant(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = None


class BookingCreate(BaseModel):
    event_slug: str
    start_utc: datetime                     # the chosen slot, UTC
    name: str = Field(min_length=1, max_length=200)   # primary contact
    email: EmailStr                                    # confirmation recipient
    booker_timezone: str                    # IANA, auto-detected in the browser
    guests: list[Participant] = []          # additional named attendees
    answers: dict = {}


class AttendanceMark(BaseModel):
    status: str                             # 'completed' | 'no_show'
