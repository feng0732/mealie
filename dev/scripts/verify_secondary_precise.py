"""Follow-up verification: compare two ways of creating UserToRecipe rows:
  A) Direct session.add(UserToRecipe(...))
  B) Via user.rated_recipes.append(recipe) / user.favorite_recipes.append(recipe)
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Float, Boolean, String, DateTime
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


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username = Column(String)

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


class Recipe(Base):
    __tablename__ = "recipes"
    id: Mapped[UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name = Column(String)

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


class UserToRecipe(Base):
    __tablename__ = "users_to_recipes"
    user_id = Column(String(36), ForeignKey("users.id"), primary_key=True)
    recipe_id = Column(String(36), ForeignKey("recipes.id"), primary_key=True)
    rating = Column(Float, nullable=True)
    is_favorite = Column(Boolean, nullable=False, default=False)

    recipe: Mapped["Recipe"] = relationship(
        "Recipe", overlaps="rated_by,rated_recipes,favorited_by,favorite_recipes"
    )


def make_engine(db_path: str):
    if os.path.exists(db_path):
        os.remove(db_path)
    return sa.create_engine(f"sqlite:///{db_path}", echo=False, future=True)


def count_rows(session: Session):
    return {
        "users": session.execute(sa.select(sa.func.count()).select_from(User)).scalar(),
        "recipes": session.execute(sa.select(sa.func.count()).select_from(Recipe)).scalar(),
        "user_to_recipe": session.execute(sa.select(sa.func.count()).select_from(UserToRecipe)).scalar(),
    }


def scenario_A_direct_session_add(db_path: str):
    """Method A: session.add(UserToRecipe(...)) — this is what Mealie actually does."""
    print("\n" + "=" * 72)
    print("  METHOD A: session.add(UserToRecipe(...))   (Mealie's actual pattern)")
    print("=" * 72)
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    user = User(username="alice")
    recipe = Recipe(name="Pasta")
    s.add_all([user, recipe])
    s.flush()

    # Direct add — this is how RepositoryUserRatings works
    utr = UserToRecipe(user_id=user.id, recipe_id=recipe.id, rating=4.0, is_favorite=True)
    s.add(utr)
    s.commit()

    print(f"  Before: {count_rows(s)}")

    s.delete(user)
    try:
        s.commit()
        raised = None
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
        s.rollback()

    print(f"  session.delete(user) + commit() raised:")
    print(f"    {raised!r}")
    print(f"  After:  {count_rows(s)}")
    s.close()


def scenario_B_via_relationship_append(db_path: str):
    """Method B: user.rated_recipes.append(recipe) — 'pure' M2M usage."""
    print("\n" + "=" * 72)
    print("  METHOD B: user.rated_recipes.append(recipe)   (pure M2M pattern)")
    print("=" * 72)
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    user = User(username="bob")
    recipe = Recipe(name="Pizza")
    # Via relationship — does NOT set rating/is_favorite correctly
    user.rated_recipes.append(recipe)
    s.add_all([user, recipe])
    s.commit()

    print(f"  Before: {count_rows(s)}")
    # Check if rating/is_favorite were populated
    utr = s.query(UserToRecipe).first()
    print(f"  UTR row: rating={utr.rating}, is_favorite={utr.is_favorite}")

    s.delete(user)
    try:
        s.commit()
        raised = None
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
        s.rollback()

    print(f"  session.delete(user) + commit() raised:")
    print(f"    {raised!r}")
    print(f"  After:  {count_rows(s)}")
    s.close()


def scenario_C_direct_sa_delete_then_delete_user(db_path: str):
    """Method C: Manual sa.delete(UserToRecipe) FIRST, then delete user — like RepositoryRecipes does."""
    print("\n" + "=" * 72)
    print("  METHOD C: sa.delete(UserToRecipe) first, then session.delete(user)   (manual cleanup)")
    print("=" * 72)
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    user = User(username="carol")
    recipe = Recipe(name="Soup")
    s.add_all([user, recipe])
    s.flush()

    utr = UserToRecipe(user_id=user.id, recipe_id=recipe.id, rating=5.0, is_favorite=True)
    s.add(utr)
    s.commit()

    print(f"  Before: {count_rows(s)}")

    # Manual cleanup first — this is what RepositoryRecipes._delete_recipe does
    s.execute(sa.delete(UserToRecipe).where(UserToRecipe.user_id == user.id))
    s.flush()
    print(f"  After manual sa.delete(UserToRecipe): {count_rows(s)}")

    s.delete(user)
    try:
        s.commit()
        raised = None
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
        s.rollback()

    print(f"  session.delete(user) + commit() raised:")
    print(f"    {raised!r}")
    print(f"  After:  {count_rows(s)}")
    s.close()


def main():
    db_dir = os.path.join(os.path.dirname(__file__), ".flush_test2")
    os.makedirs(db_dir, exist_ok=True)

    scenario_A_direct_session_add(os.path.join(db_dir, "A.db"))
    scenario_B_via_relationship_append(os.path.join(db_dir, "B.db"))
    scenario_C_direct_sa_delete_then_delete_user(os.path.join(db_dir, "C.db"))

    print("\n" + "=" * 72)
    print("  CONCLUSION")
    print("=" * 72)
    print("""
  A) session.add(UserToRecipe(...)) then session.delete(user)
     → StaleDataError! SQLAlchemy tries to auto-delete secondary rows
       but can't find them (because UTR was added directly, not via relationship).
       Transaction ROLLS BACK — nothing is deleted.

  B) user.rated_recipes.append(recipe) then session.delete(user)
     → Works. UserToRecipe row auto-deleted by SQLAlchemy's implicit
       secondary cleanup. BUT rating / is_favorite columns are NOT
       populated (they'd be NULL/False) — the relationship-based append
       cannot carry extra attributes of the association object.

  C) sa.delete(UserToRecipe) first, then session.delete(user)
     → Works cleanly. Manual cleanup bypasses the secondary confusion.
       This is exactly what RepositoryRecipes._delete_recipe does for
       deleting Recipe (but RepositoryUsers.delete does NOT do this).

  KEY IMPLICATION FOR MEALIE:
    RepositoryUsers.delete() does NOT do the manual sa.delete(UserToRecipe)
    cleanup step. When a user with favorites/ratings is deleted:
      - SQLite without FK enforcement → StaleDataError, full rollback
      - PostgreSQL with FK enforcement → either StaleDataError first,
        or IntegrityError if SQLAlchemy somehow bypasses the stale check.
    In BOTH cases the user is NOT deleted.

    This also explains why the existing test suite passes — tests use
    brand-new fixture users with NO ratings/favorites, so StaleDataError
    is never triggered.
""")


if __name__ == "__main__":
    main()
