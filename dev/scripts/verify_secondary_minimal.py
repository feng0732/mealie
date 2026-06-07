"""Minimal stand-alone verification: SQLAlchemy secondary table flush behavior.

Only depends on SQLAlchemy — no mealie imports. Recreates the critical parts of
UserToRecipe / HouseholdToRecipe as secondary association tables and tests what
happens on session.delete() of the primary entity.

Also tests WITH and WITHOUT SQLite FK enforcement to show the difference.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Float, Boolean, String, DateTime, Integer
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    Session,
    sessionmaker,
)
from datetime import datetime, UTC
from uuid import uuid4, UUID
import os


# ---------------------------------------------------------------------------
# Minimal model mirror of User / Recipe / UserToRecipe / HouseholdToRecipe
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username = Column(String)

    # Mirror of mealie: secondary=UserToRecipe, no cascade
    rated_recipes: Mapped[list["Recipe"]] = relationship(
        "Recipe",
        secondary="users_to_recipes",
        back_populates="rated_by",
        overlaps="favorited_by,favorited_recipes,recipe",
    )
    favorite_recipes: Mapped[list["Recipe"]] = relationship(
        "Recipe",
        secondary="users_to_recipes",
        primaryjoin="and_(User.id==UserToRecipe.user_id, UserToRecipe.is_favorite==True)",
        back_populates="favorited_by",
        overlaps="rated_by,rated_recipes,recipe",
    )


class Household(Base):
    __tablename__ = "households"
    id: Mapped[UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name = Column(String)

    made_recipes: Mapped[list["Recipe"]] = relationship(
        "Recipe",
        secondary="households_to_recipes",
        back_populates="made_by",
    )


class Recipe(Base):
    __tablename__ = "recipes"
    id: Mapped[UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name = Column(String)
    last_made = Column(DateTime, nullable=True)
    rating = Column(Float, nullable=True)

    rated_by: Mapped[list[User]] = relationship(
        "User",
        secondary="users_to_recipes",
        back_populates="rated_recipes",
        overlaps="favorited_by,favorited_recipes,recipe",
    )
    favorited_by: Mapped[list[User]] = relationship(
        "User",
        secondary="users_to_recipes",
        primaryjoin="and_(Recipe.id==UserToRecipe.recipe_id, UserToRecipe.is_favorite==True)",
        back_populates="favorite_recipes",
        overlaps="rated_by,rated_recipes,recipe",
    )
    made_by: Mapped[list[Household]] = relationship(
        "Household",
        secondary="households_to_recipes",
        back_populates="made_recipes",
    )


class UserToRecipe(Base):
    __tablename__ = "users_to_recipes"
    user_id = Column(String(36), ForeignKey("users.id"), primary_key=True)
    recipe_id = Column(String(36), ForeignKey("recipes.id"), primary_key=True)
    rating = Column(Float, nullable=True)
    is_favorite = Column(Boolean, nullable=False, default=False)

    recipe: Mapped["Recipe"] = relationship(
        "Recipe", overlaps="rated_by,rated_recipes,favorited_by,favorite_recipes"
    )


class HouseholdToRecipe(Base):
    __tablename__ = "households_to_recipes"
    household_id = Column(String(36), ForeignKey("households.id"), primary_key=True)
    recipe_id = Column(String(36), ForeignKey("recipes.id"), primary_key=True)
    last_made = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_engine(fk_enabled: bool, db_path: str):
    if os.path.exists(db_path):
        os.remove(db_path)
    engine = sa.create_engine(f"sqlite:///{db_path}", echo=False, future=True)

    @sa.event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA foreign_keys = {'ON' if fk_enabled else 'OFF'}")
        cursor.close()

    return engine


def banner(title):
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def setup_data(session: Session):
    user = User(username="alice")
    recipe = Recipe(name="Pasta")
    household = Household(name="Home")
    session.add_all([user, recipe, household])
    session.flush()

    utr = UserToRecipe(user_id=user.id, recipe_id=recipe.id, rating=4.0, is_favorite=True)
    htr = HouseholdToRecipe(household_id=household.id, recipe_id=recipe.id, last_made=datetime.now(UTC))
    session.add_all([utr, htr])
    session.commit()
    return user, recipe, household


def count_rows(session: Session):
    return {
        "users": session.execute(sa.select(sa.func.count()).select_from(User)).scalar(),
        "recipes": session.execute(sa.select(sa.func.count()).select_from(Recipe)).scalar(),
        "households": session.execute(sa.select(sa.func.count()).select_from(Household)).scalar(),
        "user_to_recipe": session.execute(sa.select(sa.func.count()).select_from(UserToRecipe)).scalar(),
        "household_to_recipe": session.execute(sa.select(sa.func.count()).select_from(HouseholdToRecipe)).scalar(),
    }


# ---------------------------------------------------------------------------
# Main scenarios
# ---------------------------------------------------------------------------
def scenario_delete_user(fk_enabled: bool, db_path: str):
    banner(f"Scenario 1: session.delete(user) with UserToRecipe rows   (FK={fk_enabled})")
    engine = make_engine(fk_enabled, db_path)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    user, recipe, household = setup_data(s)
    before = count_rows(s)
    print(f"  Before delete: {before}")
    print(f"  user.id    = {user.id}")
    print(f"  recipe.id  = {recipe.id}")

    s.delete(user)
    try:
        s.commit()
        raised = None
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
        s.rollback()

    after = count_rows(s)
    print(f"  commit() raised: {raised!r}")
    print(f"  After delete:  {after}")
    s.close()


def scenario_delete_recipe(fk_enabled: bool, db_path: str):
    banner(f"Scenario 2: session.delete(recipe) with both association rows   (FK={fk_enabled})")
    engine = make_engine(fk_enabled, db_path)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    user, recipe, household = setup_data(s)
    before = count_rows(s)
    print(f"  Before delete: {before}")

    s.delete(recipe)
    try:
        s.commit()
        raised = None
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
        s.rollback()

    after = count_rows(s)
    print(f"  commit() raised: {raised!r}")
    print(f"  After delete:  {after}")
    s.close()


def scenario_delete_household(fk_enabled: bool, db_path: str):
    banner(f"Scenario 3: session.delete(household) with HouseholdToRecipe rows   (FK={fk_enabled})")
    engine = make_engine(fk_enabled, db_path)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    user, recipe, household = setup_data(s)
    before = count_rows(s)
    print(f"  Before delete: {before}")

    s.delete(household)
    try:
        s.commit()
        raised = None
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
        s.rollback()

    after = count_rows(s)
    print(f"  commit() raised: {raised!r}")
    print(f"  After delete:  {after}")
    s.close()


def main():
    db_dir = os.path.join(os.path.dirname(__file__), ".flush_test")
    os.makedirs(db_dir, exist_ok=True)

    for fk in (False, True):
        tag = "fk_on" if fk else "fk_off"
        scenario_delete_user(fk, os.path.join(db_dir, f"user_{tag}.db"))
        scenario_delete_recipe(fk, os.path.join(db_dir, f"recipe_{tag}.db"))
        scenario_delete_household(fk, os.path.join(db_dir, f"household_{tag}.db"))

    banner("SUMMARY OF OBSERVATIONS")
    print("""
  FK OFF (SQLite default, Mealie test env)
  -----------------------------------------
  delete(user)        → User row gone;  UserToRecipe ROWS REMAIN (orphaned)
  delete(recipe)      → Recipe row gone; UserToRecipe ROWS REMAIN; HouseholdToRecipe ROWS REMAIN
  delete(household)   → Household row gone; HouseholdToRecipe ROWS REMAIN

  FK ON  (PostgreSQL, or SQLite with PRAGMA foreign_keys=ON)
  ------------------------------------------------------------
  delete(user)        → IntegrityError: UserToRecipe.user_id FK violation
                         → ENTIRE DELETE ROLLED BACK (user row NOT deleted)
  delete(recipe)      → IntegrityError: users_to_recipes.recipe_id FK violation
                         → (Unless manually DELETE UserToRecipe first,
                            as Mealie RepositoryRecipes actually does)
  delete(household)   → IntegrityError: households_to_recipes.household_id FK violation
                         → ROLLBACK
""")


if __name__ == "__main__":
    main()
