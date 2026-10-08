#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile


LOGGER = logging.getLogger("hmda")
MIN_YEAR, MAX_YEAR = 2018, 2025
BLOCK_SIZE = 1024 * 1024
BASE_URL = "https://files.ffiec.cfpb.gov/static-data/snapshot"
APPLICANT_AGE_FILTER = "<25"
SCHEMA_URL = (
    "https://ffiec.cfpb.gov/documentation/publications/"
    "loan-level-datasets/public-lar-schema"
)
VALID_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI "
    "MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT "
    "VA WA WV WI WY AS GU MP PR VI".split()
)

# 全部以字符串写出；编码解释见自动生成的 field_dictionary.json。
# 包含用户所需信息、年份/机构，以及研究筛样本和解释审批所需的辅助字段。
FIELDS = [
    "activity_year", "lei", "applicant_age", "co_applicant_age", "income",
    "applicant_race_1", "applicant_race_2", "applicant_race_3",
    "applicant_race_4", "applicant_race_5", "derived_race",
    "loan_amount", "debt_to_income_ratio", "rate_spread", "interest_rate",
    "action_taken", "state_code", "county_code", "census_tract",
    "loan_type", "loan_purpose", "lien_status", "occupancy_type",
    "construction_method", "total_units", "reverse_mortgage",
    "open_end_line_of_credit", "business_or_commercial_purpose", "preapproval",
    "denial_reason_1", "denial_reason_2", "denial_reason_3", "denial_reason_4",
    "combined_loan_to_value_ratio", "loan_term",
]

FIELD_NOTES = {
    "activity_year": "数据报告年度，不是精确申请日期。",
    "lei": "报告机构的 Legal Entity Identifier。",
    "applicant_age": "主申请人公开年龄组：<25、25-34、35-44、45-54、55-64、65-74、>74；8888未知。",
    "co_applicant_age": "共同申请人公开年龄组；8888未知，9999无共同申请人。",
    "income": "贷款决策/申请处理依赖的年毛收入，单位千美元；可能包含共同申请人收入。",
    "applicant_race_1": "主申请人种族原始编码；可能多选，须联合race_1至race_5解释。",
    "applicant_race_2": "主申请人第2种族编码；空值原样保留。",
    "applicant_race_3": "主申请人第3种族编码；空值原样保留。",
    "applicant_race_4": "主申请人第4种族编码；空值原样保留。",
    "applicant_race_5": "主申请人第5种族编码；空值原样保留。",
    "derived_race": "官方衍生种族分类，可能同时反映主申请人和共同申请人；不等同于主申请人原始种族。",
    "loan_amount": "申请或发放的贷款金额，美元；公开值经过隐私分档处理。",
    "debt_to_income_ratio": "DTI公开分组/单值/NA/Exempt；保持字符串，不计算任意区间中点。",
    "rate_spread": "APR相对可比APOR的利差，百分比点；换成基点乘100，NA/Exempt不作0。",
    "interest_rate": "贷款利率，百分比；不等于rate_spread。",
    "action_taken": "1发放；2批准未接受；3拒贷；4撤回；5不完整结案；6购买贷款；7预批拒绝；8预批批准未接受。",
    "state_code": "物业所在州的两字母代码；不是申请人的工作州。",
    "county_code": "物业所在县的五位州县FIPS字符串，保留前导零。",
    "census_tract": "物业所在普查区的十一位标识字符串，保留前导零。",
    "loan_type": "1常规；2 FHA；3 VA；4 RHS/FSA。",
    "loan_purpose": "1购房；2住房改善；31再融资；32现金取出再融资；4其他；5不适用。",
    "lien_status": "1第一留置权；2次级留置权。",
    "occupancy_type": "1主要自住；2第二住宅；3投资物业。",
    "construction_method": "1现场建造；2预制住宅。",
    "total_units": "物业单元数；1、2、3、4及更大的公开分组。",
    "reverse_mortgage": "1反向按揭；2非反向；1111豁免。",
    "open_end_line_of_credit": "1开放式信贷；2非开放式；1111豁免。",
    "business_or_commercial_purpose": "1主要商业用途；2非商业；1111豁免。",
    "preapproval": "1要求预批；2未要求预批。",
    "denial_reason_1": "拒贷原因第1项；包括1 DTI、2就业历史、6不可验证信息等，完整编码见官方字典。",
    "denial_reason_2": "拒贷原因第2项；不强制互斥。",
    "denial_reason_3": "拒贷原因第3项。",
    "denial_reason_4": "拒贷原因第4项。",
    "combined_loan_to_value_ratio": "总贷款价值比，百分比；2018原始CSV列名loan_to_value_ratio，统一命名并在元数据记录；NA/Exempt原样保留。",
    "loan_term": "贷款期限，月；NA/Exempt原样保留。",
}


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


