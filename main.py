#!/usr/bin/env python3
"""
ProxyIP 可用性测试脚本

功能:
- 从文本文件读取 ProxyIP 列表(每行一个,支持 IP/域名,支持 host:port,支持 # 注释)
- 并发测试每个 ProxyIP 对多个目标网站的可用性
- 输出实时进度 + CSV 报告
- 自动检测出口 IP 地理位置和 ISP

用法:
    python test_proxyip.py proxyips.txt
    python test_proxyip.py proxyips.txt -o report.csv -c 30 -t 8

proxyips.txt 格式示例:
    # 这是注释,会被忽略
    1.2.3.4
    1.2.3.4:443
    proxyip.example.com
    proxyip.example.com:2053
"""

import asyncio
import aiohttp
import argparse
import csv
import json
import socket
import sys
from aiohttp.resolver import AbstractResolver
from dataclasses import dataclass, field
from typing import Optional

# ============== 测试目标配置 ==============
# (主机名, URL, 期望返回的 JSON 字段, 类别)
# 类别用于区分 CF 背后 vs 非 CF,便于诊断 ProxyIP 类型
TEST_TARGETS = [
    # 非 CF 网站 - 测试 ProxyIP 是否能转发到任意目标
    # ("ip-api.com",  "http://ip-api.com/json",  "status", "non-cf"),
    # 用 http 而不是 https,ip-api 免费版只支持 http
    # 如果你需要 https,可以换成 ipinfo.io:
    ("ipinfo.io",   "https://ipinfo.io/json",   "ip",     "non-cf"),
    
    # CF 背后的网站 - 测试 ProxyIP 是否至少能访问 CF 网络
    ("discord.com", "https://discord.com/api/v9/gateway", "url", "cf"),
]


@dataclass
class TestResult:
    proxyip: str
    port: int
    target_host: str
    target_category: str  # 'cf' or 'non-cf'
    ok: bool
    exit_ip: str = ""
    exit_country: str = ""
    exit_region: str = ""
    exit_isp: str = ""
    error: str = ""
    elapsed_ms: int = 0


class FixedResolver(AbstractResolver):
    """强制 DNS 解析到指定 IP,模拟 curl --resolve 的效果"""
    def __init__(self, ip: str):
        self.ip = ip

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [{
            "hostname": host,
            "host": self.ip,
            "port": port,
            "family": family,
            "proto": 0,
            "flags": 0,
        }]

    async def close(self):
        pass


async def resolve_to_ip(host: str) -> Optional[str]:
    """如果是域名,先解析成 IP(aiohttp 的 resolver 需要 IP)"""
    try:
        # 已经是 IP 就直接返回
        socket.inet_aton(host)
        return host
    except OSError:
        pass
    try:
        # 异步 DNS
        loop = asyncio.get_event_loop()
        info = await loop.getaddrinfo(host, None, family=socket.AF_INET)
        return info[0][4][0] if info else None
    except Exception:
        return None


