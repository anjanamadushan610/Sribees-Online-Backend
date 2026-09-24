"""
Service areas: province unlocked -> district unlocked -> postal area active.

Migration 066 is replayed on every deploy, so the most expensive mistake here
is one that looks fine the first time: a seed that resets an admin's unlock
choices on the next deploy, or a province spelled two ways so a whole district
silently drops out of the address picker. These pin both down without a
database; the full flow (activation, guards, locking, replay) was exercised
against a freshly migrated Postgres when this was built.
"""
import re
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from app.services.location_rollout_service import (
    canonical_province,
    served_mapping_clause,
)

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "066_location_rollout.sql"
SQL = MIGRATION.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "given, stored",
    [
        ("Western Province", "Western"),
        ("western province", "western"),
        ("  North Western Province ", "North Western"),
        ("Western", "Western"),
        ("Sabaragamuwa", "Sabaragamuwa"),
        (None, ""),
    ],
)
def test_one_spelling_of_a_province(given, stored):
    assert canonical_province(given) == stored


def _values(table: str) -> list[str]:
    block = re.search(
        rf"INSERT INTO {table} \([^)]*\) VALUES\n(.*?)\nON CONFLICT", SQL, re.S
    ).group(1)
    return [line.strip() for line in block.splitlines() if line.strip()]


def test_every_seed_is_written_once_and_never_again():
    """Replayed on every deploy: an INSERT without ON CONFLICT DO NOTHING
    would either fail the deploy or, worse, reset an admin's unlocks."""
    inserts = re.findall(r"INSERT INTO (\w+)", SQL)
    assert inserts == ["location_provinces", "location_districts", "post_office_directory"]
    assert len(re.findall(r"^ON CONFLICT \(\w+\) DO NOTHING;$", SQL, re.M)) == 3
    assert "DO UPDATE" not in SQL
    assert "CREATE TABLE IF NOT EXISTS location_provinces" in SQL
    assert "CREATE TABLE IF NOT EXISTS location_districts" in SQL


def test_the_country_is_seeded_with_only_kalutara_open():
    provinces = _values("location_provinces")
    districts = _values("location_districts")
    assert len(provinces) == 9
    assert len(districts) == 25
    unlocked_p = [p for p in provinces if ", TRUE," in p]
    unlocked_d = [d for d in districts if ", TRUE," in d]
    assert len(unlocked_p) == 1 and unlocked_p[0].startswith("('Western'")
    assert len(unlocked_d) == 1 and unlocked_d[0].startswith("('Kalutara', 'Western'")


def test_the_national_directory_has_no_repeated_names():
    rows = _values("post_office_directory")
    names = [re.match(r"\(gen_random_uuid\(\), '((?:[^']|'')*)'", r).group(1) for r in rows]
    assert len(names) == len(set(names)) == 2107
    # Stored in the short spelling, like everything else here.
    assert not any("Province'" in r for r in rows)


def test_served_means_on_with_a_branch_and_unlocked_all_the_way_up():
    sql = str(
        served_mapping_clause().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "post_office_branch_mapping.is_active IS true" in sql
    assert "post_office_directory.is_launched IS true" in sql
    assert "location_districts.is_unlocked IS true" in sql
    assert "location_provinces.is_unlocked IS true" in sql


def test_the_launch_switch_lives_on_the_postal_area_not_the_mapping():
    """Anchored in the wrong class, a scripted column edit once passed every
    test and broke checkout. Read the mapping itself."""
    from app.models.branch import PostalCityBranchMapping, PostalCityDirectory

    assert "is_launched" in PostalCityDirectory.__table__.columns
    assert "is_launched" not in PostalCityBranchMapping.__table__.columns
    assert PostalCityDirectory(postal_city="X", district="Y", province="Z", is_launched=True).is_launched


def test_the_backfill_runs_once_and_keeps_what_was_live():
    sql = (MIGRATION.parent / "067_postal_area_launch.sql").read_text(encoding="utf-8")
    # Guarded by "column does not exist yet", so a replay never switches back
    # on a postal area an admin has since switched off.
    assert "IF NOT EXISTS" in sql and "column_name = 'is_launched'" in sql
    assert sql.index("ADD COLUMN is_launched") < sql.index("SET is_launched = TRUE") < sql.index("END IF")
    assert "AND m.is_active" in sql
