"""Meal plan and station food catalog of the CosmoMedic app.

The medical team sets the menu of each mission day, with its nutrition. The
astronaut only marks, for each planned meal, whether they followed it, ate
part of it, swapped it or skipped it; the API computes kcal and macros.

The catalog and the default menu are illustrative (typical ISS items with
rounded label-style values), not a real flight menu.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.cosmo.models import AppFood, AppPlanMeal

SLOTS = ("breakfast", "lunch", "dinner", "snack")
SLOT_TIME = {"breakfast": "08:00", "lunch": "12:30", "dinner": "19:00", "snack": "16:00"}
NUTRIENTS = ("kcal", "protein_g", "carbs_g", "fat_g")

# id, name, serving (g), kcal, protein, carbs, fat: per serving
FOODS = [
    ("food-oatmeal", "Oatmeal with raisins", 150, 290, 8, 55, 5),
    ("food-scrambled-eggs", "Scrambled eggs", 120, 190, 13, 3, 14),
    ("food-granola", "Granola with blueberries", 100, 420, 10, 62, 15),
    ("food-tortilla", "Flour tortilla", 45, 140, 4, 23, 3.5),
    ("food-peanut-butter", "Peanut butter", 32, 190, 7, 7, 16),
    ("food-coffee", "Coffee with cream", 240, 60, 1, 6, 3),
    ("food-orange-drink", "Orange drink", 240, 110, 0, 28, 0),
    ("food-yogurt", "Yogurt", 120, 120, 6, 20, 2),
    ("food-shrimp-cocktail", "Shrimp cocktail", 120, 150, 20, 14, 1.5),
    ("food-rice", "Rice pilaf", 150, 210, 4, 44, 2),
    ("food-chicken", "Thermostabilized chicken", 150, 260, 34, 4, 12),
    ("food-beef", "Beef steak", 140, 300, 36, 0, 17),
    ("food-salmon", "Pink salmon", 100, 150, 22, 0, 7),
    ("food-tuna", "Tuna salad spread", 85, 160, 14, 5, 9),
    ("food-chicken-soup", "Chicken soup", 240, 160, 12, 18, 4),
    ("food-mac-cheese", "Macaroni and cheese", 200, 370, 14, 46, 14),
    ("food-spaghetti", "Spaghetti with meat sauce", 230, 340, 18, 44, 10),
    ("food-green-beans", "Green beans with mushrooms", 110, 60, 3, 9, 2),
    ("food-mashed-potatoes", "Mashed potatoes", 150, 170, 3, 26, 6),
    ("food-nuts", "Mixed nuts", 40, 240, 7, 8, 21),
    ("food-dried-fruit", "Dried fruit", 50, 140, 1, 36, 0),
    ("food-fruit-cocktail", "Fruit cocktail", 120, 90, 0, 23, 0),
    ("food-protein-bar", "Protein bar", 60, 230, 20, 24, 7),
    ("food-brownie", "Brownie", 50, 220, 3, 30, 10),
]

# Default menu: a 3-day rotation, about 2,500 kcal a day. Each meal is a list of (foodId, servings).
MENU = [
    {"breakfast": [("food-oatmeal", 1), ("food-scrambled-eggs", 1), ("food-coffee", 1), ("food-orange-drink", 1)],
     "lunch": [("food-chicken", 1), ("food-rice", 1), ("food-green-beans", 1), ("food-tortilla", 1)],
     "snack": [("food-nuts", 1), ("food-protein-bar", 1)],
     "dinner": [("food-spaghetti", 1), ("food-salmon", 1), ("food-fruit-cocktail", 1), ("food-brownie", 1)]},
    {"breakfast": [("food-granola", 1), ("food-yogurt", 1), ("food-coffee", 1)],
     "lunch": [("food-shrimp-cocktail", 1), ("food-mac-cheese", 1), ("food-green-beans", 1), ("food-orange-drink", 1)],
     "snack": [("food-tortilla", 1), ("food-peanut-butter", 1), ("food-dried-fruit", 1)],
     "dinner": [("food-beef", 1), ("food-mashed-potatoes", 1), ("food-chicken-soup", 1), ("food-fruit-cocktail", 1)]},
    {"breakfast": [("food-scrambled-eggs", 1), ("food-tortilla", 2), ("food-coffee", 1), ("food-orange-drink", 1)],
     "lunch": [("food-tuna", 1), ("food-tortilla", 1), ("food-chicken-soup", 1), ("food-dried-fruit", 1)],
     "snack": [("food-protein-bar", 1), ("food-yogurt", 1)],
     "dinner": [("food-chicken", 1), ("food-spaghetti", 1), ("food-green-beans", 1), ("food-brownie", 1), ("food-nuts", 1)]},
]


def food_out(f: AppFood) -> dict:
    return {"id": f.id, "name": f.name, "servingG": f.serving_g, "kcal": f.kcal, "proteinG": f.protein_g,
            "carbsG": f.carbs_g, "fatG": f.fat_g}


def plan_meal_out(m: AppPlanMeal) -> dict:
    return {"id": m.id, "slot": m.slot, "time": m.time, "name": m.name, "items": m.items, "kcal": m.kcal,
            "proteinG": m.protein_g, "carbsG": m.carbs_g, "fatG": m.fat_g}


def nutrition_of(session: Session, portions: list[tuple[str, float]]) -> tuple[dict, list[AppFood]]:
    """Sum kcal and macros of (foodId, servings). Raises KeyError with the unknown food id."""
    total = dict.fromkeys(NUTRIENTS, 0.0)
    foods = []
    for food_id, servings in portions:
        food = session.get(AppFood, food_id)
        if food is None:
            raise KeyError(food_id)
        foods.append(food)
        for n in NUTRIENTS:
            total[n] += getattr(food, n) * servings
    return {n: round(v, 1) for n, v in total.items()}, foods


def plan_meal_id(astronaut_id: str, day: int, slot: str) -> str:
    return f"pm-{astronaut_id}-d{day}-{slot}"


def set_day(session: Session, astronaut_id: str, day: int, meals: list[dict]) -> list[AppPlanMeal]:
    """Replace the plan of one day. Each meal: slot, time, name, items, kcal, protein_g, carbs_g, fat_g."""
    session.execute(delete(AppPlanMeal).where(AppPlanMeal.astronaut_id == astronaut_id, AppPlanMeal.mission_day == day))
    rows = [AppPlanMeal(id=plan_meal_id(astronaut_id, day, m["slot"]), astronaut_id=astronaut_id, mission_day=day, **m)
            for m in meals]
    session.add_all(rows)
    session.flush()
    return rows


def default_day(session: Session, day: int) -> list[dict]:
    """The default menu of a mission day (3-day rotation), with nutrition from the catalog."""
    menu = MENU[(day - 1) % len(MENU)]
    meals = []
    for slot in SLOTS:
        total, foods = nutrition_of(session, menu[slot])
        names = [f.name for f in foods]
        meals.append({"slot": slot, "time": SLOT_TIME[slot], "name": " + ".join(names[:2]), "items": names, **total})
    return sorted(meals, key=lambda m: m["time"])


def seed_default_plan(session: Session, astronaut_id: str, from_day: int, to_day: int) -> int:
    for day in range(from_day, to_day + 1):
        set_day(session, astronaut_id, day, default_day(session, day))
    return to_day - from_day + 1


def day_plan(session: Session, astronaut_id: str, day: int) -> list[AppPlanMeal]:
    return list(session.scalars(select(AppPlanMeal).where(
        AppPlanMeal.astronaut_id == astronaut_id, AppPlanMeal.mission_day == day).order_by(AppPlanMeal.time)))
