from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pyreadstat


def normalize_fips(value: object) -> str | None:
    if pd.isna(value):
        return None

    text = str(value).strip()
    if not text:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(5)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description=(
            "Calculate mean quarterly county employment for 2021-2024 and "
            "merge it into the HMDA AI-exposure Stata dataset."
        )
    )
    parser.add_argument(
        "--excel",
        type=Path,
        default=script_dir / "county-ests-employment-and-wages.xlsx",
        help="County quarterly employment Excel file.",
    )
    parser.add_argument(
        "--input-dta",
        type=Path,
        default=script_dir
        / "hmda_2021_2024_income_aige_ai_exposure_merged.dta",
        help="HMDA Stata file containing the AI-exposure merge.",
    )
    parser.add_argument(
        "--output-dta",
        type=Path,
        default=script_dir
        / "hmda_2021_2024_income_aige_ai_exposure_employment_count_merged.dta",
        help="Path for the output Stata file.",
    )
    args = parser.parse_args()

    for path in (args.excel, args.input_dta):
        if not path.is_file():
            raise FileNotFoundError(f"Input file not found: {path}")

    output_path = args.output_dta.resolve()
    if output_path in {args.excel.resolve(), args.input_dta.resolve()}:
        raise ValueError("The output path must not overwrite an input file.")

    quarterly = pd.read_excel(args.excel)
    required_columns = {
        "year",
        "area_fips",
        "month_3_employment",
    }
    missing_columns = required_columns.difference(quarterly.columns)
    if missing_columns:
        raise ValueError(
            "Employment Excel file is missing required columns: "
            + ", ".join(sorted(missing_columns))
        )

    quarterly = quarterly.loc[quarterly["year"].between(2021, 2024)].copy()
    quarterly["area_fips"] = quarterly["area_fips"].map(normalize_fips)
    quarterly = quarterly.loc[
        quarterly["area_fips"].notna()
        & quarterly["area_fips"].ne("US000")
    ].copy()
    quarterly["employment_value"] = pd.to_numeric(
        quarterly["month_3_employment"], errors="coerce"
    )

    annual = quarterly.groupby(
        ["year", "area_fips"], as_index=False
    ).agg(
        annual_employment=("employment_value", "mean"),
        employment_quarters=("employment_value", "count"),
    )
    if annual.duplicated(["year", "area_fips"]).any():
        raise ValueError("Annual employment contains duplicate county-year keys.")

    annual_keys = (
        annual["year"].astype("int64") * 100_000
        + pd.to_numeric(annual["area_fips"]).astype("int64")
    )
    employment_by_key = dict(
        zip(annual_keys.tolist(), annual["annual_employment"].tolist())
    )
    quarters_by_key = dict(
        zip(annual_keys.tolist(), annual["employment_quarters"].tolist())
    )

    metadata = pyreadstat.read_dta(str(args.input_dta), metadataonly=True)[1]
    hmda = pd.read_stata(args.input_dta, convert_categoricals=False)
    added_columns = {"annual_employment", "employment_quarters"}
    existing_columns = added_columns.intersection(hmda.columns)
    if existing_columns:
        raise ValueError(
            "Input DTA already contains output variable(s): "
            + ", ".join(sorted(existing_columns))
        )

    county_codes = (
        pd.to_numeric(hmda["county_code"], errors="coerce")
        .fillna(-1)
        .astype("int64")
    )
    years = (
        pd.to_numeric(hmda["activity_year"], errors="coerce")
        .fillna(-1)
        .astype("int64")
    )
    merge_keys = years * 100_000 + county_codes
    hmda["annual_employment"] = merge_keys.map(employment_by_key).astype(
        "float64"
    )
    hmda["employment_quarters"] = merge_keys.map(quarters_by_key).astype(
        "float32"
    )

    row_count = len(hmda)
    matched_rows = int(hmda["annual_employment"].notna().sum())
    variable_labels = {
        name: label
        for name, label in metadata.column_names_to_labels.items()
        if label
    }
    variable_labels.update(
        {
            "annual_employment": (
                "Mean county employment across observed quarters in year"
            ),
            "employment_quarters": (
                "Number of quarters used for annual employment mean"
            ),
        }
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(
        f"{output_path.stem}.tmp{output_path.suffix}"
    )
    try:
        hmda.to_stata(
            temporary_path,
            write_index=False,
            version=118,
            variable_labels=variable_labels,
        )
        temporary_path.replace(output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    output_metadata = pyreadstat.read_dta(str(output_path), metadataonly=True)[1]
    if output_metadata.number_rows != row_count:
        raise RuntimeError(
            f"Output row count changed: {output_metadata.number_rows} != "
            f"{row_count}"
        )

    print(f"Output: {output_path}")
    print(f"HMDA rows retained: {row_count}")
    print(f"Rows matched to county-year employment: {matched_rows}")
    print(f"Rows without a county-year employment match: {row_count - matched_rows}")
    print(f"Annual county-year records: {len(annual)}")
    print(
        "County-years using 3 quarters: "
        f"{int((annual['employment_quarters'] == 3).sum())}"
    )
    print(
        "County-years using 4 quarters: "
        f"{int((annual['employment_quarters'] == 4).sum())}"
    )


if __name__ == "__main__":
    main()