async def test_target(proxyip_resolved: str, port: int, target_host: str,
                       url: str, expect_key: str, timeout: float) -> tuple:
    """
    测试单个 ProxyIP 对单个目标的可达性
    返回 (ok, exit_info_dict, error_msg, elapsed_ms)
    """
    import time
    start = time.monotonic()
    
    connector = aiohttp.TCPConnector(
        resolver=FixedResolver(proxyip_resolved),
        ssl=False if url.startswith("http://") else True,
        force_close=True,
        limit=1,
    )
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=timeout)
    
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as s:
            # 用目标域名访问,connector 会把它强制路由到 proxyip_resolved
            # 注意:这里如果端口不是 443/80,要手动拼端口
            if port not in (80, 443):
                # 自定义端口情况,把 URL 改写
                from urllib.parse import urlparse, urlunparse
                parsed = urlparse(url)
                new_netloc = f"{parsed.hostname}:{port}"
                url = urlunparse(parsed._replace(netloc=new_netloc))
            
            async with s.get(url, allow_redirects=False) as resp:
                elapsed = int((time.monotonic() - start) * 1000)
                text = await resp.text()
                
                if resp.status != 200:
                    return False, {}, f"http_{resp.status}", elapsed
                
                # 校验返回内容是不是真的目标网站(防 CF 错误页伪装成 200)
                try:
                    data = json.loads(text)
                    if expect_key not in data:
                        return False, {}, f"unexpected_response", elapsed
                    return True, data, "", elapsed
                except json.JSONDecodeError:
                    # 返回不是 JSON,大概率是 CF 错误页或者拦截页
                    snippet = text[:60].replace("\n", " ")
                    return False, {}, f"non-json: {snippet}", elapsed
                    
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start) * 1000)
        return False, {}, "timeout", elapsed
    except aiohttp.ClientConnectorCertificateError as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return False, {}, "cert_error", elapsed
    except aiohttp.ClientConnectorError as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return False, {}, f"connect_failed", elapsed
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return False, {}, f"{type(e).__name__}: {str(e)[:40]}", elapsed


async def test_proxyip(proxyip: str, port: int, timeout: float,
                        sem: asyncio.Semaphore) -> list:
    """测试一个 ProxyIP 对所有目标网站"""
    async with sem:
        results = []
        
        # 先解析 ProxyIP(如果是域名)
        resolved = await resolve_to_ip(proxyip)
        if not resolved:
            for host, url, key, cat in TEST_TARGETS:
                results.append(TestResult(
                    proxyip=proxyip, port=port, target_host=host,
                    target_category=cat, ok=False, error="dns_resolve_failed"
                ))
            return results
        
        for host, url, key, cat in TEST_TARGETS:
            ok, data, err, elapsed = await test_target(
                resolved, port, host, url, key, timeout
            )
            r = TestResult(
                proxyip=proxyip, port=port, target_host=host,
                target_category=cat, ok=ok, error=err, elapsed_ms=elapsed,
            )
            if ok and host == "ip-api.com":
                r.exit_ip = data.get("query", "")
                r.exit_country = data.get("country", "")
                r.exit_region = data.get("regionName", "")
                r.exit_isp = data.get("isp", "")
            elif ok and host == "ipinfo.io":
                r.exit_ip = data.get("ip", "")
                r.exit_country = data.get("country", "")
                r.exit_region = data.get("region", "")
                r.exit_isp = data.get("org", "")
            results.append(r)
        
        return results


def parse_proxyip_file(path: str) -> list:
    """从文本文件读取 ProxyIP 列表,返回 [(host, port), ...]"""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue
            # 支持 host 和 host:port 格式
            if ":" in line and not line.count(":") > 1:  # 简单排除 IPv6
                host, port_str = line.rsplit(":", 1)
                try:
                    port = int(port_str)
                except ValueError:
                    host, port = line, 443
            else:
                host, port = line, 443
            items.append((host.strip(), port))
    return items


def print_result(results: list):
    """实时打印一个 ProxyIP 的测试结果(单行紧凑格式)"""
    if not results:
        return
    
    proxyip = f"{results[0].proxyip}:{results[0].port}"
    
    # 汇总两类目标的成功情况
    cf_ok = any(r.ok for r in results if r.target_category == "cf")
    noncf_ok = any(r.ok for r in results if r.target_category == "non-cf")
    
    # 出口信息(从 non-cf 测试里拿)
    exit_info = ""
    for r in results:
        if r.ok and r.exit_ip:
            exit_info = f"{r.exit_country}/{r.exit_isp} ({r.exit_ip})"
            break
    
    # 分类标签
    if noncf_ok:
        tag = "🟢 万能"
    elif cf_ok:
        tag = "🟡 仅CF"
    else:
        tag = "🔴 不可用"
    
    # 失败原因(取第一个失败的)
    err = ""
    if not noncf_ok:
        for r in results:
            if not r.ok and r.target_category == "non-cf":
                err = f" [{r.error}]"
                break
    
    # 延迟(取最快的成功)
    latencies = [r.elapsed_ms for r in results if r.ok]
    lat = f"{min(latencies)}ms" if latencies else "-"
    
    print(f"{tag}  {proxyip:35s}  {lat:>7s}  {exit_info}{err}")


