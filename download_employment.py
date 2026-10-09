#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载美国青年就业数据。Python 3.10+；无需第三方Python包或Census API key。

默认：ACS1，2020-2024，完整MSA，20-24岁；2020常规ACS1未发布，明确记录缺失。
    python download_employment.py
    python download_employment.py --source acs5 --years 2020-2024
    python download_employment.py --source cps --geo us --ages 16-24

ACS5是滚动五年估计；CPS是全国年度平均，不能冒充MSA数据。
ACS公开数据按各年度发布地理提取，本脚本不验证跨年固定MSA县成员集合。
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
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

LOGGER = logging.getLogger("employment")
BLOCK_SIZE = 1024 * 1024
MISSING_2020_URL = "https://www.census.gov/programs-surveys/acs/guidance/comparing-acs-data/2020.html"
AGE_BASES = {
    "16-24": [(3, "16 to 19 years"), (10, "20 and 21 years"), (17, "22 to 24 years")],
    "20-24": [(10, "20 and 21 years"), (17, "22 to 24 years")],
    "25-34": [(24, "25 to 29 years"), (31, "30 to 34 years")],
    "35-44": [(38, "35 to 44 years")],
}
COUNT_OFFSETS = {"population": 0, "armed_forces": 2, "civilian_labor_force": 3,
                 "employed": 4, "unemployed": 5, "not_in_labor_force": 6}
CSV_FIELDS = [
    "source", "dataset", "year", "period_start", "period_end", "geography",
    "geo_id", "area_code", "area_name", "age_group", "population",
    "civilian_population", "armed_forces", "civilian_labor_force", "employed",
    "unemployed", "not_in_labor_force", "employment_population_ratio_pct",
    "civilian_employment_population_ratio_pct", "civilian_labor_force_participation_rate_pct",
    "unemployment_rate_pct", "data_status", "note",
]

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
        headers={"User-Agent": "HMDA-Research-Downloader/1.0", "Accept-Encoding": "identity"},
    )

class CurlDownloadError(OSError):
    def __init__(self, returncode: int, status: int, message: str):
        super().__init__(f"curl退出码{returncode}，HTTP{status}：{message}")
        self.returncode, self.status = returncode, status

def curl_request(url: str, timeout: float, target: Path | None = None, year: int | None = None) -> dict:
    """通过同一官方URL下载；不调用shell，不改用其他数据源。"""
    executable = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if executable is None:
        raise ValueError("找不到curl下载工具。可使用--transport urllib，或安装curl后重试。")
    marker = "\nHMDA_TRANSFER_INFO\n"
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
            raise ValueError(f"需要完整ZIP的HTTP200响应，得到{response.status}。")
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


def parse_years(values: list[str]) -> list[int]:
    years = set()
    for value in values:
        for token in value.split(","):
            if "-" in token:
                first, last = map(int, token.split("-", 1))
                if first > last or first < 2020 or last > 2024:
                    raise ValueError("已核实年份为2020-2024，请使用有效的升序范围。")
                years.update(range(first, last + 1))
            else:
                year = int(token)
                if not 2020 <= year <= 2024:
                    raise ValueError("已核实年份为2020-2024。")
                years.add(year)
    return sorted(years)


def acs_urls(year: int, source: str) -> dict[str, str]:
    period = 1 if source == "acs1" else 5
    if source == "acs1" and year == 2020:
        raise ValueError("2020常规ACS1未发布。")
    root = f"https://www2.census.gov/programs-surveys/acs/summary_file/{year}"
    if year == 2020:
        base = root + "/prototype"
        return {"table": f"{base}/5YRData/acsdt5y2020-b23001.dat",
                "geography": f"{base}/Geos20205YR.csv",
                "labels": f"{base}/ACS2020_Table_Shells.csv"}
    base = root + "/table-based-SF"
    return {"table": f"{base}/data/{period}YRData/acsdt{period}y{year}-b23001.dat",
            "geography": f"{base}/documentation/Geos{year}{period}YR.txt",
            "labels": f"{base}/documentation/ACS{year}{period}YR_Table_Shells.txt"}


