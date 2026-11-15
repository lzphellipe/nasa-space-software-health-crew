"""CosmoMedic update: refresh token, TEST and GROUND accounts, shop windows, Orbi chat, uploads, meal plan."""

import pytest

API = "/api/v1/app"


def login(client, crew_id: str, pin: str) -> dict:
    r = client.post(f"{API}/auth/login", json={"crewId": crew_id, "pin": pin})
    assert r.status_code == 200, r.text
    return r.json()


def auth(body: dict) -> dict:
    return {"Authorization": f"Bearer {body['accessToken']}"}


@pytest.fixture(scope="module")
def test_user(client):
    return auth(login(client, "TEST", "0000"))


@pytest.fixture(scope="module")
def mika(client):
    return auth(login(client, "ORB3-MIKA", "2026"))


@pytest.fixture(scope="module")
def ground(client):
    return auth(login(client, "GROUND", "2026"))


# --- 1-4: accounts and tokens ------------------------------------------------------------------


def test_app_login_returns_a_refresh_token_that_rotates(client):
    body = login(client, "ORB3-ANA", "2026")
    assert body["role"] == "astronaut" and body["expiresIn"] == 1800 and body["refreshExpiresIn"] > 86400
    renewed = client.post("/api/v1/auth/refresh", json={"refreshToken": body["refreshToken"]})
    assert renewed.status_code == 200
    new = renewed.json()
    assert new["refreshToken"] != body["refreshToken"]
    assert client.get(f"{API}/me/home", headers={"Authorization": f"Bearer {new['accessToken']}"}).status_code == 200
    # a used refresh token cannot be used again
    assert client.post("/api/v1/auth/refresh", json={"refreshToken": body["refreshToken"]}).status_code == 401


def test_test_account_has_coins_for_the_whole_shop(client, test_user):
    body = login(client, "test", "0000")
    assert body["astronaut"]["onboardingDone"] is True and body["next"] == "/home"
    view = client.get(f"{API}/me/avatar", headers=test_user).json()
    assert view["avatar"]["coins"] >= 999_999  # starts at 999,999; other tests may add coins
    total = sum(i["price"] for group in view["shop"].values() for i in group)
    assert total < 999_999
    home = client.get(f"{API}/me/home", headers=test_user).json()
    assert home["logsTotal"] == 6


def test_test_account_is_not_in_the_crew_table_or_ranking(client, test_user, mika, ground):
    crew = client.get(f"{API}/ground/crew", headers=ground).json()["crew"]
    assert [r["astronaut"]["id"] for r in crew] == ["ast-01", "ast-02", "ast-03", "ast-04"]
    assert all({"statusLevel", "logsTotal", "nutritionPlanPct"} <= set(r) for r in crew)
    assert len(client.get(f"{API}/leaderboard", headers=mika).json()["ranking"]) == 4
    assert len(client.get(f"{API}/leaderboard", headers=test_user).json()["ranking"]) == 5


def test_ground_logs_in_through_the_app_login(client, ground, mika):
    body = login(client, "ground", "2026")
    assert body["role"] == "ground" and body["astronaut"] is None and body["next"] == "/ground"
    assert client.get(f"{API}/ground/alerts", headers=ground).status_code == 200
    assert client.get(f"{API}/me/home", headers=ground).status_code == 403
    assert client.get(f"{API}/ground/crew", headers=mika).status_code == 403


# --- 7: shop windows ----------------------------------------------------------------------------


def test_new_windows_can_be_bought_and_equipped(client, test_user):
    windows = {i["id"]: i for i in client.get(f"{API}/shop", params={"category": "window"}).json()}
    expected = {"window-beach": ("Beach", 400), "window-field": ("Countryside", 350),
                "window-mountains": ("Mountains", 450), "window-city": ("City lights", 500)}
    for item_id, (name, price) in expected.items():
        assert (windows[item_id]["name"], windows[item_id]["price"]) == (name, price)
    before = client.get(f"{API}/me/avatar", headers=test_user).json()["avatar"]["coins"]
    bought = client.post(f"{API}/me/avatar/purchase", headers=test_user, json={"itemId": "window-city"}).json()
    assert bought["avatar"]["coins"] == before - 500
    equipped = client.post(f"{API}/me/avatar/equip", headers=test_user, json={"itemId": "window-city"}).json()
    assert equipped["avatar"]["equipped"]["room"]["window"] == "window-city"


