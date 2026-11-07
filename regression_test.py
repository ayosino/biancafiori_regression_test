#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Biancafiori WordPress Upgrade Regression Test
=============================================
アップグレード前の http://biancafiori.com (およびBASIC認証サイト) の状態を baseline.json に記録し、
アップグレード後に再巡回して「アップグレード前後で変化・退行がないか」を検証するスクリプト。

使い方:
  1. メンテ前 (ベースライン記録):
     python3 regression_test.py record

  2. メンテ後 (ベースラインとの差分検証):
     python3 regression_test.py verify

  3. SSL導入後 (https:// に切り替えてベースラインと比較検証):
     python3 regression_test.py verify --https
"""

import argparse
import base64
import concurrent.futures
import datetime
import difflib
import getpass
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

BASE_HOST = "biancafiori.com"
MEMBERS_HOST = "biancafiori.ciao.jp"
HTTPS_MEMBERS_HOST = "members.biancafiori.com"
BASE_URL = f"http://{BASE_HOST}"
HTTPS_BASE_URL = f"https://{BASE_HOST}"
MEMBERS_URL = f"http://{MEMBERS_HOST}/members/"
HTTPS_MEMBERS_URL = f"https://{HTTPS_MEMBERS_HOST}/"
DEFAULT_BASELINE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")
DEFAULT_REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

# http://biancafiori.com 配下の各WordPressサイト
SUB_WP_PATHS = [
    "/",
    "/30_years_anniversary/",
    "/31_program_additional/",
    "/32_program_additional/",
    "/33_program_additional/",
]

# トップからのリンクだけでは到達しない既知の公開固定ページ
KNOWN_SEED_PATHS = [
    "/25_program/",
    "/cd_form/",
    "/cd_thanks/",
    "/video_17/",
    "/video_19/",
    "/video_20/",
    "/感染対策/",
    "/曲目詳細解説/",
]

ASSET_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".css", ".js", ".pdf", ".mp3", ".mp4", ".wav", ".m4a", ".ogg",
    ".zip", ".xml", ".json", ".woff", ".woff2", ".ttf", ".eot", ".htm", ".html"
)

ERROR_PATTERNS = [
    ("PHP Error/Warning/Deprecated", re.compile(r"(Fatal error|Parse error|Warning|Notice|Deprecated):\s+.+?\s+in\s+/.+?\s+on line\s+\d+", re.IGNORECASE)),
    ("DB Connection Error", re.compile(r"(データベース接続確立エラー|Error establishing a database connection)", re.IGNORECASE)),
    ("WP Critical Error", re.compile(r"(このサイトで重大なエラーが発生しました|There has been a critical error on this website)", re.IGNORECASE)),
]


class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_basic_auth_header(user_pass: str) -> str:
    token = base64.b64encode(user_pass.encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def normalize_url(url: str, base_url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    joined = urllib.parse.urljoin(base_url, url)
    parsed = urllib.parse.urlsplit(joined)
    if parsed.scheme not in ("http", "https"):
        return None
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/:@!$&'()*+,;=-._~")
    if not path:
        path = "/"
    query = urllib.parse.quote(urllib.parse.unquote(parsed.query), safe="/?:@!$&'()*+,;=-._~=")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


MEMBERS_PREFIX_RE = re.compile(
    r"^https?://(?:biancafiori\.ciao(?:\.co)?\.jp/members|members\.biancafiori\.com)(?=/|\?|#|$)",
    re.IGNORECASE,
)
BASE_PREFIX_RE = re.compile(
    r"^https?://biancafiori\.com(?=/|\?|#|$)",
    re.IGNORECASE,
)


def is_members_scope_url(url: str | None, target_members_url: str | None = None) -> bool:
    """URLが団員専用サイト (biancafiori.ciao.jp/members または members.biancafiori.com 等) に属するか判定する"""
    if not url:
        return False
    if MEMBERS_PREFIX_RE.match(url):
        return True
    if target_members_url:
        tm_root = target_members_url.rstrip("/")
        if url.lower().startswith(tm_root.lower()):
            rest = url[len(tm_root):]
            if not rest or rest.startswith(("/", "?", "#")):
                return True
    return False


def rewrite_in_scope_url(
    url: str | None,
    target_base_url: str = BASE_URL,
    target_members_url: str = MEMBERS_URL,
) -> str | None:
    """
    テスト対象サイトのURLを指定されたターゲットURL体系に変換する。
    - http://biancafiori.com/... <-> https://biancafiori.com/...
    - http://biancafiori.ciao.jp/members/... <-> https://members.biancafiori.com/... (等)
    """
    if not url:
        return url

    tm_root = target_members_url.rstrip("/")
    tm_has_path = bool(urllib.parse.urlsplit(tm_root).path)

    m_mem = MEMBERS_PREFIX_RE.match(url)
    if not m_mem and tm_root:
        custom_mem_re = re.compile(rf"^https?://{re.escape(urllib.parse.urlsplit(tm_root).netloc)}{re.escape(urllib.parse.urlsplit(tm_root).path)}(?=/|\?|#|$)", re.IGNORECASE)
        m_mem = custom_mem_re.match(url)

    if m_mem:
        rest = url[m_mem.end():]
        if not rest:
            if not tm_has_path:
                rest = "/"
        elif rest.startswith(("?", "#")):
            rest = "/" + rest
        return tm_root + rest

    tb_root = target_base_url.rstrip("/")
    m_base = BASE_PREFIX_RE.match(url)
    if m_base:
        rest = url[m_base.end():]
        if rest.startswith(("?", "#")):
            rest = "/" + rest
        return tb_root + rest

    return url


def normalize_in_scope_text_scheme(
    text: str,
    target_base_url: str = BASE_URL,
    target_members_url: str = MEMBERS_URL,
) -> str:
    """本文テキスト中に記載された対象サイトURL (旧members URLやhttp/httpsの違い) を正規化する"""
    if not text:
        return text
    tm_root = target_members_url.rstrip("/")
    tb_root = target_base_url.rstrip("/")
    text = re.sub(
        r"https?://(?:biancafiori\.ciao(?:\.co)?\.jp/members|members\.biancafiori\.com)/?",
        tm_root + "/",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"https?://biancafiori\.com",
        tb_root,
        text,
        flags=re.IGNORECASE,
    )
    return text


def extract_main_text(soup: BeautifulSoup) -> str:
    """ページのメインコンテンツ領域のテキストを抽出・正規化する（動的ウィジェット等のノイズを除外）"""
    clone = BeautifulSoup(str(soup), "html.parser")
    for tag in clone.find_all(["script", "style", "noscript"]):
        tag.decompose()

    post_contents = clone.find_all(class_="post-content")
    if post_contents:
        texts = [re.sub(r"\s+", " ", pc.get_text(separator=" ", strip=True)).strip() for pc in post_contents]
        joined = " ".join(t for t in texts if t)
        if joined:
            return joined

    main_el = (
        clone.find(class_="posts")
        or clone.find(class_="content")
        or clone.find("article")
        or clone.find("main")
        or clone.find(id="content")
        or clone.find("body")
    )
    if not main_el:
        return ""
    text = main_el.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


class SiteCrawler:
    def __init__(
        self,
        auth_30: str | None = None,
        auth_members: str | None = None,
        skip_members: bool = False,
        use_https: bool = False,
        base_url: str | None = None,
        members_url: str | None = None,
        workers: int = 8,
        timeout: int = 20,
    ):
        default_base = HTTPS_BASE_URL if use_https else BASE_URL
        default_members = HTTPS_MEMBERS_URL if use_https else MEMBERS_URL
        self.base_url = (base_url or default_base).rstrip("/")
        raw_members = members_url or default_members
        self.members_url = raw_members if raw_members.endswith("/") else raw_members + "/"
        self.use_https = (
            use_https
            or self.base_url.lower().startswith("https://")
            or self.members_url.lower().startswith("https://")
        )
        self.scheme = "https" if self.use_https else "http"
        self.base_host = urllib.parse.urlsplit(self.base_url).netloc.lower()
        self.members_host = urllib.parse.urlsplit(self.members_url).netloc.lower()
        self.auth_30 = auth_30
        self.auth_members = auth_members
        self.skip_members = skip_members
        self.workers = workers
        self.timeout = timeout

    def get_auth_header_for_url(self, url: str) -> str | None:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.netloc.lower()
        path = urllib.parse.unquote(parsed.path)
        if host == self.base_host and path.startswith("/30_years_anniversary"):
            if self.auth_30:
                return make_basic_auth_header(self.auth_30)
        if is_members_scope_url(url, self.members_url):
            if self.auth_members:
                return make_basic_auth_header(self.auth_members)
        return None

    def is_in_scope(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.netloc.lower()
        if host == self.base_host:
            return True
        if not self.skip_members and is_members_scope_url(url, self.members_url):
            return True
        return False

    def request_url(
        self,
        url: str,
        method: str = "GET",
        use_auth: bool = True,
        follow_redirects: bool = True,
    ) -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BiancafioriRegressionTester/1.0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if use_auth:
            auth_h = self.get_auth_header_for_url(url)
            if auth_h:
                headers["Authorization"] = auth_h

        req = urllib.request.Request(url, method=method, headers=headers)
        opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(NoRedirectHandler())

        try:
            with opener.open(req, timeout=self.timeout) as resp:
                body = resp.read() if method == "GET" else b""
                return {
                    "status": resp.status,
                    "final_url": resp.geturl(),
                    "headers": dict(resp.headers.items()),
                    "body": body,
                    "error": None,
                }
        except urllib.error.HTTPError as e:
            return {
                "status": e.code,
                "final_url": e.geturl() if hasattr(e, "geturl") else url,
                "headers": dict(e.headers.items()) if e.headers else {},
                "body": b"",
                "error": f"HTTP {e.code}: {e.reason}",
            }
        except Exception as e:
            return {
                "status": "ERROR",
                "final_url": url,
                "headers": {},
                "body": b"",
                "error": str(e),
            }

    def check_basic_auth_endpoints(self) -> list[dict]:
        """BASIC認証サイトの保護状態（認証なしで401になるか）と認証通過（200になるか）を検証"""
        results = []
        targets = [
            {
                "id": "30_years_anniversary",
                "name": "30周年記念サイト (/30_years_anniversary/)",
                "url": f"{self.base_url}/30_years_anniversary/",
                "creds": self.auth_30,
            }
        ]
        if not self.skip_members:
            parsed_m = urllib.parse.urlsplit(self.members_url)
            m_label = f"{parsed_m.netloc}{parsed_m.path}"
            targets.append(
                {
                    "id": "members",
                    "name": f"団員専用サイト ({m_label})",
                    "url": self.members_url,
                    "creds": self.auth_members,
                }
            )

        for t in targets:
            url = t["url"]
            unauth_resp = self.request_url(url, method="GET", use_auth=False, follow_redirects=False)
            unauth_status = unauth_resp["status"]
            protected_ok = (unauth_status == 401)
            auth_status = None
            auth_ok = None
            note = ""

            if not protected_ok:
                note = f"認証なしアクセスで 401 ではなく {unauth_status} が返されました（BASIC認証設定が外れている可能性）"

            if t["creds"]:
                auth_resp = self.request_url(url, method="GET", use_auth=True, follow_redirects=True)
                auth_status = auth_resp["status"]
                auth_ok = (auth_status == 200)
                if not auth_ok:
                    note = (note + " / " if note else "") + f"認証ありアクセス失敗 (HTTP {auth_status})"
            else:
                note = (note + " / " if note else "") + "認証情報未入力のため内部巡回はスキップ（401保護チェックのみ）"

            results.append({
                "id": t["id"],
                "name": t["name"],
                "url": url,
                "unauth_status": unauth_status,
                "protected_ok": protected_ok,
                "credentials_provided": bool(t["creds"]),
                "auth_status": auth_status,
                "auth_ok": auth_ok,
                "note": note,
            })
        return results

    def discover_initial_urls(self, baseline_urls: list[str] | None = None) -> list[str]:
        seeds = set()
        for sub in SUB_WP_PATHS:
            if sub.startswith("/30_years_anniversary") and not self.auth_30:
                continue
            u = normalize_url(sub, self.base_url + "/")
            if u:
                seeds.add(u)

        for sp in KNOWN_SEED_PATHS:
            u = normalize_url(sp, self.base_url + "/")
            if u:
                seeds.add(u)

        if not self.skip_members and self.auth_members:
            seeds.add(self.members_url)

        if baseline_urls:
            for bu in baseline_urls:
                parsed = urllib.parse.urlsplit(bu)
                path = urllib.parse.unquote(parsed.path)
                if path.startswith("/30_years_anniversary") and not self.auth_30:
                    continue
                if is_members_scope_url(bu, self.members_url) and (self.skip_members or not self.auth_members):
                    continue
                rewritten_bu = rewrite_in_scope_url(bu, self.base_url, self.members_url)
                if rewritten_bu:
                    seeds.add(rewritten_bu)

        wp_roots = []
        for sub in SUB_WP_PATHS:
            if sub.startswith("/30_years_anniversary") and not self.auth_30:
                continue
            wp_roots.append(self.base_url + sub.rstrip("/"))
        if not self.skip_members and self.auth_members:
            wp_roots.append(self.members_url.rstrip("/"))

        for wpr in wp_roots:
            for ep in ("pages", "posts"):
                api_url = f"{wpr}/wp-json/wp/v2/{ep}?per_page=100"
                resp = self.request_url(api_url, method="GET", use_auth=True)
                if resp["status"] == 200 and resp["body"]:
                    try:
                        items = json.loads(resp["body"].decode("utf-8", errors="replace"))
                        if isinstance(items, list):
                            for item in items:
                                link = item.get("link") if isinstance(item, dict) else None
                                nu = normalize_url(link, wpr + "/")
                                if nu and self.is_in_scope(nu):
                                    seeds.add(rewrite_in_scope_url(nu, self.base_url, self.members_url))
                    except Exception:
                        pass

            if wpr == self.base_url:
                for sm_path in (
                    "/wp-sitemap-posts-post-1.xml",
                    "/wp-sitemap-posts-page-1.xml",
                    "/wp-sitemap-taxonomies-category-1.xml",
                ):
                    sm_url = self.base_url + sm_path
                    resp = self.request_url(sm_url, method="GET", use_auth=True)
                    if resp["status"] == 200 and resp["body"]:
                        try:
                            root = ET.fromstring(resp["body"])
                            for elem in root.iter():
                                if elem.tag.endswith("loc") and elem.text:
                                    nu = normalize_url(elem.text, sm_url)
                                    if nu and self.is_in_scope(nu):
                                        seeds.add(rewrite_in_scope_url(nu, self.base_url, self.members_url))
                        except Exception:
                            pass

        return sorted(seeds)

    def run(self, baseline_urls: list[str] | None = None) -> dict:
        print(f"{Colors.CYAN}[1/3] BASIC認証サイトの保護・疎通確認中...{Colors.RESET}", flush=True)
        basic_auth_checks = self.check_basic_auth_endpoints()
        for b in basic_auth_checks:
            ok = b["protected_ok"] and (b["auth_ok"] in (True, None))
            icon = f"{Colors.GREEN}OK{Colors.RESET}" if ok else f"{Colors.RED}NG{Colors.RESET}"
            print(f"  - {b['name']}: 未認証={b['unauth_status']}, 認証後={b['auth_status']} [{icon}] {b['note']}", flush=True)

        print(f"{Colors.CYAN}[2/3] 巡回対象URLの収集中...{Colors.RESET}", flush=True)
        initial_urls = self.discover_initial_urls(baseline_urls)
        print(f"  シードURL数: {len(initial_urls)} 件", flush=True)

        visited_pages = set()
        queue = list(initial_urls)
        queued_set = set(initial_urls)

        pages_data = {}
        assets_to_check = {}  # url -> set of source decoded_urls

        print(f"{Colors.CYAN}[3/3] 全ページ巡回およびコンテンツ解析中 (並列数: {self.workers})...{Colors.RESET}", flush=True)

        def process_page(url: str):
            resp = self.request_url(url, method="GET", use_auth=True, follow_redirects=True)
            decoded_url = urllib.parse.unquote(url)
            final_url = resp["final_url"]
            final_decoded = urllib.parse.unquote(final_url) if final_url else decoded_url
            content_type = resp["headers"].get("Content-Type", resp["headers"].get("content-type", ""))

            page_info = {
                "url": url,
                "decoded_url": decoded_url,
                "status": resp["status"],
                "final_url": final_url,
                "final_decoded_url": final_decoded,
                "content_type": content_type,
                "title": None,
                "headings": [],
                "content_length": 0,
                "content_hash": None,
                "content_text": "",
                "php_errors": [],
                "test_domain_refs": [],
                "mixed_content_refs": [],
                "links": [],
            }

            discovered_pages = []
            discovered_assets = []

            if resp["status"] != 200:
                return url, page_info, discovered_pages, discovered_assets

            if "text/html" not in content_type.lower() and "application/xhtml+xml" not in content_type.lower():
                return url, page_info, discovered_pages, discovered_assets

            html_text = resp["body"].decode("utf-8", errors="replace")

            for label, regex in ERROR_PATTERNS:
                matches = regex.findall(html_text)
                if matches:
                    sample = matches[0] if isinstance(matches[0], str) else str(matches[0])
                    page_info["php_errors"].append(f"{label}: {sample[:120]}")

            soup = BeautifulSoup(html_text, "html.parser")
            if soup.title and soup.title.string:
                page_info["title"] = re.sub(r"\s+", " ", soup.title.string).strip()

            headings = []
            for h in soup.find_all(["h1", "h2"]):
                ht = re.sub(r"\s+", " ", h.get_text(strip=True)).strip()
                if ht:
                    headings.append(f"{h.name}:{ht}")
            page_info["headings"] = headings

            main_text = extract_main_text(soup)
            page_info["content_length"] = len(main_text)
            page_info["content_hash"] = hashlib.sha256(main_text.encode("utf-8")).hexdigest() if main_text else None
            page_info["content_text"] = main_text

            tag_attrs = [
                ("a", "href", "link"),
                ("area", "href", "link"),
                ("img", "src", "asset"),
                ("img", "srcset", "srcset"),
                ("script", "src", "asset"),
                ("link", "href", "link_tag"),
                ("iframe", "src", "embed"),
                ("embed", "src", "embed"),
                ("object", "data", "embed"),
                ("source", "src", "asset"),
                ("source", "srcset", "srcset"),
                ("video", "src", "asset"),
                ("video", "poster", "asset"),
                ("audio", "src", "asset"),
                ("form", "action", "form"),
            ]

            links_set = set()
            mixed_content_set = set()
            for tag_name, attr_name, kind in tag_attrs:
                for tag in soup.find_all(tag_name):
                    val = tag.get(attr_name)
                    if not val:
                        continue
                    raw_urls = []
                    if kind == "srcset":
                        for part in val.split(","):
                            part = part.strip()
                            if part:
                                raw_urls.append(part.split()[0])
                    else:
                        raw_urls.append(val)

                    rel = tag.get("rel", [])
                    rel_str = " ".join(rel).lower() if isinstance(rel, list) else str(rel).lower()
                    is_subresource = (
                        kind in ("asset", "srcset", "embed")
                        or (kind == "link_tag" and ("stylesheet" in rel_str or "icon" in rel_str or "preload" in rel_str))
                    )

                    for raw_u in raw_urls:
                        norm_u = normalize_url(raw_u, url)
                        if not norm_u:
                            continue
                        parsed_u = urllib.parse.urlsplit(norm_u)
                        host = parsed_u.netloc.lower()
                        path_lower = urllib.parse.unquote(parsed_u.path).lower()

                        if host == "test.biancafiori.com":
                            page_info["test_domain_refs"].append(urllib.parse.unquote(norm_u))

                        if self.use_https and is_subresource and parsed_u.scheme.lower() == "http":
                            mixed_content_set.add(urllib.parse.unquote(norm_u))

                        if self.is_in_scope(norm_u):
                            if path_lower.startswith("/30_years_anniversary") and not self.auth_30:
                                continue
                            if is_members_scope_url(norm_u, self.members_url) and not self.auth_members:
                                continue

                            is_asset_ext = path_lower.endswith(ASSET_EXTENSIONS)
                            is_wp_system = (
                                "/wp-json/" in path_lower
                                or "/wp-admin/" in path_lower
                                or "wp-login.php" in path_lower
                                or "xmlrpc.php" in path_lower
                                or path_lower.endswith("/feed/")
                                or path_lower.endswith("/feed")
                            )

                            target_u = rewrite_in_scope_url(norm_u, self.base_url, self.members_url)
                            if kind == "link" and not is_asset_ext and not is_wp_system:
                                links_set.add(urllib.parse.unquote(target_u))
                                discovered_pages.append(target_u)
                                discovered_assets.append((target_u, decoded_url))
                            else:
                                if kind in ("asset", "srcset", "embed") or "stylesheet" in rel_str or "icon" in rel_str or is_asset_ext:
                                    if not is_wp_system:
                                        discovered_assets.append((target_u, decoded_url))

            page_info["mixed_content_refs"] = sorted(mixed_content_set)
            page_info["links"] = sorted(links_set)
            return url, page_info, discovered_pages, discovered_assets

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            while queue:
                batch = []
                while queue and len(batch) < self.workers * 2:
                    u = queue.pop(0)
                    if u not in visited_pages:
                        visited_pages.add(u)
                        batch.append(u)
                if not batch:
                    break

                futures = {executor.submit(process_page, u): u for u in batch}
                for fut in concurrent.futures.as_completed(futures):
                    u, p_info, disc_pages, disc_assets = fut.result()
                    pages_data[u] = p_info
                    for au, src_url in disc_assets:
                        assets_to_check.setdefault(au, set()).add(src_url)
                    for pu in disc_pages:
                        if pu not in visited_pages and pu not in queued_set:
                            queued_set.add(pu)
                            queue.append(pu)
                    st_str = str(p_info["status"])
                    color = (
                        Colors.GREEN
                        if p_info["status"] == 200 and not p_info["php_errors"] and not p_info.get("mixed_content_refs")
                        else Colors.YELLOW
                    )
                    print(f"  [{len(pages_data):3d}] {color}{st_str}{Colors.RESET} {p_info['decoded_url']}", flush=True)

        visited_url_status = {u: info["status"] for u, info in pages_data.items()}
        remaining_assets = [u for u in assets_to_check.keys() if u not in visited_url_status]
        print(f"{Colors.CYAN}内部アセット・リンク ({len(assets_to_check)} 件中 未確認 {len(remaining_assets)} 件) のステータス確認中...{Colors.RESET}", flush=True)

        assets_data = {}
        for u, sources in assets_to_check.items():
            if u in visited_url_status:
                assets_data[u] = {
                    "url": u,
                    "decoded_url": urllib.parse.unquote(u),
                    "status": visited_url_status[u],
                    "sources": sorted(list(sources))[:10],
                }

        def check_single_asset(u: str):
            resp = self.request_url(u, method="HEAD", use_auth=True, follow_redirects=True)
            st = resp["status"]
            if st in (405, 400, "ERROR"):
                resp = self.request_url(u, method="GET", use_auth=True, follow_redirects=True)
                st = resp["status"]
            return u, {
                "url": u,
                "decoded_url": urllib.parse.unquote(u),
                "status": st,
                "sources": sorted(list(assets_to_check[u]))[:10],
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(check_single_asset, u) for u in remaining_assets]
            for fut in concurrent.futures.as_completed(futures):
                u, a_info = fut.result()
                assets_data[u] = a_info

        return {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(),
            "base_url": self.base_url,
            "members_url": None if self.skip_members else self.members_url,
            "basic_auth_checks": basic_auth_checks,
            "pages": pages_data,
            "assets": assets_data,
        }


def compare_before_after(baseline: dict, current: dict) -> dict:
    """ベースライン (record) とアップグレード後 (verify) を比較し、変化があった箇所をすべて抽出する"""
    curr_base_url = current.get("base_url") or BASE_URL
    curr_members_url = current.get("members_url") or (
        HTTPS_MEMBERS_URL if curr_base_url.lower().startswith("https://") else MEMBERS_URL
    )

    raw_base_pages = baseline.get("pages", {})
    raw_curr_pages = current.get("pages", {})
    raw_base_assets = baseline.get("assets", {})
    raw_curr_assets = current.get("assets", {})

    base_pages = {}
    for u, p in raw_base_pages.items():
        ru = rewrite_in_scope_url(u, curr_base_url, curr_members_url)
        if ru not in base_pages or u.endswith("/"):
            base_pages[ru] = p

    curr_pages = {}
    for u, p in raw_curr_pages.items():
        ru = rewrite_in_scope_url(u, curr_base_url, curr_members_url)
        if ru not in curr_pages or u.endswith("/"):
            curr_pages[ru] = p

    base_assets = {}
    for u, a in raw_base_assets.items():
        ru = rewrite_in_scope_url(u, curr_base_url, curr_members_url)
        if ru not in base_assets or u.endswith("/"):
            base_assets[ru] = a

    curr_assets = {}
    for u, a in raw_curr_assets.items():
        ru = rewrite_in_scope_url(u, curr_base_url, curr_members_url)
        if ru not in curr_assets or u.endswith("/"):
            curr_assets[ru] = a

    base_auth = {b["id"]: b for b in baseline.get("basic_auth_checks", []) if "id" in b}

    diffs = []

    # 1. BASIC認証の挙動変化・エラーチェック
    for b in current.get("basic_auth_checks", []):
        b_old = base_auth.get(b.get("id"))
        if not b["protected_ok"]:
            diffs.append({
                "category": "BASIC認証保護異常",
                "target": b["name"],
                "detail": b["note"],
            })
        elif b["credentials_provided"] and not b["auth_ok"]:
            diffs.append({
                "category": "BASIC認証ログイン失敗",
                "target": b["name"],
                "detail": b["note"],
            })
        elif b_old and b_old.get("unauth_status") != b.get("unauth_status"):
            diffs.append({
                "category": "BASIC認証ステータス変化",
                "target": b["name"],
                "detail": f"未認証ステータス: {b_old.get('unauth_status')} -> {b.get('unauth_status')}",
            })

    # 2. 各ページのステータス・リダイレクト・タイトル・見出し・本文・エラーの比較
    all_page_urls = sorted(set(base_pages.keys()) | set(curr_pages.keys()))
    for u in all_page_urls:
        bp = base_pages.get(u)
        cp = curr_pages.get(u)

        # BASIC認証未入力時にスキップされたURLは除外
        parsed = urllib.parse.urlsplit(u)
        path = urllib.parse.unquote(parsed.path)
        if path.startswith("/30_years_anniversary") and not any(
            b["credentials_provided"] for b in current.get("basic_auth_checks", []) if b.get("id") == "30_years_anniversary"
        ):
            continue
        if is_members_scope_url(u, curr_members_url) and not any(
            b["credentials_provided"] for b in current.get("basic_auth_checks", []) if b.get("id") == "members"
        ):
            continue

        if bp and not cp:
            diffs.append({
                "category": "ページ欠落",
                "target": rewrite_in_scope_url(bp["decoded_url"], curr_base_url, curr_members_url),
                "detail": f"ベースライン時に存在したページ (HTTP {bp['status']}) が巡回されませんでした",
            })
            continue

        if cp and not bp:
            # ベースラインになかった新規ページでエラーがある場合
            if cp["status"] != 200:
                diffs.append({
                    "category": "新規リンク先のHTTPエラー",
                    "target": cp["decoded_url"],
                    "detail": f"Status: {cp['status']}",
                })
            if cp.get("mixed_content_refs"):
                diffs.append({
                    "category": "Mixed Content (httpアセット残存)",
                    "target": cp["decoded_url"],
                    "detail": f"ページ内のアセットに http:// が残っています ({len(cp['mixed_content_refs'])}件): {cp['mixed_content_refs'][:3]}",
                })
            continue

        # 両方に存在する場合の比較
        decoded = cp["decoded_url"]

        # (a) HTTPステータスコードの変化
        if bp["status"] != cp["status"]:
            diffs.append({
                "category": "HTTPステータス変化",
                "target": decoded,
                "detail": f"Before: {bp['status']} -> After: {cp['status']}",
            })

        # (b) リダイレクト先URLの変化 (SSL化やmembersドメイン変更に伴う正規URL変化は許容)
        bp_final = rewrite_in_scope_url(bp.get("final_decoded_url"), curr_base_url, curr_members_url)
        cp_final = cp.get("final_decoded_url")
        if bp_final != cp_final:
            diffs.append({
                "category": "リダイレクト先変化",
                "target": decoded,
                "detail": f"Before: {bp.get('final_decoded_url')} -> After: {cp_final}",
            })

        # (c) PHPエラー・WordPress重大エラーの混入
        if cp.get("php_errors") and cp.get("php_errors") != bp.get("php_errors"):
            diffs.append({
                "category": "PHP/WPエラー発生",
                "target": decoded,
                "detail": "; ".join(cp["php_errors"]),
            })

        # (d) test.biancafiori.com URLの混入
        if cp.get("test_domain_refs") and cp.get("test_domain_refs") != bp.get("test_domain_refs"):
            diffs.append({
                "category": "テストURL混入",
                "target": decoded,
                "detail": f"test.biancafiori.com への参照: {cp['test_domain_refs'][:3]}",
            })

        # (d2) Mixed Content (HTTPSページ内の http:// アセット残存) の検出
        if cp.get("mixed_content_refs"):
            diffs.append({
                "category": "Mixed Content (httpアセット残存)",
                "target": decoded,
                "detail": f"ページ内のアセットに http:// が残っています ({len(cp['mixed_content_refs'])}件): {cp['mixed_content_refs'][:3]}",
            })

        if bp["status"] == 200 and cp["status"] == 200:
            # (e) <title> の変化
            if bp.get("title") != cp.get("title"):
                diffs.append({
                    "category": "タイトル(<title>)変化",
                    "target": decoded,
                    "detail": f"Before: '{bp.get('title')}' -> After: '{cp.get('title')}'",
                })

            # (f) 見出し (h1/h2) の変化
            if bp.get("headings") != cp.get("headings"):
                diffs.append({
                    "category": "見出し(h1/h2)変化",
                    "target": decoded,
                    "detail": f"Before: {bp.get('headings')} -> After: {cp.get('headings')}",
                })

            # (g) 本文テキストの変化 (本文中の対象サイトURLの http/https や members URL変化は許容)
            if bp.get("content_hash") != cp.get("content_hash"):
                b_text = normalize_in_scope_text_scheme(bp.get("content_text", ""), curr_base_url, curr_members_url)
                c_text = normalize_in_scope_text_scheme(cp.get("content_text", ""), curr_base_url, curr_members_url)
                if b_text != c_text:
                    b_len = bp.get("content_length", 0)
                    c_len = cp.get("content_length", 0)
                    # 差分箇所の抜粋を作成
                    matcher = difflib.SequenceMatcher(None, b_text, c_text)
                    diff_snippets = []
                    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                        if tag != "equal":
                            diff_snippets.append(f"[{tag}] '{b_text[i1:i2][:40]}' -> '{c_text[j1:j2][:40]}'")
                            if len(diff_snippets) >= 3:
                                break
                    snippet_str = " / ".join(diff_snippets)
                    diffs.append({
                        "category": "本文テキスト変化",
                        "target": decoded,
                        "detail": f"文字数: {b_len} -> {c_len} ({snippet_str})",
                    })

            # (h) ページ内リンク一覧の変化 (対象サイトURLの http/https や members URL変化は許容)
            b_links = {rewrite_in_scope_url(l, curr_base_url, curr_members_url) for l in bp.get("links", [])}
            c_links = {rewrite_in_scope_url(l, curr_base_url, curr_members_url) for l in cp.get("links", [])}
            if b_links != c_links:
                removed = sorted(b_links - c_links)
                added = sorted(c_links - b_links)
                diffs.append({
                    "category": "ページ内リンク変化",
                    "target": decoded,
                    "detail": f"削除: {removed[:3]} / 追加: {added[:3]}",
                })

    # 3. アセット（画像・CSS・JS・PDF・内部リンク）のステータス変化チェック
    for u, ba in sorted(base_assets.items()):
        parsed = urllib.parse.urlsplit(u)
        path = urllib.parse.unquote(parsed.path)
        if path.startswith("/30_years_anniversary") and not any(
            b["credentials_provided"] for b in current.get("basic_auth_checks", []) if b.get("id") == "30_years_anniversary"
        ):
            continue
        if is_members_scope_url(u, curr_members_url) and not any(
            b["credentials_provided"] for b in current.get("basic_auth_checks", []) if b.get("id") == "members"
        ):
            continue

        ca = curr_assets.get(u)
        if not ca:
            if ba["status"] == 200:
                diffs.append({
                    "category": "アセット・リンク参照消失",
                    "target": rewrite_in_scope_url(ba["decoded_url"], curr_base_url, curr_members_url),
                    "detail": f"ベースライン時に存在したアセット (参照元: {ba.get('sources', [])[:2]}) が見つかりません",
                })
            continue

        if ba["status"] != ca["status"]:
            diffs.append({
                "category": "アセット・内部リンクのステータス変化",
                "target": ca["decoded_url"],
                "detail": f"Before: {ba['status']} -> After: {ca['status']} (参照元: {ca.get('sources', [])[:2]})",
            })

    return {
        "passed": len(diffs) == 0,
        "diffs": diffs,
        "summary": {
            "total_pages_checked": len(curr_pages),
            "pages_200_ok": sum(1 for p in curr_pages.values() if p["status"] == 200),
            "total_assets_checked": len(curr_assets),
            "assets_200_ok": sum(1 for a in curr_assets.values() if a["status"] == 200),
            "diffs_count": len(diffs),
        },
    }


def generate_markdown_report(baseline: dict, current: dict, comparison: dict) -> str:
    s = comparison["summary"]
    status_badge = "✅ PASS (ベースラインから変化なし)" if comparison["passed"] else f"❌ FAIL ({s['diffs_count']} 件の差分・異常を検出)"

    lines = [
        "# Biancafiori WordPress Upgrade Regression Report",
        "",
        f"- **判定結果**: **{status_badge}**",
        f"- **ベースライン記録日時**: `{baseline.get('timestamp')}` (`{baseline.get('base_url', BASE_URL)}` / `{baseline.get('members_url') or 'スキップ'}`)",
        f"- **検証実行日時**: `{current.get('timestamp')}`",
        f"- **対象URL**: `{current.get('base_url')}` / `{current.get('members_url') or 'スキップ'}`",
        "",
        "## 1. サマリー",
        "",
        "| 項目 | 結果 |",
        "| :--- | :--- |",
        f"| 巡回ページ数 (200 OK / 全体) | **{s['pages_200_ok']} / {s['total_pages_checked']}** |",
        f"| 内部アセット・リンク数 (200 OK / 全体) | **{s['assets_200_ok']} / {s['total_assets_checked']}** |",
        f"| ベースラインとの差分・エラー数 | **{s['diffs_count']} 件** |",
        "",
        "## 2. BASIC認証サイトの検証結果",
        "",
        "| サイト | 認証なしアクセス (期待値: 401) | 認証ありアクセス (期待値: 200) | 備考 |",
        "| :--- | :--- | :--- | :--- |",
    ]

    for b in current.get("basic_auth_checks", []):
        p_ok = "✅ 401 OK" if b["protected_ok"] else f"❌ {b['unauth_status']} NG"
        if not b["credentials_provided"]:
            a_ok = "⏭️ 未指定(スキップ)"
        elif b["auth_ok"]:
            a_ok = "✅ 200 OK"
        else:
            a_ok = f"❌ {b['auth_status']} NG"
        lines.append(f"| {b['name']} | {p_ok} | {a_ok} | {b['note'] or '正常'} |")

    lines.extend(["", f"## 3. 検出された差分・異常一覧 ({len(comparison['diffs'])} 件)", ""])
    if not comparison["diffs"]:
        lines.append("アップグレード前後で変化したページ・本文・リンク・アセットはありません。")
    else:
        lines.append("| カテゴリ | 対象URL / 箇所 | 差分内容 |")
        lines.append("| :--- | :--- | :--- |")
        for d in comparison["diffs"]:
            lines.append(f"| **{d['category']}** | `{d['target']}` | {d['detail']} |")

    lines.append("")
    return "\n".join(lines)


def prompt_credentials_if_needed(args) -> tuple[str | None, str | None]:
    auth_30 = args.auth_30 or args.auth
    auth_members = args.auth_members or args.auth

    if args.no_prompt or not sys.stdin.isatty():
        return auth_30, auth_members

    default_members = HTTPS_MEMBERS_URL if getattr(args, "https", False) else MEMBERS_URL
    members_url_disp = getattr(args, "members_url", None) or default_members

    if not auth_30:
        print(f"\n{Colors.BOLD}[BASIC認証入力] 30周年記念サイト (/30_years_anniversary/){Colors.RESET}")
        user_30 = input("  ユーザー名 (空Enterで認証後クロールをスキップ): ").strip()
        if user_30:
            pass_30 = getpass.getpass("  パスワード: ")
            auth_30 = f"{user_30}:{pass_30}"

    if not args.skip_members and not auth_members:
        print(f"\n{Colors.BOLD}[BASIC認証入力] 団員専用サイト ({members_url_disp}){Colors.RESET}")
        if auth_30:
            same = input("  30周年記念サイトと同じ認証情報を使いますか？ [Y/n]: ").strip().lower()
            if same in ("", "y", "yes"):
                auth_members = auth_30
                return auth_30, auth_members
        user_m = input("  ユーザー名 (空Enterで認証後クロールをスキップ): ").strip()
        if user_m:
            pass_m = getpass.getpass("  パスワード: ")
            auth_members = f"{user_m}:{pass_m}"

    return auth_30, auth_members


def main():
    parser = argparse.ArgumentParser(
        description="http://biancafiori.com WordPress Upgrade Regression Test",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "mode",
        choices=["record", "verify"],
        help=(
            "record : アップグレード前の対象サイトの状態を baseline.json に保存します\n"
            "verify : アップグレード後の対象サイトを巡回し、baseline.json から変化がないか検証します"
        ),
    )
    parser.add_argument(
        "--https",
        action="store_true",
        help=(
            "SSL化・移転後のURL体系 (https://biancafiori.com および https://members.biancafiori.com/) で巡回・検証し、\n"
            "旧URLベースラインとの自動読み替え比較および Mixed Content チェックを行う"
        ),
    )
    parser.add_argument(
        "--base-url",
        help="メインサイトのベースURLを個別に指定する場合に使用 (例: https://biancafiori.com)",
    )
    parser.add_argument(
        "--members-url",
        help=(
            "団員専用サイトのURLを個別に指定する場合に使用 (例: https://members.biancafiori.com/)\n"
            "※ ベースラインの http://biancafiori.ciao.jp/members/ から自動で読み替えて比較します"
        ),
    )
    parser.add_argument(
        "--auth",
        help="全BASIC認証サイト共通の認証情報 ('USER:PASS' 形式)",
    )
    parser.add_argument(
        "--auth-30",
        help="30周年記念サイト (/30_years_anniversary/) のBASIC認証情報 ('USER:PASS' 形式)",
    )
    parser.add_argument(
        "--auth-members",
        help="団員専用サイト (members) のBASIC認証情報 ('USER:PASS' 形式)",
    )
    parser.add_argument(
        "--skip-members",
        action="store_true",
        help="団員専用サイトを対象外にする",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="対話プロンプトでのBASIC認証入力を行わない（引数未指定時は401保護チェックのみ実施）",
    )
    parser.add_argument(
        "--baseline",
        default=DEFAULT_BASELINE_FILE,
        help=f"ベースラインJSONファイルのパス (デフォルト: {DEFAULT_BASELINE_FILE})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="並列リクエスト数 (デフォルト: 8)",
    )
    args = parser.parse_args()

    baseline_data = None
    baseline_urls = None
    if args.mode == "verify":
        if not os.path.exists(args.baseline):
            print(f"{Colors.RED}エラー: ベースラインファイルが見つかりません: {args.baseline}{Colors.RESET}")
            print("先に `python3 regression_test.py record` を実行してベースラインを作成してください。")
            sys.exit(2)
        with open(args.baseline, "r", encoding="utf-8") as f:
            baseline_data = json.load(f)
        baseline_urls = list(baseline_data.get("pages", {}).keys())

    auth_30, auth_members = prompt_credentials_if_needed(args)

    crawler = SiteCrawler(
        auth_30=auth_30,
        auth_members=auth_members,
        skip_members=args.skip_members,
        use_https=args.https,
        base_url=args.base_url,
        members_url=args.members_url,
        workers=args.workers,
    )

    start_time = time.time()
    current_data = crawler.run(baseline_urls=baseline_urls)
    elapsed = time.time() - start_time

    if args.mode == "record":
        os.makedirs(os.path.dirname(os.path.abspath(args.baseline)), exist_ok=True)
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=False, indent=2)
        p_cnt = len(current_data["pages"])
        a_cnt = len(current_data["assets"])
        print("\n" + "=" * 70)
        print(f"{Colors.GREEN}{Colors.BOLD}✓ ベースライン記録完了: {args.baseline}{Colors.RESET}  [所要時間: {elapsed:.1f}秒]")
        print(f"  - 記録ページ数         : {p_cnt} 件")
        print(f"  - 記録アセット・リンク : {a_cnt} 件")
        print("=" * 70)
        sys.exit(0)

    # verify モード
    comp = compare_before_after(baseline_data, current_data)

    os.makedirs(DEFAULT_REPORT_DIR, exist_ok=True)
    ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = os.path.join(DEFAULT_REPORT_DIR, f"report_verify_{ts_str}.md")
    json_path = os.path.join(DEFAULT_REPORT_DIR, f"result_verify_{ts_str}.json")

    md_text = generate_markdown_report(baseline_data, current_data, comp)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"current": current_data, "comparison": comp}, f, ensure_ascii=False, indent=2)

    s = comp["summary"]
    print("\n" + "=" * 70)
    if comp["passed"]:
        print(f"{Colors.GREEN}{Colors.BOLD}判定結果: PASS (アップグレード前後の差分 0 件){Colors.RESET}  [所要時間: {elapsed:.1f}秒]")
    else:
        print(f"{Colors.RED}{Colors.BOLD}判定結果: FAIL ({s['diffs_count']} 件の差分・異常を検出){Colors.RESET}  [所要時間: {elapsed:.1f}秒]")
    print("=" * 70)
    print(f"  - 巡回ページ数         : {s['pages_200_ok']} / {s['total_pages_checked']} (200 OK)")
    print(f"  - 内部アセット・リンク : {s['assets_200_ok']} / {s['total_assets_checked']} (200 OK)")
    print(f"  - ベースラインとの差分 : {s['diffs_count']} 件")
    print(f"  - レポート出力先       : {md_path}")
    print("=" * 70)

    if comp["diffs"]:
        print(f"\n{Colors.RED}{Colors.BOLD}[検出された差分一覧 (最大20件表示)]{Colors.RESET}")
        for d_item in comp["diffs"][:20]:
            print(f"  ✗ [{d_item['category']}] {d_item['target']} -> {d_item['detail']}")
        if len(comp["diffs"]) > 20:
            print(f"  ... 他 {len(comp['diffs']) - 20} 件（詳細はレポートファイルを参照してください）")

    sys.exit(0 if comp["passed"] else 1)


if __name__ == "__main__":
    main()