def normalized(value: str) -> str:
    return " ".join(value.strip().rstrip(":").split())


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


def verify_labels(path: Path, ages: list[str]) -> dict:
    labels, paths = {}, {}
    parents: list[str] = []
    has_indent = False
    for row in dict_rows(path):
        if row.get("Table ID") != "B23001":
            continue
        identifier = row.get("Unique ID", "")
        if not identifier:
            continue  # 2020原型文件含无字段编号的标题/空行。
        label = normalized(row.get("Label", row.get("Stub", "")))
        if identifier in labels:
            raise ValueError(f"重复字段标签：{identifier}")
        labels[identifier] = label
        if "Indent" in row:
            has_indent = True
            indent = int(float(row["Indent"]))
            if indent > len(parents):
                raise ValueError(f"标签缺少父级：{identifier}")
            parents = parents[:indent] + [label]
            paths[identifier] = tuple(parents)
    expected_leaves = [None, "In labor force", "In Armed Forces", "Civilian", "Employed", "Unemployed", "Not in labor force"]
    verified = {}
    for sex, shift in (("Male", 0), ("Female", 86)):
        if labels.get(f"B23001_{2 + shift:03}") != sex:
            raise ValueError("B23001性别父级字段与预期不符。")
        for age in ages:
            for base, age_label in AGE_BASES[age]:
                for offset, leaf in enumerate(expected_leaves):
                    identifier = f"B23001_{base + shift + offset:03}"
                    expected_leaf = age_label if offset == 0 else leaf
                    if labels.get(identifier) != expected_leaf:
                        raise ValueError(f"字段定义变化：{identifier}，预期{expected_leaf!r}。")
                    if has_indent:
                        suffix = [] if offset == 0 else (["Not in labor force"] if offset == 6 else ["In labor force"])
                        if offset == 2:
                            suffix += ["In Armed Forces"]
                        elif offset >= 3 and offset <= 5:
                            suffix += ["Civilian"] + ([] if offset == 3 else [leaf])
                        expected_path = ("Total", sex, age_label, *suffix)
                        if paths.get(identifier) != expected_path:
                            raise ValueError(f"字段父级与预期不符：{identifier}。")
                    verified[identifier] = list(paths.get(identifier, (labels[identifier],)))
    return {"checks": verified, "hierarchy_available": has_indent,
            "note": "2020原型表壳无Indent，核验性别父级、年龄块及各行标签；后续年份同时核验完整层级。"}


def select_geographies(path: Path, geography: str) -> dict:
    selected = {}
    for row in dict_rows(path):
        required = {"SUMLEVEL", "COMPONENT", "NAME"}
        if not required <= row.keys():
            raise ValueError("地理文件缺少必要字段。")
        if row["COMPONENT"] != "00":
            continue
        level = row["SUMLEVEL"]
        if geography == "msa":
            # 310包括Metro和Micro，按官方NAME后缀只取完整Metro；不取314都市分区。
            if level != "310" or not row["NAME"].endswith(" Metro Area"):
                continue
            code = row.get("CBSA", "")
        elif geography == "county":
            if level != "050":
                continue
            code = row.get("STATE", "") + row.get("COUNTY", "")
        elif geography == "state":
            if level != "040":
                continue
            code = row.get("STATE", "")
        else:
            if level != "010":
                continue
            code = "US"
        geo_id = row.get("GEO_ID", row.get("DADSID", ""))
        if not geo_id or not code:
            raise ValueError("选中地理缺少完整GEO_ID或区域代码。")
        if geo_id in selected or any(item["area_code"] == code for item in selected.values()):
            raise ValueError(f"所选地理不唯一：{geo_id}")
        selected[geo_id] = {"geo_id": geo_id, "area_code": code, "area_name": row["NAME"]}
    if not selected:
        raise ValueError("没有找到指定层级的地理，停止处理。")
    return selected


