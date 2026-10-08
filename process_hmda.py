from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
FIRST_YEAR = 2018
LAST_YEAR = 2025
CHUNK_SIZE = 250_000
CSV_PATH = DATA_DIR / "hmda_2018_2025_applicant_age_under_25.csv"
DTA_PATH = DATA_DIR / "hmda_2018_2025_applicant_age_under_25.dta"
STRING_COLUMNS = (
    "lei",
    "applicant_age",
    "co_applicant_age",
    "derived_race",
    "debt_to_income_ratio",
    "rate_spread",
    "interest_rate",
    "state_code",
    "county_code",
    "census_tract",
    "total_units",
    "combined_loan_to_value_ratio",
    "loan_term",
)


def build_filtered_csv() -> int:
    temporary_path = CSV_PATH.with_suffix(CSV_PATH.suffix + ".tmp")
    rows_written = 0
    header_written = False
    years = {str(year) for year in range(FIRST_YEAR, LAST_YEAR + 1)}

    try:
        for year in range(FIRST_YEAR, LAST_YEAR + 1):
            source_path = DATA_DIR / f"hmda_{year}_ALL.csv.gz"
            if not source_path.is_file():
                raise FileNotFoundError(f"Source archive not found: {source_path}")

            year_rows = 0
            for chunk in pd.read_csv(
                source_path,
                compression="gzip",
                dtype=str,
                keep_default_na=False,
                chunksize=CHUNK_SIZE,
            ):
                required_columns = {"activity_year", "applicant_age"}
                missing_columns = required_columns.difference(chunk.columns)
                if missing_columns:
                    missing = ", ".join(sorted(missing_columns))
                    raise ValueError(f"{source_path.name} is missing columns: {missing}")

                selected = chunk.loc[
                    chunk["activity_year"].isin(years)
                    & chunk["applicant_age"].eq("<25")
                ]
                if not selected.empty:
                    selected.to_csv(
                        temporary_path,
                        mode="a" if header_written else "w",
                        index=False,
                        header=not header_written,
                        encoding="utf-8",
                        lineterminator="\n",
                    )
                    header_written = True
                    year_rows += len(selected)

            rows_written += year_rows
            print(f"{year}: {year_rows:,} rows")

        if not header_written:
            columns = pd.read_csv(
                DATA_DIR / f"hmda_{FIRST_YEAR}_ALL.csv.gz",
                compression="gzip",
                nrows=0,
            ).columns
            pd.DataFrame(columns=columns).to_csv(
                temporary_path,
                index=False,
                encoding="utf-8",
                lineterminator="\n",
            )

        os.replace(temporary_path, CSV_PATH)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    return rows_written


def build_stata_file(expected_rows: int) -> None:
    temporary_path = DTA_PATH.with_suffix(DTA_PATH.suffix + ".tmp")

    try:
        data = pd.read_csv(
            CSV_PATH,
            dtype={column: str for column in STRING_COLUMNS},
            low_memory=False,
        )
        if len(data) != expected_rows:
            raise ValueError(
                f"CSV row count changed during conversion: "
                f"expected {expected_rows:,}, found {len(data):,}"
            )
        if data["applicant_age"].ne("<25").any():
            raise ValueError("CSV contains records with applicant_age other than <25")
        if not data["activity_year"].between(FIRST_YEAR, LAST_YEAR).all():
            raise ValueError(
                f"CSV contains activity_year values outside "
                f"{FIRST_YEAR}-{LAST_YEAR}"
            )

        data.to_stata(
            temporary_path,
            write_index=False,
            version=118,
            data_label="HMDA applicant age under 25, 2018-2025",
        )
        del data
        os.replace(temporary_path, DTA_PATH)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main() -> None:
    if not DATA_DIR.is_dir():
        raise FileNotFoundError(f"Data directory not found: {DATA_DIR}")

    rows_written = build_filtered_csv()
    build_stata_file(rows_written)

    print(f"Total rows: {rows_written:,}")
    print(f"CSV: {CSV_PATH}")
    print(f"Stata: {DTA_PATH}")


if __name__ == "__main__":
    main()
