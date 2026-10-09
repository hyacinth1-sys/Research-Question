#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载2021—2024年美国各县人均收入（Census ACS B19301）。

默认ACS5覆盖50州及DC全部县/县等价地区，2021为2017—2021估计，2024为2020—2024估计。
不是四个单年值；各年金额分别以各发布年份的美元计价。
Python 3.10+，无需第三方Python包或Census API key。

    python download_county_income.py
    python download_county_income.py --years 2021-2024 --include-pr
    python download_county_income.py --source acs1

ACS1为一年期，只覆盖有发布数据的大县；不能保证覆盖所有县。
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("county_income")
BLOCK_SIZE = 1024 * 1024
STATE_FIPS = set("01 02 04 05 06 08 09 10 11 12 13 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48 49 50 51 53 54 55 56".split())
FIELDS = ["year", "source", "period_start", "period_end", "dollar_year",
          "state_fips", "state_name", "county_code", "county_fips", "county_name",
          "geo_id", "per_capita_income_usd", "moe_usd", "raw_estimate", "raw_moe",
          "data_status", "moe_status", "ct_geography", "note"]

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()

def write_json(path: Path, value: dict) -> None:
    """先写临时文件，成功后替换；中断不留下假完整JSON。"""
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)

def request(url: str, method: str = "GET") -> Request:
    return Request(
        url, method=method,
        headers={"User-Agent": "County-Income-Downloader/1.0", "Accept-Encoding": "identity"},
    )

class CurlDownloadError(OSError):
    def __init__(self, returncode: int, status: int, message: str):
        super().__init__(f"curl退出码{returncode}，HTTP{status}：{message}")
        self.returncode, self.status = returncode, status

def curl_request(url: str, timeout: float, target: Path | None = None, year: int | None = None) -> dict:
    """通过同一官方URL下载；不调用shell，不改用其他数据源。"""
    executable = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if executable is None:
        raise ValueError("找不到curl下载工具。请安装curl或在Python能直接访问Census的网络重试。")
    marker = "\nINCOME_TRANSFER_INFO\n"
    command = [
        executable, "--location", "--silent", "--show-error", "--fail",
        "--proto", "=https", "--proto-redir", "=https",
        "--connect-timeout", str(min(timeout, 60)),
        "--speed-limit", "1", "--speed-time", str(max(1, math.ceil(timeout))),
        "--dump-header", "-", "--output", str(target) if target else os.devnull,
        "--write-out", marker + "%{http_code}\n%{url_effective}\n%{size_download}\n",
    ]
    if target is None:
        command.append("--head")
    elif target.exists():
        target.unlink()  # 仅清理本次未完成的.part文件。
    command.append(url)
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=10)
                break
            except subprocess.TimeoutExpired:
                if target is not None and target.exists():
                    LOGGER.info("%s已下载%s MiB（curl）。", year, f"{target.stat().st_size / BLOCK_SIZE:,.1f}")
    except BaseException:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    headers_text, separator, information = stdout.rpartition(marker)
    parts = information.strip().splitlines() if separator else []
    status = int(parts[0]) if parts and parts[0].isdigit() else 0
    if process.returncode != 0:
        raise CurlDownloadError(process.returncode, status, stderr.strip())
    if len(parts) != 3 or status != 200:
        raise ValueError(f"curl未返回完整HTTP200响应：{status}。")
    # 多跳重定向、代理CONNECT或103响应，只使用最终HTTP响应头。
    headers: dict[str, str] = {}
    header_status = 0
    for line in headers_text.replace("\r\n", "\n").splitlines():
        if line.startswith("HTTP/"):
            headers = {}
            words = line.split()
            header_status = int(words[1]) if len(words) > 1 and words[1].isdigit() else 0
        elif ":" in line and header_status:
            key, value = line.split(":", 1)
            headers[key.lower().strip()] = value.strip()
    if header_status != status:
        raise ValueError("curl响应头状态与最终HTTP状态不一致。")
    return {
        "resolved_url": parts[1], "headers": headers,
        "reported_bytes": int(float(parts[2])), "download_transport": "curl",
    }

