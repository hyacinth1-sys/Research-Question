"""Filter HMDA applicant ages and convert CSV files under data/ to Stata."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


DEFAULT_ROOT = Path.home() / "Desktop" / "hmda_data"
CHUNK_SIZE = 100_000
AGE_COLUMNS = ("applicant_age", "co_applicant_age")
ALLOWED_AGES = {"<25", "25-34"}
STATA_NAME_MAP = {
    "manufactured_home_secured_property_type": "manufactured_home_secured_type",
    "manufactured_home_land_property_interest": "manufactured_home_land_interest",
    "tract_minority_population_percent": "tract_minority_pop_percent",
    "ffiec_msa_md_median_family_income": "ffiec_msa_median_family_income",
    "tract_median_age_of_housing_units": "tract_median_housing_age",
}


def convert_csv(csv_path: Path, overwrite: bool) -> int:
    """Filter a CSV by applicant ages and write one Stata file."""
    output_path = csv_path.with_suffix(".dta")
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output already exists: {output_path}. "
            "Use --overwrite to replace it."
        )

    chunks: list[pd.DataFrame] = []
    kept_rows = 0
    reader = pd.read_csv(
        csv_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
        chunksize=CHUNK_SIZE,
    )
    for chunk in reader:
        missing_columns = set(AGE_COLUMNS).difference(chunk.columns)
        if missing_columns:
            raise ValueError(
                f"{csv_path} is missing required age column(s): "
                f"{', '.join(sorted(missing_columns))}"
            )

        keep = chunk[AGE_COLUMNS[0]].isin(ALLOWED_AGES) | chunk[
            AGE_COLUMNS[1]
        ].isin(ALLOWED_AGES)
        filtered = chunk.loc[keep].copy()
        if filtered.empty:
            continue

        filtered.rename(columns=STATA_NAME_MAP, inplace=True)
        chunks.append(filtered)
        kept_rows += len(filtered)
        print(f"  Scanned {kept_rows:,} matching rows so far")

    if not chunks:
        raise ValueError(f"No rows matching the age filter found in {csv_path}")

    filtered_data = pd.concat(chunks, ignore_index=True)
    temporary_path = output_path.with_name(f"{output_path.stem}.tmp.dta")
    try:
        filtered_data.to_stata(
            temporary_path,
            write_index=False,
            version=118,
        )
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return kept_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Keep rows where applicant_age or co_applicant_age is "
            "'<25' or '25-34', then convert CSV files under data/ to one Stata file each."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Project folder containing data/ (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing .dta files.",
    )
    args = parser.parse_args()

    data_folder = args.root / "data"
    csv_files = sorted(path for path in data_folder.rglob("*.csv") if path.is_file())
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found under {data_folder}")

    print(f"Found {len(csv_files)} CSV file(s) under {data_folder}")
    for csv_path in csv_files:
        print(f"Filtering and converting {csv_path}")
        rows = convert_csv(csv_path, args.overwrite)
        print(f"Finished {csv_path.name}: {rows:,} rows in {csv_path.with_suffix('.dta')}")


if __name__ == "__main__":
    main()
