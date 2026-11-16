"""Orbi chat, rule-based for now (RULE_DERIVED).

`reply_to` picks an answer from keywords in the astronaut's message and from the
day's evaluation (sleep, mood, exercise, nutrition). There is no language model
here: when one is added, it replaces `reply_to` behind the same route.

Modes: "journal" stores both messages and a journal entry; "private" stores nothing.
"""

from __future__ import annotations

import re
import unicodedata

from app.cosmo import logic

# Checked in order; the first topic whose words appear in the message wins.
TOPICS: list[tuple[str, tuple[str, ...]]] = [
    ("crisis", ("hurt myself", "kill myself", "end my life", "suicide", "suicidal", "no reason to live", "can't go on",
                "cant go on", "don't want to live", "dont want to live", "want to die", "self harm", "end it all",
                "better off without me", "no point in living", "not worth living")),
    ("urgent", ("chest pain", "can't breathe", "cant breathe", "cannot breathe", "short of breath", "fainted",
                "passed out", "severe pain", "bleeding a lot", "heavy bleeding", "can't feel my")),
    ("sleep", ("sleep", "slept", "insomnia", "awake", "woke", "nap", "nightmare")),
    ("tired", ("tired", "exhausted", "fatigue", "drained", "no energy", "worn out")),
    ("stress", ("stress", "anxious", "anxiety", "overwhelmed", "overloaded", "pressure", "worried", "nervous", "panic")),
    ("lonely", ("lonely", "alone", "miss my", "homesick", "family", "isolated", "my kids", "my wife", "my husband")),
    ("sad", ("sad", "down", "depressed", "unhappy", "crying", "hopeless", "bad day")),
    ("pain", ("pain", "hurts", "ache", "injury", "injured", "sore", "bruise", "headache")),
    ("food", ("food", "meal", "eat", "eating", "hungry", "appetite", "menu", "taste")),
    ("exercise", ("exercise", "workout", "treadmill", "training", "gym", "resistive", "cycle")),
    ("radiation", ("radiation", "dose", "dosimeter")),
    ("crew", ("crew", "conflict", "argument", "annoyed", "angry", "frustrated")),
    ("good", ("good", "great", "happy", "fine", "better", "excited", "proud", "calm", "rested")),
    ("thanks", ("thanks", "thank you")),
    ("greeting", GREETINGS := ("hello", "hi", "hey", "hiya", "good morning", "good afternoon", "good evening",
                               "good night", "how are you", "how's it going", "whats up", "what's up", "oi", "ola",
                               "bom dia", "boa tarde", "boa noite", "tudo bem")),
]
# "not good", "don't feel well", "not feeling great"...: a low day, not a good one.
NEGATED_GOOD = re.compile(r"\b(not|never|no longer|don't|dont|didn't|didnt|isn't|wasn't|ain't)\s+(really\s+|so\s+|very\s+|that\s+|"
                          r"feeling\s+|feel\s+|doing\s+|been\s+|too\s+)*(good|great|fine|well|ok|okay|happy|better|rested|calm)\b")
FILLER = {"orbi", "there", "again", "you", "today", "friend", "buddy", "and", "so", "how", "are", "doing"}

NEGATIVE = {"crisis", "urgent", "tired", "stress", "lonely", "sad", "pain", "crew"}
MOOD_HINT = {"good": 4, "thanks": None, "greeting": None, "sleep": None, "tired": 2, "stress": 1, "lonely": 2,
             "sad": 2, "pain": 2, "crew": 2, "crisis": 1, "urgent": 2}