def download_using_urllib(url: str, temporary: Path, timeout: float, year: int) -> dict:
    digest = hashlib.sha256()
    received = 0
    last_report = time.monotonic()
    with urlopen(request(url), timeout=timeout) as response, temporary.open("wb") as handle:
        if response.status != 200:
            raise ValueError(f"需要完整文件的HTTP200响应，得到{response.status}。")
        headers = {key.lower(): value for key, value in response.headers.items()}
        expected = headers.get("content-length")
        for block in iter(lambda: response.read(BLOCK_SIZE), b""):
            handle.write(block)
            digest.update(block)
            received += len(block)
            if time.monotonic() - last_report >= 10:
                progress = f"/{int(expected) / BLOCK_SIZE:,.1f} MiB" if expected is not None else " MiB"
                LOGGER.info("%s已下载%s%s", year, f"{received / BLOCK_SIZE:,.1f}", progress)
                last_report = time.monotonic()
        return {
            "resolved_url": response.geturl(), "headers": headers,
            "bytes": received, "sha256": digest.hexdigest(), "download_transport": "urllib",
        }

def fetch(url: str, target: Path, timeout: float, retries: int, refresh: bool) -> dict:
    """缓存通过来源与SHA核验才复用；每次完整下载后才发布正式原始文件。"""
    receipt = target.with_name(target.name + ".source.json")
    temporary = target.with_name(target.name + ".part")
    if target.exists() and not refresh:
        if not receipt.exists():
            raise ValueError(f"缓存缺少来源记录：{target}；请加--refresh。")
        metadata = json.loads(receipt.read_text(encoding="utf-8"))
        if metadata.get("source_url") != url or metadata.get("sha256") != sha256_file(target):
            raise ValueError(f"缓存与来源不一致：{target}；请加--refresh。")
        LOGGER.info("复用校验通过的文件：%s", target.name)
        return metadata
    for attempt in range(1, retries + 1):
        try:
            LOGGER.info("下载%s（%s/%s）", target.name, attempt, retries)
            try:
                result = download_using_urllib(url, temporary, timeout, target.name)
            except HTTPError as exc:
                if exc.code != 403:
                    raise
                LOGGER.info("Python请求被拒绝，改用curl访问相同URL。")
                result = curl_request(url, timeout, temporary, target.name)
                result["bytes"] = temporary.stat().st_size
                result["sha256"] = sha256_file(temporary)
                if result["reported_bytes"] != result["bytes"]:
                    raise HTTPException("curl报告字节数与文件大小不一致。")
            expected = result["headers"].get("content-length")
            if expected is not None and int(expected) != result["bytes"]:
                raise HTTPException("下载长度不完整。")
            with temporary.open("rb") as stream:
                beginning = stream.read(4096).lstrip().lower()
            if not beginning or beginning.startswith((b"<!doctype", b"<html")):
                # CPS本身是HTML；要求真正的官方表格内容，解析阶段仍会进一步核验。
                if not (target.suffix == ".html" and b"cps" in beginning):
                    raise ValueError("下载结果为空或为错误网页。")
            metadata = {"source_url": url, "resolved_url": result["resolved_url"],
                        "downloaded_at_utc": utc_now(), "bytes": result["bytes"],
                        "sha256": result["sha256"], "transport": result["download_transport"],
                        "etag": result["headers"].get("etag"),
                        "last_modified": result["headers"].get("last-modified")}
            os.replace(temporary, target)
            write_json(receipt, metadata)
            return metadata
        except HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == retries:
                raise
        except CurlDownloadError as exc:
            retryable = exc.status in {408, 429, 500, 502, 503, 504} or (
                exc.status < 400 and exc.returncode in {5, 6, 7, 18, 28, 35, 52, 55, 56, 92})
            if not retryable or attempt == retries:
                raise
        except (URLError, TimeoutError, socket.timeout, HTTPException):
            if attempt == retries:
                raise
        LOGGER.warning("暂时的网络错误，稍后从头重试。")
        time.sleep(min(2 ** attempt, 30))
    raise RuntimeError("未完成下载。")