# --- 8: Orbi chat -------------------------------------------------------------------------------


def test_orbi_history_starts_with_the_demo_conversation(client):
    ana = auth(login(client, "ORB3-ANA", "2026"))
    messages = client.get(f"{API}/me/orbi/messages", headers=ana).json()
    assert messages[0]["id"] == "msg-001" and messages[0]["author"] == "orbi"
    assert {m["astronautId"] for m in messages} == {"ast-01"}


def test_orbi_journal_mode_saves_messages_and_a_journal_entry(client, test_user):
    r = client.post(f"{API}/me/orbi/chat", headers=test_user,
                    json={"text": "I slept badly again and feel tired", "mode": "journal"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["topic"] == "sleep" and "sleep" in body["reply"].lower() and body["provenance"] == "RULE_DERIVED"
    assert body["journalEntry"]["summary"] == "I slept badly again and feel tired"
    history = client.get(f"{API}/me/orbi/messages", headers=test_user).json()
    assert [m["author"] for m in history[-2:]] == ["astronaut", "orbi"]
    assert history[-1]["text"] == body["reply"]
    journal = client.get(f"{API}/me/journal", headers=test_user).json()
    assert journal[0]["id"] == body["journalEntry"]["id"]


def test_orbi_private_mode_saves_nothing(client, test_user):
    before = client.get(f"{API}/me/orbi/messages", headers=test_user).json()
    journal_before = client.get(f"{API}/me/journal", headers=test_user).json()
    r = client.post(f"{API}/me/orbi/chat", headers=test_user,
                    json={"text": "I am anxious about tomorrow", "mode": "private"})
    body = r.json()
    assert r.status_code == 200 and body["topic"] == "stress" and body["reply"]
    assert body["saved"] is False and body["journalEntry"] is None and body["messages"] == []
    assert client.get(f"{API}/me/orbi/messages", headers=test_user).json() == before
    assert client.get(f"{API}/me/journal", headers=test_user).json() == journal_before


def test_orbi_sends_serious_messages_to_people(client, test_user):
    body = client.post(f"{API}/me/orbi/chat", headers=test_user,
                       json={"text": "sometimes I think about how to end my life", "mode": "private"}).json()
    assert body["topic"] == "crisis" and "flight surgeon" in body["reply"]
    assert client.post(f"{API}/me/orbi/chat", headers=test_user, json={"text": "  ", "mode": "x"}).status_code == 422


# --- 9: uploads and voice note -----------------------------------------------------------------


def test_upload_voice_note_and_use_it_in_a_mood_log(client, test_user):
    up = client.post(f"{API}/me/uploads", headers=test_user,
                     files={"file": ("note.m4a", b"\x00\x00\x00\x18ftypM4A fake audio", "audio/mp4")})
    assert up.status_code == 201, up.text
    url = up.json()["url"]
    assert url.startswith("http") and up.json()["path"].endswith(".m4a") and up.json()["kind"] == "audio"
    got = client.get(up.json()["path"])
    assert got.status_code == 200 and got.content.startswith(b"\x00\x00\x00\x18ftyp")
    assert got.headers["content-type"].startswith("audio/")

    r = client.post(f"{API}/me/logs", headers=test_user, json={"type": "mood", "data": {
        "level": 3, "tags": ["calm"], "voiceNote": {"url": url, "durationSec": 12.5, "transcript": "Long day."}}})
    assert r.status_code == 201, r.text
    assert r.json()["log"]["data"]["voiceNote"] == {"url": url, "durationSec": 12.5, "transcript": "Long day."}


def test_upload_rejects_other_types_big_files_and_odd_names(client, test_user):
    assert client.post(f"{API}/me/uploads", headers=test_user,
                       files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 415
    big = b"0" * (10 * 1024 * 1024 + 1)
    assert client.post(f"{API}/me/uploads", headers=test_user,
                       files={"file": ("big.jpg", big, "image/jpeg")}).status_code == 413
    # the default test client is the flight surgeon, who has no crew profile
    assert client.post(f"{API}/me/uploads", files={"file": ("a.jpg", b"x", "image/jpeg")}).status_code == 403
    assert client.get(f"{API}/uploads/..%2F..%2Fcrew_health.db").status_code == 404
    assert client.get(f"{API}/uploads/{'a' * 32}.jpg").status_code == 404


def test_injury_photo_url(client, test_user):
    up = client.post(f"{API}/me/uploads", headers=test_user, files={"file": ("p.png", b"\x89PNG fake", "image/png")})
    r = client.post(f"{API}/me/logs", headers=test_user, json={"type": "injury", "data": {
        "bodyPart": "hand", "side": "right", "kind": "cut", "severity": "minor", "pain": 1,
        "description": "Small cut", "photoUrl": up.json()["url"], "status": "healing"}})
    assert r.status_code == 201 and r.json()["log"]["data"]["photoUrl"] == up.json()["url"]


# --- 10: meal plan ------------------------------------------------------------------------------


def test_food_catalog(client):
    foods = client.get(f"{API}/foods").json()
    assert len(foods) >= 20
    assert set(foods[0]) == {"id", "name", "servingG", "kcal", "proteinG", "carbsG", "fatG"}
    rice = client.get(f"{API}/foods", params={"q": "RICE"}).json()
    assert [f["id"] for f in rice] == ["food-rice"]


def test_meal_plan_of_the_day(client, test_user):
    plan = client.get(f"{API}/me/meal-plan", headers=test_user, params={"day": 12}).json()
    assert plan["missionDay"] == 12 and [m["slot"] for m in plan["meals"]] == ["breakfast", "lunch", "snack", "dinner"]
    meal = plan["meals"][0]
    assert {"id", "slot", "time", "name", "items", "kcal", "proteinG", "carbsG", "fatG", "logged"} <= set(meal)
    assert 2200 <= plan["totals"]["kcal"] <= 2800
    assert plan["eatenPct"] == 0 and meal["logged"] is None
    # default day is today
    assert client.get(f"{API}/me/meal-plan", headers=test_user).json()["missionDay"] == 12


def test_log_planned_meals_followed_partial_swapped_skipped(client, test_user):
    plan = client.get(f"{API}/me/meal-plan", headers=test_user).json()
    meals = {m["slot"]: m for m in plan["meals"]}
    post = lambda data: client.post(f"{API}/me/logs", headers=test_user, json={"type": "meal", "data": data})

    followed = post({"planMealId": meals["breakfast"]["id"], "adherence": "followed"})
    assert followed.status_code == 201, followed.text
    d = followed.json()["log"]["data"]
    assert d["kcal"] == meals["breakfast"]["kcal"] and d["eatenPct"] == 100 and d["name"] == meals["breakfast"]["name"]
    assert "tasteRating" not in d and followed.json()["coinsEarned"] == 10

    partial = post({"planMealId": meals["lunch"]["id"], "adherence": "partial", "eatenPct": 50, "tasteRating": 2})
    d = partial.json()["log"]["data"]
    assert d["kcal"] == round(meals["lunch"]["kcal"] * 0.5, 1) and d["proteinG"] == round(meals["lunch"]["proteinG"] * 0.5, 1)

    swapped = post({"planMealId": meals["snack"]["id"], "adherence": "swapped",
                    "swaps": [{"foodId": "food-rice", "servings": 2}, {"foodId": "food-salmon", "servings": 1}],
                    "kcal": 4999})  # the client's kcal is ignored
    d = swapped.json()["log"]["data"]
    assert d["kcal"] == 2 * 210 + 150 and d["carbsG"] == 88 and d["name"] == "Rice pilaf + Pink salmon"
    assert d["swaps"][0] == {"foodId": "food-rice", "servings": 2, "name": "Rice pilaf"}

    skipped = post({"planMealId": meals["dinner"]["id"], "adherence": "skipped"})
    d = skipped.json()["log"]["data"]
    assert d["kcal"] == 0 and d["eatenPct"] == 0

    after = client.get(f"{API}/me/meal-plan", headers=test_user).json()
    assert after["loggedMeals"] == 4
    eaten = meals["breakfast"]["kcal"] + round(meals["lunch"]["kcal"] * 0.5, 1) + 570
    assert after["eatenKcal"] == round(eaten, 1) and after["eatenPct"] == round(eaten / plan["totals"]["kcal"] * 100)
    assert {m["slot"]: m["logged"]["adherence"] for m in after["meals"]} == {
        "breakfast": "followed", "lunch": "partial", "snack": "swapped", "dinner": "skipped"}

    overview = client.get(f"{API}/me/logs/meals/overview", headers=test_user).json()
    assert overview["stats"]["planPctToday"] == after["eatenPct"]
    assert overview["stats"]["planText"] == f"{after['eatenPct']}% of plan" and len(overview["plan"]["meals"]) == 4
    home = client.get(f"{API}/me/home", headers=test_user).json()
    assert "% of plan" in next(i for i in home["logs"] if i["module"] == "meals")["detail"]


def test_meal_log_rules(client, test_user, mika):
    plan = client.get(f"{API}/me/meal-plan", headers=test_user).json()
    breakfast = plan["meals"][0]["id"]
    post = lambda data, h=test_user: client.post(f"{API}/me/logs", headers=h, json={"type": "meal", "data": data})
    assert post({"planMealId": breakfast, "adherence": "followed"}).status_code == 409  # already logged
    assert post({"planMealId": breakfast}, mika).status_code == 422  # someone else's plan
    assert post({"planMealId": breakfast, "adherence": "partial"}).status_code == 422  # needs eatenPct
    assert post({"planMealId": breakfast, "adherence": "swapped"}).status_code == 422  # needs swaps
    assert post({"adherence": "followed"}).status_code == 422  # needs planMealId
    assert post({"swaps": [{"foodId": "food-unknown"}]}).status_code == 422
    assert post({}).status_code == 422
    # outside the plan: catalog foods, or the old format with name and kcal (tasteRating now optional)
    free = post({"swaps": [{"foodId": "food-brownie", "servings": 1}]})
    assert free.status_code == 201 and free.json()["log"]["data"]["kcal"] == 220
    old = post({"name": "Crew pizza night", "kcal": 700})
    assert old.status_code == 201 and old.json()["log"]["data"]["proteinG"] == 0
    # editing the logged breakfast keeps its plan link
    log_id = client.get(f"{API}/me/meal-plan", headers=test_user).json()["meals"][0]["logged"]["logId"]
    edit = client.put(f"{API}/me/logs/{log_id}", headers=test_user,
                      json={"type": "meal", "data": {"planMealId": breakfast, "adherence": "partial", "eatenPct": 80}})
    assert edit.status_code == 200 and edit.json()["log"]["data"]["eatenPct"] == 80


def test_ground_sets_the_meal_plan(client, ground, mika):
    one = client.put(f"{API}/ground/astronauts/ast-03/meal-plan/20", headers=ground, json={"meals": [
        {"slot": "breakfast", "name": "Oatmeal", "items": ["oatmeal"], "kcal": 300, "proteinG": 8, "carbsG": 55, "fatG": 5},
        {"slot": "dinner", "time": "18:30", "foods": [{"foodId": "food-beef", "servings": 1},
                                                      {"foodId": "food-rice", "servings": 1.5}]}]})
    assert one.status_code == 200, one.text
    meals = one.json()["meals"]
    assert [m["id"] for m in meals] == ["pm-ast-03-d20-breakfast", "pm-ast-03-d20-dinner"]
    assert meals[1]["kcal"] == 300 + 1.5 * 210 and meals[1]["time"] == "18:30" and meals[1]["name"] == "Beef steak + Rice pilaf"
    mine = client.get(f"{API}/me/meal-plan", headers=mika, params={"day": 20}).json()
    assert mine["totals"]["kcal"] == 300 + 615

    bulk = client.post(f"{API}/ground/astronauts/ast-03/meal-plan", headers=ground, json={
        "seed": True, "fromDay": 21, "toDay": 23,
        "days": [{"day": 22, "meals": [{"slot": "lunch", "name": "Soup", "kcal": 160}]}]})
    assert bulk.status_code == 201 and bulk.json()["days"] == [21, 22, 23]
    assert len(client.get(f"{API}/ground/astronauts/ast-03/meal-plan", headers=ground, params={"day": 21}).json()["meals"]) == 4
    assert len(client.get(f"{API}/me/meal-plan", headers=mika, params={"day": 22}).json()["meals"]) == 1

    put = lambda day, body, h=ground: client.put(f"{API}/ground/astronauts/ast-03/meal-plan/{day}", headers=h, json=body)
    assert put(99, {"meals": [{"slot": "lunch", "name": "Soup", "kcal": 160}]}).status_code == 422
    assert put(20, {"meals": [{"slot": "lunch"}]}).status_code == 422
    assert put(20, {"meals": [{"slot": "lunch", "name": "A", "kcal": 1}, {"slot": "lunch", "name": "B", "kcal": 1}]}).status_code == 422
    assert put(20, {"meals": [{"slot": "lunch", "foods": [{"foodId": "nope"}]}]}).status_code == 422
    assert put(20, {"meals": [{"slot": "lunch", "name": "Soup", "kcal": 160}]}, mika).status_code == 403
    assert client.post(f"{API}/ground/astronauts/ast-03/meal-plan", headers=ground, json={}).status_code == 422


def test_plan_share_appears_in_the_nutrition_summary(client):
    ana = auth(login(client, "ORB3-ANA", "2026"))
    stats = client.get(f"{API}/me/logs/meals/overview", headers=ana).json()["stats"]
    assert isinstance(stats["planPct"], int) and stats["planText"] == f"{stats['planPct']}% of plan"


# --- optional: 7-day check-in streak -------------------------------------------------------------


def test_mood_streak_bonus_on_the_seventh_day_in_a_row(client, test_user):
    """With check-ins on days 6-11, the first check-in of day 12 is the 7th in a row: +10 and +50."""
    from datetime import datetime, timezone

    from sqlalchemy import delete

    from app import db
    from app.cosmo.models import AppLog

    with db.SessionLocal() as session:
        session.execute(delete(AppLog).where(AppLog.astronaut_id == "ast-test", AppLog.type == "mood"))
        for day in range(6, 12):
            session.add(AppLog(id=f"log-streak-{day}", astronaut_id="ast-test", type="mood", mission_day=day,
                               timestamp=datetime(2026, 11, 2 + day, 8, tzinfo=timezone.utc), coins_earned=10,
                               data={"level": 3, "label": "okay", "tags": []}, source="app"))
        session.commit()
    before = client.get(f"{API}/me/home", headers=test_user).json()["coins"]
    first = client.post(f"{API}/me/logs", headers=test_user, json={"type": "mood", "data": {"level": 3}}).json()
    assert first["coinsEarned"] == 60 and first["bonus"] == {"type": "mood_streak_7", "coins": 50, "streak": 7}
    assert first["coins"] == before + 60 and "7-day check-in streak" in first["message"]
    assert client.get(f"{API}/me/status", headers=test_user).json()["streak"] == 7
    again = client.post(f"{API}/me/logs", headers=test_user, json={"type": "mood", "data": {"level": 3}}).json()
    assert again["coinsEarned"] == 10 and again["bonus"] is None  # only the first check-in of the day
