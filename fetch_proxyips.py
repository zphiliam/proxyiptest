#!/usr/bin/env python3
"""
从 cmliu 社区 ProxyIP 仓库拉取节点列表,按国家代码筛选后写入文本文件。
生成的文件可直接给 main.py 读取测试。

数据源:
    https://zip.cm.edu.kg/all.json

用法:
    uv run fetch_proxyips.py             # 默认 SG → SG.txt
    uv run fetch_proxyips.py -C JP       # JP → JP.txt
    uv run fetch_proxyips.py -C SG -o SG.txt
    uv run fetch_proxyips.py -C SG,JP -o asia.txt
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone

import aiohttp

DEFAULT_SOURCE_URL = "https://zip.cm.edu.kg/all.json"


async def fetch_proxyips(url: str, countries: set[str]) -> list[tuple[str, int]]:
    """拉取 JSON,按 country 过滤,返回去重后的 [(ip, port), ...]"""
    headers = {"User-Agent": "curl/8.7.1"}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        async with s.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

    items: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
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
                port_int = int(port)
            except (TypeError, ValueError):
                continue
            key = (ip, port_int)
            if key in seen:
                continue
            seen.add(key)
            items.append(key)
    return items


def write_list(path: str, items: list[tuple[str, int]], source: str, countries: set[str]) -> None:
    """写入 main.py 可读的格式: 一行一个 IP,非 443 端口会带 :port"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Source:    {source}\n")
        f.write(f"# Countries: {','.join(sorted(countries))}\n")
        f.write(f"# Generated: {now}\n")
        f.write(f"# Count:     {len(items)}\n")
        for ip, port in items:
            if port == 443:
                f.write(f"{ip}\n")
            else:
                f.write(f"{ip}:{port}\n")


async def main():
    parser = argparse.ArgumentParser(
        description="拉取并保存指定国家的 ProxyIP 列表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-C", "--countries", default="SG",
                        help="国家代码,逗号分隔(默认 SG)。例: SG / SG,JP,US")
    parser.add_argument("-u", "--url", default=DEFAULT_SOURCE_URL,
                        help=f"数据源 URL(默认 {DEFAULT_SOURCE_URL})")
    parser.add_argument("-o", "--output", default="",
                        help="输出文件路径(默认 <COUNTRY>.txt,多国时拼接)")
    args = parser.parse_args()

    countries = {c.strip().upper() for c in args.countries.split(",") if c.strip()}
    if not countries:
        print("❌ 必须指定至少一个国家代码", file=sys.stderr)
        sys.exit(1)

    output = args.output or f"{'_'.join(sorted(countries))}.txt"

    print(f"⬇  从 {args.url} 拉取节点(country={','.join(sorted(countries))})...")
    try:
        items = await fetch_proxyips(args.url, countries)
    except Exception as e:
        print(f"❌ 拉取失败: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    if not items:
        print(f"❌ 没有匹配国家 {countries} 的节点", file=sys.stderr)
        sys.exit(1)

    write_list(output, items, args.url, countries)
    print(f"✅ 写入 {len(items)} 个节点到 {output}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹  已中断")
        sys.exit(130)