def parse_years(values: list[str]) -> list[int]:
    years = set()
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if not token:
                raise ValueError("年份不能为空。")
            if "-" in token:
                first, last = map(int, token.split("-", 1))
                if first > last:
                    raise ValueError(f"年份区间顺序错误：{token}")
                if first < MIN_YEAR or last > MAX_YEAR:
                    raise ValueError(f"当前已核实的静态快照范围是{MIN_YEAR}-{MAX_YEAR}。")
                years.update(range(first, last + 1))
            else:
                year = int(token)
                if not MIN_YEAR <= year <= MAX_YEAR:
                    raise ValueError(f"当前已核实的静态快照范围是{MIN_YEAR}-{MAX_YEAR}。")
                years.add(year)
    return sorted(years)


def parse_states(values: list[str] | None) -> list[str]:
    states = sorted({v.strip().upper() for value in (values or []) for v in value.split(",")})
    invalid = set(states) - VALID_STATES
    if invalid:
        raise ValueError(f"未知州/地区代码：{', '.join(sorted(invalid))}")
    return states


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


def check_url(year: int, timeout: float, transport: str = "auto") -> None:
    url = f"{BASE_URL}/{year}/{year}_public_lar_csv.zip"
    if transport != "curl":
        try:
            with urlopen(request(url, "HEAD"), timeout=timeout) as response:
                LOGGER.info("%s可访问；压缩大小=%s字节；URL=%s", year, response.headers.get("Content-Length", "未知"), response.geturl())
                return
        except HTTPError as exc:
            if transport != "auto" or exc.code != 403:
                raise
            LOGGER.info("Python请求被拒绝；改用curl访问同一官方文件。")
    result = curl_request(url, timeout)
    LOGGER.info("%s可访问；压缩大小=%s字节；URL=%s（curl）", year, result["headers"].get("content-length", "未知"), result["resolved_url"])


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


def download_archive(year: int, raw_dir: Path, timeout: float, retries: int, refresh: bool, transport: str = "auto") -> tuple[Path, dict]:
    url = f"{BASE_URL}/{year}/{year}_public_lar_csv.zip"
    target = raw_dir / f"{year}_public_lar_csv.zip"
    provenance = raw_dir / f"{year}_source.json"
    temporary = target.with_name(target.name + ".part")

    if target.exists() and not refresh:
        if not provenance.exists():
            raise ValueError(f"{target}缺少下载记录；请加--refresh从官方重新获取。")
        metadata = json.loads(provenance.read_text(encoding="utf-8"))
        if metadata.get("source_url") != url or metadata.get("sha256") != sha256_file(target):
            raise ValueError(f"{target}与来源记录不一致；请检查文件，或加--refresh重新获取。")
        if not zipfile.is_zipfile(target):
            raise ValueError(f"缓存不是有效ZIP：{target}")
        LOGGER.info("%s复用已通过SHA-256校验的原始ZIP。", year)
        return target, metadata

    retryable = (URLError, TimeoutError, socket.timeout, HTTPException)
    selected_transport = transport
    for attempt in range(1, retries + 1):
        try:
            LOGGER.info("下载%s（尝试%s/%s）...", year, attempt, retries)
            if selected_transport != "curl":
                try:
                    result = download_using_urllib(url, temporary, timeout, year)
                except HTTPError as exc:
                    if selected_transport != "auto" or exc.code != 403:
                        raise
                    LOGGER.info("Python请求被拒绝；改用curl访问同一官方文件。")
                    selected_transport = "curl"
            if selected_transport == "curl":
                result = curl_request(url, timeout, temporary, year)
                result["bytes"] = temporary.stat().st_size
                result["sha256"] = sha256_file(temporary)
                if result["reported_bytes"] != result["bytes"]:
                    raise HTTPException("curl报告的下载字节数与文件大小不一致。")
            received, headers = result["bytes"], result["headers"]
            expected = int(headers["content-length"]) if "content-length" in headers else None
            metadata = {
                "dataset": "Snapshot National Loan-Level Dataset",
                "year": year, "source_url": url, "resolved_url": result["resolved_url"],
                "publication_page": f"https://ffiec.cfpb.gov/data-publication/snapshot-national-loan-level-dataset/{year}",
                "downloaded_at_utc": utc_now(), "bytes": received,
                "sha256": result["sha256"], "download_transport": result["download_transport"],
                "etag": headers.get("etag"), "last_modified": headers.get("last-modified"),
            }
            if expected is not None and received != expected:
                raise HTTPException(f"下载不完整：预计{expected}字节，收到{received}。")
            if not zipfile.is_zipfile(temporary):
                raise ValueError("官方响应不是ZIP；可能为错误页，未保存为完整数据。")
            os.replace(temporary, target)
            write_json(provenance, metadata)
            return target, metadata
        except HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == retries:
                raise
            LOGGER.warning("HTTP%s；稍后重试。", exc.code)
        except CurlDownloadError as exc:
            temporary_http = exc.status in {408, 429, 500, 502, 503, 504}
            temporary_network = exc.status < 400 and exc.returncode in {5, 6, 7, 18, 28, 35, 52, 55, 56, 92}
            if attempt == retries or not (temporary_http or temporary_network):
                raise
            LOGGER.warning("%s；稍后从头重试。", exc)
        except retryable as exc:
            if attempt == retries:
                raise
            LOGGER.warning("网络下载未完成：%s；稍后从头重试。", exc)
        time.sleep(min(2 ** attempt, 30))
    raise RuntimeError("未能完成下载。")