def dict_rows(path: Path):
    """官方2020 CSV用逗号，后续TXT用pipe；按表头识别，保留引号字段。"""
    # 2020原型.csv实际采用Windows西欧编码；后续.txt为UTF-8。
    encoding = "cp1252" if path.suffix.lower() == ".csv" else "utf-8-sig"
    with path.open("r", encoding=encoding, newline="") as stream:
        first = stream.readline()
        delimiter = "|" if "|" in first else ","
        stream.seek(0)
        reader = csv.DictReader(stream, delimiter=delimiter, strict=True)
        if not reader.fieldnames:
            raise ValueError(f"文件没有表头：{path}")
        # 2020官方地理CSV有多个BLANK占位列；实际使用字段必须唯一。
        duplicate_columns = {name for name in reader.fieldnames
                             if name not in {"BLANK", ""} and reader.fieldnames.count(name) > 1}
        if duplicate_columns:
            raise ValueError(f"文件存在重复表头：{sorted(duplicate_columns)}")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV列数错误：{path}")
            yield row


def parse_years(values: list[str]) -> list[int]:
    years = set()
    for value in values:
        for token in value.split(","):
            if "-" in token:
                first, last = map(int, token.split("-", 1))
                if first > last or first < 2021 or last > 2024:
                    raise ValueError("已核实年份为2021—2024；年份区间必须升序。")
                years.update(range(first, last + 1))
            else:
                year = int(token)
                if not 2021 <= year <= 2024:
                    raise ValueError("已核实年份为2021—2024。")
                years.add(year)
    return sorted(years)


def source_urls(year: int, source: str) -> dict[str, str]:
    period = 5 if source == "acs5" else 1
    base = f"https://www2.census.gov/programs-surveys/acs/summary_file/{year}/table-based-SF"
    return {"table": f"{base}/data/{period}YRData/acsdt{period}y{year}-b19301.dat",
            "geography": f"{base}/documentation/Geos{year}{period}YR.txt",
            "labels": f"{base}/documentation/ACS{year}{period}YR_Table_Shells.txt"}


def verify_income_label(path: Path, year: int) -> dict:
    matched = []
    for row in dict_rows(path):
        if row.get("Unique ID") == "B19301_001":
            matched.append(row)
    if len(matched) != 1:
        raise ValueError("字段壳中B19301_001缺失或重复。")
    row = matched[0]
    title = " ".join(row.get("Title", "").split()).lower()
    expected = f"per capita income in the past 12 months (in {year} inflation-adjusted dollars)"
    if title != expected or row.get("Table ID") != "B19301":
        raise ValueError(f"{year}人均收入字段定义或美元年份与预期不同：{title}")
    return {"table": "B19301", "estimate_column": "B19301_E001", "moe_column": "B19301_M001",
            "definition": row["Title"], "universe": row.get("Universe", ""),
            "dollar_year": year}