def normalize(text: str) -> str:
    """Lower case, no accents, straight apostrophes, single spaces, padded (for word matching)."""
    t = unicodedata.normalize("NFKD", text.replace("\u2019", "'").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " " + " ".join(re.sub(r"[^a-z' ]+", " ", t).split()) + " "


def _has(t: str, words) -> bool:
    return any(f" {w} " in t for w in words)


def is_greeting(text: str) -> bool:
    """A message that is (almost) only a greeting: "hi", "good morning Orbi", "oi, tudo bem?"."""
    t = normalize(text)
    if not _has(t, GREETINGS):
        return False
    rest = t
    for g in sorted(GREETINGS, key=len, reverse=True):
        rest = rest.replace(f" {g} ", " ")
    return len([w for w in rest.split() if w not in FILLER]) <= 1


def topic_of(text: str) -> str:
    t = normalize(text)
    for topic, words in TOPICS[:2]:  # danger first, always
        if _has(t, words):
            return topic
    if is_greeting(text):
        return "greeting"
    if NEGATED_GOOD.search(t):
        return "sad"
    for g in ("good morning", "good afternoon", "good evening", "good night"):
        t = t.replace(f" {g} ", " ")  # a greeting inside a longer message is not "good"
    for topic, words in TOPICS[2:]:
        if _has(t, words):
            return topic
    return "other"


def part_of_day(hour: int) -> str:
    return "Good morning" if 5 <= hour < 12 else "Good afternoon" if 12 <= hour < 18 else "Good evening"


def reply_to(text: str, first_name: str, ev: logic.DayEvaluation | None, hour: int | None = None) -> tuple[str, str]:
    """Return (reply, topic). `hour`: station time (UTC), for greetings."""
    topic = topic_of(text)
    dom = ev.domains if ev else {}
    sleep = dom["sleep"].detail if ev and dom["sleep"].detail else {}
    fitness = dom["fitness"].detail if ev and dom["fitness"].detail else {}
    nutrition = dom["nutrition"].detail if ev and dom["nutrition"].detail else {}

    if topic == "crisis":
        return (f"{first_name}, I'm glad you told me. You don't have to carry this alone: please talk to your flight "
                "surgeon or a crewmate right now. You can send an urgent message to Mission Control from the Messages "
                "screen, and I can stay here with you while you do."), topic
    if topic == "urgent":
        return ("That can be serious. Stop what you are doing, tell a crewmate and send an urgent message to Mission "
                "Control now. If you log it as a severe injury or symptom, the medical team is flagged right away."), topic
    if topic == "sleep":
        if sleep.get("avgHours") is not None:
            return (f"Your last nights averaged {sleep['avgHours']:.1f} h (goal {sleep.get('goalHours', 7)} h). "
                    "On station, dimming lights and screens an hour before bed and keeping the same sleep window "
                    "helps most. Want to tell me what kept you up?"), topic
        return ("Sleep is harder in orbit: about 16 sunrises a day confuse the body clock. A fixed sleep window and "
                "dim lights before bed help. How many hours did you get?"), topic
    if topic == "tired":
        extra = f" Your sleep has averaged {sleep['avgHours']:.1f} h lately, which fits." if sleep.get("avgHours") and sleep["avgHours"] < 6.5 else ""
        return (f"That sounds draining.{extra} A short break, water and a proper meal can help today; if the "
                "tiredness lasts a few days, it is worth telling the medical team."), topic
    if topic == "stress":
        return ("That is a lot to hold. Try one slow minute: breathe in for 4, hold for 4, out for 6. Then pick the "
                "one task that matters most now. What is weighing on you the most?"), topic
    if topic == "lonely":
        return ("Missing home is one of the hardest parts of a mission, and it is normal. A call or a recorded message "
                "to family often helps; so does a meal with the crew. Who would you like to hear from?"), topic
    if topic == "sad":
        return ("I'm sorry today is heavy. Low days happen on long missions and they pass. Would it help to talk "
                "about what happened, or to ask Mission Control for a conversation with the support team?"), topic
    if topic == "pain":
        return ("Thanks for telling me. Please log it under Body so the medical team can see where and how strong it "
                "is; if it is severe, it is flagged to the ground at once. How strong is it from 1 to 5?"), topic
    if topic == "food":
        if nutrition.get("planText"):
            return (f"You have eaten about {nutrition['planText']} this week. If the menu is getting dull, you can "
                    "swap a planned meal for another item from the station catalog and still log it."), topic
        return ("Eating enough matters for energy and bones up here. If the menu is getting dull, try a swap from "
                "the station catalog. What did you feel like eating?"), topic
    if topic == "exercise":
        if fitness.get("activeDays") is not None:
            return (f"You exercised 60 minutes or more on {fitness['activeDays']} of the last {fitness['windowDays']} "
                    f"days (goal {fitness.get('goalMinPerDay', 90)} min a day). Exercise is the main protection for "
                    "muscle and bone in microgravity. How did the last session feel?"), topic
        return ("Exercise is the main protection for muscle and bone in microgravity. How did your last session feel?"), topic
    if topic == "radiation":
        return ("Your dosimeter readings are in the Radiation log. The values in this demo are illustrative; the "
                "medical team follows your real dose over the mission."), topic
    if topic == "crew":
        return ("Living this close for weeks makes friction normal. Naming it early, calmly and in private usually "
                "works best. Do you want to think through what you would say?"), topic
    if topic == "good":
        return (f"Good to hear, {first_name}. What made today work? It helps to notice it so you can repeat it."), topic
    if topic == "thanks":
        return ("Any time. I'm here whenever you want to talk."), topic
    if topic == "greeting":
        hello = part_of_day(hour) if hour is not None else "Hi"
        if sleep.get("avgHours") is not None and sleep["avgHours"] < 6:
            hook = f" Your last nights averaged {sleep['avgHours']:.1f} h of sleep. How are you feeling today?"
        elif ev and ev.domains.get("mood") and ev.domains["mood"].level in ("warn", "alert"):
            hook = " The last few days looked heavy in your check-ins. How are you feeling now?"
        else:
            hook = " Good to hear from you. How are you feeling today?"
        return f"{hello}, {first_name}!{hook}", topic
    return ("I'm listening. Tell me a bit more about how you are feeling, or about your day."), topic


def journal_summary(text: str) -> str:
    """Short journal line from the astronaut's own words (first sentences, at most 200 characters)."""
    clean = " ".join(text.split())
    if len(clean) <= 200:
        return clean
    cut = clean[:200]
    return (cut[:cut.rfind(" ")] if " " in cut else cut) + "…"