def extract_fields(archive_path: Path, year: int, target: Path, states: list[str]) -> dict:
    """逐条读取ZIP中的CSV，仅输出主申请人年龄<25且符合可选州筛选的记录。"""
    temporary = target.with_name(target.name + ".part")
    source_rows = output_rows = 0
    age_matched_rows = 0
    selected_states = set(states)
    with zipfile.ZipFile(archive_path) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".csv") and not n.startswith("__MACOSX/")]
        if len(members) != 1:
            raise ValueError(f"需要唯一LAR CSV文件，实际发现{members}；不自动混合多个文件。")
        member = members[0]
        with archive.open(member) as binary_source, io.TextIOWrapper(binary_source, encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, strict=True)
            header = next(reader, None)
            if header is None:
                raise ValueError("原始CSV为空。")
            if len(set(header)) != len(header):
                raise ValueError("原始CSV存在重复列名。")
            source_columns = {name: name for name in FIELDS}
            # 实际官方2018快照使用此历史列名；仅对该年明确映射，并保留映射记录。
            if year == 2018 and "combined_loan_to_value_ratio" not in header and "loan_to_value_ratio" in header:
                source_columns["combined_loan_to_value_ratio"] = "loan_to_value_ratio"
            missing = sorted(set(source_columns.values()) - set(header))
            if missing:
                raise ValueError(f"{year}官方文件缺少所需字段：{missing}；停止而非静默补空。")
            positions = [header.index(source_columns[name]) for name in FIELDS]
            year_index, state_index = header.index("activity_year"), header.index("state_code")
            age_index = header.index("applicant_age")

            # mtime=0且filename为空，使相同输入与配置生成相同gzip字节。
            with temporary.open("wb") as raw_output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0, compresslevel=1) as compressed:
                    with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as output:
                        writer = csv.writer(output, lineterminator="\n")
                        writer.writerow(FIELDS)
                        for row in reader:
                            if not row:
                                continue
                            source_rows += 1
                            if len(row) != len(header):
                                raise ValueError(f"第{source_rows}条记录列数错误：{len(row)}，预期{len(header)}。")
                            if row[year_index] != str(year):
                                raise ValueError(f"记录年度{row[year_index]!r}与请求{year}不一致。")
                            # 公开HMDA年龄为分组字符串；精确匹配<25，不用共同申请人年龄。
                            age_matches = row[age_index] == APPLICANT_AGE_FILTER
                            if age_matches:
                                age_matched_rows += 1
                            if age_matches and (not selected_states or row[state_index] in selected_states):
                                writer.writerow([row[i] for i in positions])
                                output_rows += 1
                            if source_rows % 500_000 == 0:
                                LOGGER.info("%s已读取%s条；保留%s条。", year, f"{source_rows:,}", f"{output_rows:,}")
        # 读取到EOF时zipfile会检查该成员CRC；错误不会发布正式输出。
    os.replace(temporary, target)
    return {
        "archive_member": member, "source_rows": source_rows, "output_rows": output_rows,
        "applicant_age_filter": APPLICANT_AGE_FILTER, "age_matched_rows": age_matched_rows,
        "fields": FIELDS, "states": states, "output_file": str(target.resolve()),
        "source_columns": source_columns,
        "output_sha256": sha256_file(target), "output_bytes": target.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    # Windows终端及重定向日志统一使用UTF-8，避免中文说明乱码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", nargs="+", default=["2021-2025"], help="年份，支持2023 2024 2025或2021-2025；默认只下载2021-2025五年。")
    parser.add_argument("--states", nargs="+", help="可选州代码，如CA NY；本地筛选，仍须下载全国ZIP。")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "hmda_data", help="输出目录；默认脚本旁的hmda_data。")
    parser.add_argument("--timeout", type=float, default=120, help="网络连接/每次读取超时秒数，不是整项下载总时限。")
    parser.add_argument("--transport", choices=["auto", "urllib", "curl"], default="auto", help="默认Python请求，遇HTTP403改用curl访问同一URL；也可强制指定。")
    parser.add_argument("--retries", type=int, default=3, help="网络下载最多尝试次数。")
    parser.add_argument("--refresh", action="store_true", help="从官方重新下载，替换已有原始ZIP及来源记录。")
    parser.add_argument("--check", action="store_true", help="仅HEAD检查所选年度链接，不下载全国数据、不写数据文件。")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        years, states = parse_years(args.years), parse_states(args.states)
        if not math.isfinite(args.timeout) or args.timeout <= 0 or args.retries < 1:
            raise ValueError("timeout必须为有限正数，retries必须≥1。")
        if args.check:
            for year in years:
                check_url(year, args.timeout, args.transport)
            return 0
        root = args.out.resolve()
        raw_dir, data_dir, metadata_dir = root / "raw", root / "data", root / "metadata"
        for directory in (raw_dir, data_dir, metadata_dir):
            directory.mkdir(parents=True, exist_ok=True)
        write_json(root / "field_dictionary.json", {
            "sample_filter": {"applicant_age": APPLICANT_AGE_FILTER},
            "official_schema": SCHEMA_URL,
            "official_field_definitions": "https://ffiec.cfpb.gov/documentation/publications/loan-level-datasets/lar-data-fields/",
            "storage": "CSV中的原始文本值；NA、Exempt、空值、DTI/年龄分组不改写。读取分析文件时请dtype=str, keep_default_na=False。",
            "fields": FIELD_NOTES,
        })
        tag = "_".join(states) if states else "ALL"
        LOGGER.info("仅保留主申请人applicant_age为%s的记录；年度：%s。", APPLICANT_AGE_FILTER, ", ".join(map(str, years)))
        for year in years:
            archive, provenance = download_archive(year, raw_dir, args.timeout, args.retries, args.refresh, args.transport)
            target = data_dir / f"hmda_{year}_{tag}_age_lt25.csv.gz"
            job_metadata = metadata_dir / f"hmda_{year}_{tag}_age_lt25.json"
            # 一旦开始替换数据，旧作业验收不再有效；先移除旧验收文件。
            if job_metadata.exists():
                job_metadata.unlink()
            details = extract_fields(archive, year, target, states)
            write_json(job_metadata, {
                **provenance, **details, "processed_at_utc": utc_now(),
                "script_sha256": sha256_file(Path(__file__)), "python_version": sys.version,
            })
            LOGGER.info("%s完成：%s条、%s列；%s", year, f"{details['output_rows']:,}", len(FIELDS), target)
        LOGGER.info("全部完成。原始ZIP、字段字典与来源/校验记录保存在%s", root)
        return 0
    except KeyboardInterrupt:
        LOGGER.error("已中断；.part文件不是完整数据。再次运行会重新处理未完成步骤。")
        return 130
    except HTTPError as exc:
        if exc.code == 403:
            LOGGER.error(
                "官方服务器返回HTTP403（访问被拒绝）：%s。请在能正常访问FFIEC的网络重试；"
                "脚本不会把错误网页当成数据，也不会自动改用不同版本的数据源。", exc.url,
            )
        else:
            LOGGER.error("HTTP%s，未完成：%s", exc.code, exc.url)
        return 1
    except (ValueError, OSError, URLError, HTTPException, zipfile.BadZipFile, csv.Error, EOFError) as exc:
        LOGGER.error("未完成：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