async def run_tests(proxyips: list, output: str, concurrency: int, timeout: float, label: str = ""):
    """跑一组 ProxyIP 测试并写报告。proxyips 是 [(host, port), ...]"""
    suffix = f"{label}" if label else ""
    print(f"📋 加载 {len(proxyips)} 个 ProxyIP{suffix},开始测试...")
    print(f"   目标: {[t[0] for t in TEST_TARGETS]}")
    print(f"   并发: {concurrency}  超时: {timeout}s")
    print("-" * 90)

    sem = asyncio.Semaphore(concurrency)
    all_results = []

    tasks = [test_proxyip(host, port, timeout, sem) for host, port in proxyips]
    for coro in asyncio.as_completed(tasks):
        results = await coro
        print_result(results)
        all_results.extend(results)

    print("-" * 90)
    by_proxyip = {}
    for r in all_results:
        key = f"{r.proxyip}:{r.port}"
        by_proxyip.setdefault(key, []).append(r)

    universal = sum(1 for rs in by_proxyip.values() if any(r.ok and r.target_category == "non-cf" for r in rs))
    cf_only = sum(1 for rs in by_proxyip.values() if
                  any(r.ok and r.target_category == "cf" for r in rs) and
                  not any(r.ok and r.target_category == "non-cf" for r in rs))
    dead = len(by_proxyip) - universal - cf_only

    print(f"📊 汇总: 共 {len(by_proxyip)} 个")
    print(f"   🟢 万能(可访问任意网站): {universal}")
    print(f"   🟡 仅 CF(只能访问 CF 背后网站): {cf_only}")
    print(f"   🔴 不可用: {dead}")

    countries = {}
    for rs in by_proxyip.values():
        for r in rs:
            if r.ok and r.exit_country:
                countries[r.exit_country] = countries.get(r.exit_country, 0) + 1
                break
    if countries:
        print(f"\n🌍 万能 ProxyIP 出口分布:")
        for country, count in sorted(countries.items(), key=lambda x: -x[1]):
            print(f"   {country}: {count}")

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "proxyip", "port", "target_host", "target_category",
            "ok", "exit_ip", "exit_country", "exit_region", "exit_isp",
            "elapsed_ms", "error",
        ])
        for r in all_results:
            writer.writerow([
                r.proxyip, r.port, r.target_host, r.target_category,
                "Y" if r.ok else "N", r.exit_ip, r.exit_country, r.exit_region,
                r.exit_isp, r.elapsed_ms, r.error,
            ])

    print(f"\n💾 详细报告已保存到: {output}")
    return all_results


async def main():
    parser = argparse.ArgumentParser(
        description="批量测试 ProxyIP 可用性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", help="ProxyIP 列表文件路径(每行一个,支持 host 或 host:port)")
    parser.add_argument("-o", "--output", default="proxyip_report.csv", help="输出 CSV 文件路径(默认 proxyip_report.csv)")
    parser.add_argument("-c", "--concurrency", type=int, default=20, help="并发数(默认 20)")
    parser.add_argument("-t", "--timeout", type=float, default=6.0, help="单个请求超时秒数(默认 6)")
    args = parser.parse_args()

    try:
        proxyips = parse_proxyip_file(args.input)
    except FileNotFoundError:
        print(f"❌ 找不到文件: {args.input}", file=sys.stderr)
        sys.exit(1)

    if not proxyips:
        print("❌ 列表为空", file=sys.stderr)
        sys.exit(1)

    await run_tests(proxyips, args.output, args.concurrency, args.timeout)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹  已中断")
        sys.exit(130)