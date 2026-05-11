#!/usr/bin/env python3
"""
从 cmliu 社区维护的 ProxyIP 仓库拉取节点,按 country code 过滤后批量测试。

数据源:
    https://zip.cm.edu.kg/all.json
    格式: {"data": [{"ip": "1.2.3.4", "port": [443], "meta": {"country": "SG", ...}}, ...]}

用法:
    uv run test_remote.py                 # 默认 SG
    uv run test_remote.py -C SG           # 显式 SG
    uv run test_remote.py -C SG,JP,US     # 多国
    uv run test_remote.py -C SG -c 30 -o sg.csv
    uv run test_remote.py -C SG --limit 100   # 只取前 100 个(调试用)
"""

import argparse
import asyncio
import json
import sys

import aiohttp

from main import run_tests

DEFAULT_SOURCE_URL = "https://zip.cm.edu.kg/all.json"


def filter_payload(payload: dict, countries: set[str], limit: int | None = None) -> list[tuple[str, int]]:
    """按国家代码过滤 JSON payload,展开为 [(ip, port), ...]"""
    items: list[tuple[str, int]] = []
    for entry in payload.get("data", []):
        meta = entry.get("meta") or {}
        country = meta.get("country", "")
        if countries and country not in countries:
            continue
        ip = entry.get("ip")
        ports = entry.get("port") or [443]
        if not ip:
            continue
        for port in ports:
            try:
                items.append((ip, int(port)))
            except (TypeError, ValueError):
                continue
    if limit and limit > 0:
        items = items[:limit]
    return items


async def fetch_proxyips(url: str, countries: set[str], limit: int | None = None) -> list[tuple[str, int]]:
    """拉取远程 JSON,展开为 [(ip, port), ...],按国家代码过滤。"""
    # 数据源对默认 aiohttp UA 返回 403,用 curl 的 UA 绕过(住宅 IP 下有效;
    # 数据中心 IP 如 GitHub Actions 仍可能被 Cloudflare 拦截,改用 --input-json)
    headers = {"User-Agent": "curl/8.7.1"}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        async with s.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
    return filter_payload(payload, countries, limit)

    items: list[tuple[str, int]] = []
    for entry in payload.get("data", []):
        meta = entry.get("meta") or {}
        country = meta.get("country", "")
        if countries and country not in countries:
            continue
        ip = entry.get("ip")
        ports = entry.get("port") or [443]
        if not ip:
            continue
        for port in ports:
            try:
                items.append((ip, int(port)))
            except (TypeError, ValueError):
                continue

    if limit and limit > 0:
        items = items[:limit]
    return items


async def main():
    parser = argparse.ArgumentParser(
        description="从 cmliu 社区源拉取并测试 ProxyIP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-C", "--countries", default="SG",
                        help="国家代码,逗号分隔(默认 SG)。例: SG / SG,JP,US。空字符串表示不过滤")
    parser.add_argument("-u", "--url", default=DEFAULT_SOURCE_URL,
                        help=f"数据源 URL(默认 {DEFAULT_SOURCE_URL})")
    parser.add_argument("--input-json", default="",
                        help="从本地 JSON 文件读取(跳过 HTTP 抓取,用于 GH Actions 等被 CF 拦的环境)")
    parser.add_argument("-o", "--output", default="proxyip_report.csv",
                        help="输出 CSV 路径(默认 proxyip_report.csv)")
    parser.add_argument("-c", "--concurrency", type=int, default=30,
                        help="并发数(默认 30)")
    parser.add_argument("-t", "--timeout", type=float, default=6.0,
                        help="单个请求超时秒数(默认 6)")
    parser.add_argument("--limit", type=int, default=0,
                        help="只取前 N 个(默认 0=不限制,调试用)")
    args = parser.parse_args()

    countries = {c.strip().upper() for c in args.countries.split(",") if c.strip()}

    if args.input_json:
        label = f"(来源 {args.input_json}, 过滤 {','.join(sorted(countries)) or '不过滤'})"
        print(f"📂 从本地文件读取: {args.input_json}")
        try:
            with open(args.input_json, "r", encoding="utf-8") as f:
                payload = json.load(f)
            proxyips = filter_payload(payload, countries, args.limit or None)
        except Exception as e:
            print(f"❌ 读取失败: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        label = f"(来源 {args.url}, 过滤 {','.join(sorted(countries)) or '不过滤'})"
        print(f"⬇  正在拉取节点列表: {args.url}")
        try:
            proxyips = await fetch_proxyips(args.url, countries, args.limit or None)
        except Exception as e:
            print(f"❌ 拉取失败: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)

    if not proxyips:
        print(f"❌ 过滤后没有节点(国家={countries})", file=sys.stderr)
        sys.exit(1)

    await run_tests(proxyips, args.output, args.concurrency, args.timeout, label=label)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹  已中断")
        sys.exit(130)