def count_value(value: str) -> int | None:
    if value.strip() in {"", "-", "N", "(X)", "null"}:
        return None
    number = int(value)
    return number if number >= 0 else None


def complete_sum(values: list[int | None]) -> int | None:
    return None if any(value is None for value in values) else sum(values)


def percent(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(100 * numerator / denominator, 6)


def employment_metrics(counts: dict) -> dict:
    total, army = counts["population"], counts["armed_forces"]
    civilian = None if total is None or army is None else total - army
    if civilian is not None and civilian < 0:
        raise ValueError("军队人数大于总人口。")
    return {**counts, "civilian_population": civilian,
            "employment_population_ratio_pct": percent(counts["employed"], total),
            "civilian_employment_population_ratio_pct": percent(counts["employed"], civilian),
            "civilian_labor_force_participation_rate_pct": percent(counts["civilian_labor_force"], civilian),
            "unemployment_rate_pct": percent(counts["unemployed"], counts["civilian_labor_force"])}


def extract_acs(table: Path, geos: dict, year: int, source: str, geography: str, ages: list[str]) -> list[dict]:
    records, seen = [], set()
    columns = {f"B23001_E{base + shift + offset:03}" for age in ages
               for base, _ in AGE_BASES[age] for shift in (0, 86) for offset in COUNT_OFFSETS.values()}
    for row in dict_rows(table):
        if not {"GEO_ID", *columns} <= row.keys():
            raise ValueError("B23001数据文件缺少所需列。")
        geo_id = row["GEO_ID"]
        if geo_id not in geos:
            continue
        if geo_id in seen:
            raise ValueError(f"数据地理重复：{geo_id}")
        seen.add(geo_id)
        for age in ages:
            counts = {name: complete_sum([count_value(row[f"B23001_E{base + shift + offset:03}"])
                                         for base, _ in AGE_BASES[age] for shift in (0, 86)])
                      for name, offset in COUNT_OFFSETS.items()}
            records.append({"source": "Census ACS", "dataset": source, "year": year,
                            "period_start": year if source == "acs1" else year - 4,
                            "period_end": year, "geography": geography, **geos[geo_id],
                            "age_group": age, **employment_metrics(counts),
                            "data_status": "ok" if all(value is not None for value in counts.values()) else "missing_component",
                            "note": "人数单位为人；总年龄人口含军队，civilian人口扣除军队；按该年发布地理。"})
    if not records:
        raise ValueError("所选地理没有数据记录，停止处理。")
    missing = set(geos) - seen
    if missing:
        LOGGER.warning("%s有%s个所选区域未发布B23001记录；保留缺失行并标记no_table_row。", year, len(missing))
        for geo_id in sorted(missing):
            for age in ages:
                counts = {name: None for name in COUNT_OFFSETS}
                records.append({"source": "Census ACS", "dataset": source, "year": year,
                                "period_start": year if source == "acs1" else year - 4,
                                "period_end": year, "geography": geography, **geos[geo_id],
                                "age_group": age, **employment_metrics(counts),
                                "data_status": "no_table_row", "note": "当年地理文件列出此区域，但B23001没有发布对应记录；人数与比例为空。"})
    return records


from decimal import Decimal, InvalidOperation

from hashlib import sha256

from html.parser import HTMLParser

from pathlib import Path

import re

_CPS_TABLE_TITLE = (
    "Employment status of the civilian noninstitutional population "
    "by age, sex, and race"
)

_CPS_ALLOWED_AGES = {"16-24", "20-24"}

_CPS_TARGET_LABELS = {"16 to 19 years": "16-19", "20 to 24 years": "20-24"}

def _cps_space(value):
    return " ".join(value.replace("\xa0", " ").split())

class _CPSHTMLTables(HTMLParser):
    """Read rows and rendered text while retaining explicit group headings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.text = []
        self._stack = []
        self._ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignored += 1
            return
        if self._ignored:
            return
        if tag == "table":
            self._stack.append({"rows": [], "text": [], "row": None, "cell": None})
        elif self._stack:
            frame = self._stack[-1]
            if tag == "tr":
                frame["row"] = []
            elif tag in {"th", "td"} and frame["row"] is not None:
                frame["cell"] = []
            elif tag == "br" and frame["cell"] is not None:
                frame["cell"].append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._ignored:
            self._ignored -= 1
            return
        if self._ignored or not self._stack:
            return
        frame = self._stack[-1]
        if tag in {"th", "td"} and frame["cell"] is not None:
            frame["row"].append(_cps_space("".join(frame["cell"])))
            frame["cell"] = None
        elif tag == "tr" and frame["row"] is not None:
            frame["rows"].append(frame["row"])
            frame["row"] = None
            frame["cell"] = None
        elif tag == "table":
            completed = self._stack.pop()
            completed.pop("row")
            completed.pop("cell")
            self.tables.append(completed)

    def handle_data(self, data):
        if self._ignored:
            return
        self.text.append(data)
        for frame in self._stack:
            frame["text"].append(data)
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"].append(data)

def _cps_decimal(value):
    try:
        number = Decimal(value.replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"CPS Table 3 contains a nonnumeric value: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"CPS Table 3 contains an invalid numeric value: {value!r}")
    return number

def _cps_decode_age_row(row):
    if len(row) != 9:
        raise ValueError(f"CPS Table 3 age row requires 9 cells, got {len(row)}: {row!r}")
    values = [_cps_decimal(cell) for cell in row[1:]]
    count_names = {
        0: "population",
        1: "civilian_labor_force",
        3: "employed",
        5: "unemployed",
        7: "not_in_labor_force",
    }
    counts = {}
    for position, name in count_names.items():
        if values[position] != values[position].to_integral_value():
            raise ValueError(f"CPS Table 3 {name} is not a whole thousand")
        counts[name] = int(values[position]) * 1000
    rates = {
        "labor_force_participation_rate_pct": float(values[2]),
        "employment_population_ratio_pct": float(values[4]),
        "unemployment_rate_pct": float(values[6]),
    }
    if counts["population"] <= 0 or counts["civilian_labor_force"] <= 0:
        raise ValueError("CPS Table 3 has a zero population or labor-force denominator")
    if any(rate > 100 for rate in rates.values()):
        raise ValueError("CPS Table 3 contains a percentage outside 0 to 100")
    # Counts are independently rounded to thousands; do not alter them to force
    # exact accounting identities. Two thousand is a conservative row tolerance.
    if abs(counts["employed"] + counts["unemployed"] - counts["civilian_labor_force"]) > 2000:
        raise ValueError("CPS employment/unemployment identity exceeds rounding tolerance")
    if abs(counts["civilian_labor_force"] + counts["not_in_labor_force"] - counts["population"]) > 2000:
        raise ValueError("CPS population identity exceeds rounding tolerance")
    calculated = _cps_rates(counts)
    if any(abs(rates[name] - calculated[name]) > 0.15 for name in rates):
        raise ValueError("CPS published percentages do not match the row's numeric columns")
    return counts, rates

def _cps_rates(counts):
    return {
        "labor_force_participation_rate_pct": 100.0 * counts["civilian_labor_force"] / counts["population"],
        "employment_population_ratio_pct": 100.0 * counts["employed"] / counts["population"],
        "unemployment_rate_pct": 100.0 * counts["unemployed"] / counts["civilian_labor_force"],
    }

def parse_cps_annual_table3(html, year, age_group):
    """Return ``(record, metadata)`` from an already downloaded official HTML.

    Only the explicit first TOTAL block is used. Missing or duplicate requested
    age rows fail validation; later Men/Women/race rows cannot replace them.
    """
    if year not in range(2020, 2025):
        raise ValueError("CPS annual mode supports only years 2020 through 2024")
    if age_group not in _CPS_ALLOWED_AGES:
        raise ValueError("CPS annual mode supports only age_group='20-24' or '16-24'")
    parser = _CPSHTMLTables()
    parser.feed(html)
    parser.close()
    document_text = _cps_space(" ".join(parser.text))
    if not re.search(r"\bANNUAL\s+AVERAGES\b", document_text, re.IGNORECASE):
        raise ValueError("CPS response is not an annual-average table")
    title_pattern = r"\b3\s*\.\s*" + re.escape(_CPS_TABLE_TITLE)
    if not re.search(title_pattern, document_text, re.IGNORECASE):
        raise ValueError("CPS response is not the expected annual Table 3")
    if "numbers in thousands" not in document_text.lower():
        raise ValueError("CPS Table 3 does not declare its expected thousand-person units")

    candidates = []
    for table in parser.tables:
        rows = table["rows"]
        labels = {_cps_space(row[0]).upper() for row in rows if row}
        if "TOTAL" in labels and "20 TO 24 YEARS" in labels:
            candidates.append(table)
    if len(candidates) != 1:
        raise ValueError(f"Expected one CPS Table 3 data table, found {len(candidates)}")
    rows = candidates[0]["rows"]
    # Require the requested year in an actual table header, rather than relying
    # on a footer's modification date or the requested filename.
    if not any(len(row) < 9 and str(year) in row for row in rows):
        raise ValueError(f"CPS Table 3 does not have the requested year header {year}")

    active = False
    total_seen = False
    age_rows = {}
    for row in rows:
        if not row:
            continue
        label = _cps_space(row[0])
        if label.upper() == "TOTAL":
            if total_seen:
                raise ValueError("CPS Table 3 repeats a TOTAL block")
            active = True
            total_seen = True
            continue
        if not active:
            continue
        if label.lower() in {"men", "women"} or (
            label and not re.match(r"^\d+\s+(?:years|to)\b", label, re.IGNORECASE)
            and len(row) < 9
        ):
            break
        if label in _CPS_TARGET_LABELS:
            canonical = _CPS_TARGET_LABELS[label]
            if canonical in age_rows:
                raise ValueError(f"Duplicate age row in CPS TOTAL block: {label}")
            age_rows[canonical] = _cps_decode_age_row(row)

    wanted = ["20-24"] if age_group == "20-24" else ["16-19", "20-24"]
    missing = [age for age in wanted if age not in age_rows]
    if missing:
        raise ValueError(f"CPS TOTAL block is missing age groups: {', '.join(missing)}")
    count_keys = [
        "population", "employed", "unemployed", "civilian_labor_force", "not_in_labor_force"
    ]
    counts = {name: sum(age_rows[age][0][name] for age in wanted) for name in count_keys}
    if age_group == "20-24":
        rates = age_rows["20-24"][1].copy()
        rate_method = "official_published_age_group_percentages"
    else:
        rates = _cps_rates(counts)
        rate_method = "ratios_of_summed_rounded_age_group_levels"
    note = (
        "CPS/BLS annual-average Table 3; United States; both sexes and all races; "
        "civilian noninstitutional population. Source levels are in thousands; "
        "stored counts equal published levels times 1,000 and retain source rounding. "
        "This is an annual average, not a July youth-employment estimate."
    )
    if age_group == "16-24":
        note += (
            " Ages 16-24 are the sum of disjoint 16-19 and 20-24 groups; percentages "
            "are recomputed from their summed levels, not averaged from group percentages."
        )
    record = {"year": year, "age_group": age_group, **counts, **rates, "note": note}
    metadata = {
        "source": "U.S. Bureau of Labor Statistics, Current Population Survey",
        "table": 3,
        "table_title": _CPS_TABLE_TITLE,
        "year": year,
        "geography": "United States",
        "frequency": "annual_average",
        "source_count_units": "thousands_of_persons",
        "output_count_units": "persons_with_thousand_person_source_rounding",
        "source_age_groups": wanted,
        "percentage_method": rate_method,
        "source_published_percentages": {age: age_rows[age][1] for age in wanted},
    }
    return record, metadata

def cps_annual_employment(year, age_group, raw_dir, fetch, timeout=45, retries=3, refresh=False):
    """Download one official annual Table 3 and return record/path/metadata.

    Signature: ``(year: int, age_group: str, raw_dir: str|Path, fetch: callable,
    timeout: int=45, retries: int=3, refresh: bool=False) -> (dict, Path, dict)``.

    The injected fetch callback must write the requested original HTML to path.
    The callback owns directory creation, retry/backoff and cache/refresh policy.
    """
    if year not in range(2020, 2025):
        raise ValueError("CPS annual mode supports only years 2020 through 2024")
    if age_group not in _CPS_ALLOWED_AGES:
        raise ValueError("CPS annual mode supports only age_group='20-24' or '16-24'")
    prefix = "https://www.bls.gov/cps/" + ("data/" if year >= 2023 else "")
    url = f"{prefix}aa{year}/cpsaat03.htm"
    raw_path = Path(raw_dir).resolve() / f"cps_annual_table3_{year}.html"
    download_metadata = fetch(url, raw_path, timeout, retries, refresh)
    raw_bytes = raw_path.read_bytes()
    if not raw_bytes:
        raise ValueError("CPS fetch callback saved an empty response")
    html = raw_bytes.decode("utf-8-sig")
    record, metadata = parse_cps_annual_table3(html, year, age_group)
    metadata.update({
        "source_url": url,
        "raw_path": str(raw_path),
        "sha256": sha256(raw_bytes).hexdigest(),
        "raw_bytes": len(raw_bytes),
    })
    if isinstance(download_metadata, dict):
        metadata["download_metadata"] = download_metadata
    return record, raw_path, metadata


def write_csv(target: Path, records: list[dict]) -> None:
    temporary = target.with_name(target.name + ".part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    os.replace(temporary, target)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=["acs1", "acs5", "cps"], default="acs1", help="默认ACS1；ACS5是五年估计；CPS为全国年度平均。")
    parser.add_argument("--years", nargs="+", default=["2020-2024"], help="默认2020-2024；ACS1的2020明确记录为不可用。")
    parser.add_argument("--geo", choices=["msa", "county", "state", "us"], default="msa", help="默认完整MSA；CPS须指定us。")
    parser.add_argument("--ages", nargs="+", choices=list(AGE_BASES), default=["20-24"], help="默认20-24；可选16-24、25-34、35-44。CPS仅支持前两种。")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "employment_data", help="默认脚本旁employment_data。")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--refresh", action="store_true", help="重新下载官方原文件并替换缓存。")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        years = parse_years(args.years)
        ages = list(dict.fromkeys(args.ages))
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 86400 or args.retries < 1:
            raise ValueError("timeout应为0-86400的有限正数，retries至少为1。")
        if args.source == "cps" and (args.geo != "us" or set(ages) - {"16-24", "20-24"}):
            raise ValueError("CPS仅支持全国：请使用--geo us，以及--ages 16-24或20-24。")
        root = args.out.resolve() / args.source
        raw, data, metadata_dir = root / "raw", root / "data", root / "metadata"
        for directory in (raw, data, metadata_dir):
            directory.mkdir(parents=True, exist_ok=True)
        stem = f"employment_{args.source}_{args.geo}_{'_'.join(ages)}"
        receipt = metadata_dir / f"{stem}.json"
        if receipt.exists():
            receipt.unlink()
        records, statuses, sources, checks = [], [], [], {}
        for year in years:
            if args.source == "acs1" and year == 2020:
                LOGGER.warning("2020常规ACS1未发布：标为不可用，不补0、不用ACS5替换。")
                statuses.append({"year": 2020, "status": "unavailable", "rows": 0,
                                 "reason": "常规ACS1未发布；实验表不能构成MSA B23001", "official_notice": MISSING_2020_URL})
                continue
            if args.source == "cps":
                for age in ages:
                    record, _, source_metadata = cps_annual_employment(year, age, raw, fetch, args.timeout, args.retries, args.refresh)
                    # CPS的population本身就是civilian noninstitutional population。
                    counts = {name: record[name] for name in COUNT_OFFSETS if name != "armed_forces"}
                    counts["armed_forces"] = 0
                    metrics = employment_metrics(counts)
                    # 单一20-24年龄组保留BLS发布率；合并16-24由两组人数重算。
                    metrics.update({
                        "employment_population_ratio_pct": record["employment_population_ratio_pct"],
                        "civilian_employment_population_ratio_pct": record["employment_population_ratio_pct"],
                        "civilian_labor_force_participation_rate_pct": record["labor_force_participation_rate_pct"],
                        "unemployment_rate_pct": record["unemployment_rate_pct"],
                    })
                    records.append({"source": "BLS CPS", "dataset": "cps", "year": year,
                                    "period_start": year, "period_end": year, "geography": "us",
                                    "geo_id": "US", "area_code": "US", "area_name": "United States",
                                    "age_group": age, **metrics, "data_status": "ok",
                                    "note": record["note"]})
                    sources.append(source_metadata)
            else:
                urls = acs_urls(year, args.source)
                paths = {}
                for role, url in urls.items():
                    path = raw / f"{year}_{role}_{url.rsplit('/', 1)[1]}"
                    sources.append(fetch(url, path, args.timeout, args.retries, args.refresh))
                    paths[role] = path
                checks[str(year)] = verify_labels(paths["labels"], ages)
                geographies = select_geographies(paths["geography"], args.geo)
                records.extend(extract_acs(paths["table"], geographies, year, args.source, args.geo, ages))
            count = sum(record["year"] == year for record in records)
            statuses.append({"year": year, "status": "downloaded", "rows": count,
                             "rows_no_table_row": sum(record["year"] == year and record["data_status"] == "no_table_row" for record in records)})
            LOGGER.info("%s完成：%s条地区×年龄记录。", year, count)
        if not records:
            write_json(metadata_dir / "availability.json", {"requested_years": years, "years": statuses})
            raise ValueError("请求中没有可用数据；已保存availability.json说明。")
        target = data / f"{stem}.csv"
        write_csv(target, records)
        write_json(receipt, {"requested_years": years, "years": statuses, "source": args.source,
                             "geography": args.geo, "ages": ages, "rows": len(records),
                             "output_file": str(target), "output_sha256": sha256_file(target),
                             "script_sha256": sha256_file(Path(__file__)), "download_sources": sources,
                             "field_checks": checks, "processed_at_utc": utc_now(),
                             "count_unit": "people", "rate_unit": "percent, not decimal fraction",
                             "missing_rule": "Blank or negative Census special values -> missing; any missing component -> missing sum; zero denominator -> missing rate.",
                             "acs5_note": "每个年份表示截至该年的滚动五年估计；相邻窗口重叠，不是单年冲击。",
                             "geography_note": "各年度发布地理；未执行固定2021县成员集合验证。"})
        LOGGER.info("完成：%s。年度可用性见%s", target, receipt)
        return 0
    except KeyboardInterrupt:
        LOGGER.error("已中断；.part不是完整数据，再次运行复用已校验的原文件。")
        return 130
    except (ValueError, OSError, HTTPException, csv.Error) as exc:
        LOGGER.error("未完成：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