def county_geographies(path: Path, include_pr: bool) -> dict[str, dict]:
    counties, codes, states = {}, set(), {}
    allowed = STATE_FIPS | ({"72"} if include_pr else set())
    for row in dict_rows(path):
        required = {"GEO_ID", "SUMLEVEL", "COMPONENT", "STATE", "COUNTY", "NAME"}
        if not required <= row.keys():
            raise ValueError("地理文件缺少必要字段。")
        if row["COMPONENT"] != "00" or row["STATE"] not in allowed:
            continue
        if row["SUMLEVEL"] == "040":
            if row["STATE"] in states:
                raise ValueError("地理文件州记录重复。")
            states[row["STATE"]] = row["NAME"]
        if row["SUMLEVEL"] != "050":
            continue
        state, county, geo_id = row["STATE"], row["COUNTY"], row["GEO_ID"]
        if not re.fullmatch(r"\d{2}", state) or not re.fullmatch(r"\d{3}", county):
            raise ValueError("县FIPS格式错误；不能把数字浮点值当代码。")
        fips = state + county
        if geo_id != "0500000US" + fips:
            raise ValueError(f"县GEO_ID与州县FIPS不一致：{geo_id}")
        if geo_id in counties or fips in codes:
            raise ValueError(f"县地理重复：{fips}")
        codes.add(fips)
        counties[geo_id] = {"geo_id": geo_id, "state_fips": state, "county_code": county,
                            "county_fips": fips, "county_name": row["NAME"]}
    if not counties:
        raise ValueError("地理文件中没有县级记录。")
    for county in counties.values():
        if county["state_fips"] not in states:
            raise ValueError("县的州名缺失，停止合并。")
        county["state_name"] = states[county["state_fips"]]
    return counties


def income_value(value: str) -> int | None:
    if value.strip() in {"", "-", "N", "(X)", "null"}:
        return None
    number = int(value)
    return number if number >= 0 else None


def extract_income(path: Path, counties: dict, year: int, source: str, include_pr: bool = False) -> tuple[list[dict], dict]:
    records, seen = [], set()
    allowed_states = STATE_FIPS | ({"72"} if include_pr else set())
    for row in dict_rows(path):
        if not {"GEO_ID", "B19301_E001", "B19301_M001"} <= row.keys():
            raise ValueError("B19301文件缺少人均收入或MOE列。")
        geo_id = row["GEO_ID"]
        if geo_id not in counties:
            # 若出现未知但属于所需范围的县，不能悄悄丢失。
            match = re.fullmatch(r"0500000US(\d{2})\d{3}", geo_id)
            if match and match[1] in allowed_states:
                raise ValueError(f"收入表存在地理文件没有的县：{geo_id}")
            continue
        if geo_id in seen:
            raise ValueError(f"收入表县记录重复：{geo_id}")
        seen.add(geo_id)
        estimate, moe = income_value(row["B19301_E001"]), income_value(row["B19301_M001"])
        records.append({**counties[geo_id], "year": year, "source": source,
                        "period_start": year - 4 if source == "acs5" else year,
                        "period_end": year, "dollar_year": year,
                        "per_capita_income_usd": estimate, "moe_usd": moe,
                        "raw_estimate": row["B19301_E001"], "raw_moe": row["B19301_M001"],
                        "data_status": "ok" if estimate is not None else "estimate_missing",
                        "moe_status": "ok" if moe is not None else "moe_missing",
                        "ct_geography": ("planning_region" if year >= 2022 else "historical_county") if counties[geo_id]["state_fips"] == "09" else "",
                        "note": "全体人口人均收入，非青年工资；当年美元计价；保留当年县边界。"})
    if not seen:
        raise ValueError("收入表未找到任何所选县的记录。")
    no_data = set(counties) - seen
    for geo_id in sorted(no_data):
        county = counties[geo_id]
        records.append({**county, "year": year, "source": source,
                        "period_start": year - 4 if source == "acs5" else year,
                        "period_end": year, "dollar_year": year,
                        "per_capita_income_usd": None, "moe_usd": None,
                        "raw_estimate": "", "raw_moe": "", "data_status": "no_table_row",
                        "moe_status": "no_table_row",
                        "ct_geography": ("planning_region" if year >= 2022 else "historical_county") if county["state_fips"] == "09" else "",
                        "note": "当年地理文件列出该县，但人均收入表没有记录；不是收入为零。"})
    records.sort(key=lambda record: record["county_fips"])
    return records, {"year": year, "geography_count": len(counties), "data_row_count": len(seen),
                     "no_table_row_count": len(no_data),
                     "income_missing_count": sum(record["data_status"] != "ok" for record in records),
                     "moe_missing_count": sum(record["moe_status"] != "ok" for record in records),
                     "no_table_row_fips": [counties[geo_id]["county_fips"] for geo_id in sorted(no_data)]}


def write_csv(target: Path, records: list[dict]) -> None:
    temporary = target.with_name(target.name + ".part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    os.replace(temporary, target)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", nargs="+", default=["2021-2024"], help="默认2021-2024四个发布版本。")
    parser.add_argument("--source", choices=["acs5", "acs1"], default="acs5", help="默认ACS5全县五年估计；ACS1单年但不覆盖所有县。")
    parser.add_argument("--include-pr", action="store_true", help="另含波多黎各municipios；默认仅50州+DC。")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "county_income_data", help="默认脚本旁county_income_data。")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--refresh", action="store_true", help="重新下载并替换原件缓存。")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        years = parse_years(args.years)
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 86400 or args.retries < 1:
            raise ValueError("timeout必须为不超过86400秒的有限正数，retries至少为1。")
        root = args.out.resolve() / args.source
        raw, data, meta_dir = root / "raw", root / "data", root / "metadata"
        for directory in (raw, data, meta_dir):
            directory.mkdir(parents=True, exist_ok=True)
        scope = "US_PR" if args.include_pr else "US"
        stem = f"county_per_capita_income_{args.source}_{scope}_{'_'.join(map(str, years))}"
        receipt = meta_dir / f"{stem}.json"
        if receipt.exists():
            receipt.unlink()
        records, statuses, downloads, labels = [], [], [], {}
        LOGGER.info("下载%s，2021—2024可选；地区范围：%s。", args.source, "50州+DC+波多黎各" if args.include_pr else "50州+DC")
        for year in years:
            urls, paths = source_urls(year, args.source), {}
            for role, url in urls.items():
                target = raw / f"{year}_{role}_{url.rsplit('/', 1)[1]}"
                downloads.append(fetch(url, target, args.timeout, args.retries, args.refresh))
                paths[role] = target
            labels[str(year)] = verify_income_label(paths["labels"], year)
            counties = county_geographies(paths["geography"], args.include_pr)
            yearly, status = extract_income(paths["table"], counties, year, args.source, args.include_pr)
            records.extend(yearly)
            statuses.append(status)
            LOGGER.info("%s：%s个县及等价地区，%s条实际表记录，%s条收入缺失。", year, status["geography_count"], status["data_row_count"], status["income_missing_count"])
        output = data / f"{stem}.csv"
        write_csv(output, records)
        write_json(receipt, {"source": "Census ACS B19301", "dataset": args.source,
                             "requested_years": years, "scope": scope, "include_pr": args.include_pr,
                             "years": statuses, "rows": len(records), "field_checks": labels,
                             "download_sources": downloads, "output_file": str(output),
                             "output_sha256": sha256_file(output), "script_sha256": sha256_file(Path(__file__)),
                             "processed_at_utc": utc_now(), "python_version": sys.version,
                             "dollar_note": "Each release uses its own terminal-year inflation-adjusted dollars, not a common-dollar series.",
                             "period_note": "ACS5 is an overlapping five-year estimate; ACS1 is a one-year estimate with limited county coverage.",
                             "geography_note": "Original release geographies; CT changes from 8 historical counties in 2021 to 9 planning regions from 2022.",
                             "missing_rule": "Negative Census special values and blanks are missing, never zero; E and M are handled independently.",
                             "fields": FIELDS})
        LOGGER.info("完成：%s；逐年覆盖情况和来源记录：%s", output, receipt)
        return 0
    except KeyboardInterrupt:
        LOGGER.error("已中断；.part不代表完整数据。再次运行会复用已校验原件。")
        return 130
    except (ValueError, OSError, HTTPException, csv.Error) as exc:
        LOGGER.error("未完成：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
