"""
KR adapter — Seoul mirror of safetykorea (제품안전정보센터).

Source: sftc.seoul.go.kr 소비자 안전정보 > 국내 리콜정보. The city republishes
safetykorea recall data ("정보 출처인 제품안전정보센터") on a plain
GET-paginated table with NO authentication — our zero-signup door into
Korean recalls while the safetykorea Open API application is pending.

Verified live 2026-08-19: 1,240 domestic records / 124 pages, columns
제품명(카테고리 괄호 내장) · 모델명 · 사업자명 · 리콜종류 · 공표일.
Window: the mirror keeps the most recent ~3 years only — fine for the
daily body, NOT a backfill source (full history needs the safetykorea API).

Identity: the mirror's 번호 column is a list ordinal, not a stable id —
using it would invent identity. The collector tries to extract the real
safetykorea uid from each row's detail link (onclick/href patterns); when
none is exposed, the record's identity IS its natural key
(공표일 + 제품명 + 모델명 + 사업자), recorded verbatim in id_scheme so a
later safetykorea-API record can supersede it by declared match.
"""

import hashlib
import re

UID_PATTERNS = [
    # Real markup observed 2026-08-19 in the mirror's list HTML:
    #   <a href="#" onclick="viewDtail(10022792)">저속전동이륜차</a>
    # and 10022792 is EXACTLY the safetykorea recallUid — verified equal to
    # the numeric part of OECD KR native_id K26/EP/10022792 (same product,
    # same date). One id family across mirror, safetykorea, and OECD.
    re.compile(r"viewDtail\s*\(\s*['\"]?(\d+)"),
    re.compile(r"recallUid=(\d+)"),
    re.compile(r"fn\w*View\w*\(\s*['\"]?(\d+)"),
    re.compile(r"NR_view\.do[^\"']*[?&](?:uid|seq|idx|recallUid|boardSeq)=(\d+)"),
    re.compile(r"data-(?:uid|seq|id)=[\"'](\d+)"),
    # last resort: any onclick handler whose first argument is a long number
    # (uids are 8 digits; page numbers are short, so require 6+)
    re.compile(r"onclick=\"[A-Za-z_$][\w$]*\(\s*['\"]?(\d{6,})"),
]

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
DATE_RE = re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")


def _clean(cell_html: str) -> str:
    txt = TAG_RE.sub(" ", cell_html)
    txt = (txt.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
              .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", txt).strip()


def _uid(row_html: str):
    for pat in UID_PATTERNS:
        m = pat.search(row_html)
        if m:
            return m.group(1)
    return None


def _category(product_name: str):
    """The mirror embeds the KATS 품목 in trailing parens:
    '수영보조용품(착용형)(물놀이기구)' -> '물놀이기구'."""
    parens = re.findall(r"\(([^()]+)\)", product_name)
    return parens[-1].strip() if parens else ""


def parse_list_page(html: str, list_kind: str = "domestic"):
    """Yield normalized records from one NR_list.do page's raw HTML."""
    out = []
    for row_html in ROW_RE.findall(html):
        cells = [_clean(c) for c in CELL_RE.findall(row_html)]
        # data rows: 번호 · 제품명 · 모델명 · 사업자명 · 리콜종류 · 공표일
        if len(cells) < 6 or not cells[0].isdigit():
            continue
        m = DATE_RE.search(cells[5])
        if not m:
            continue
        date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        product, model, company, kind = cells[1], cells[2], cells[3], cells[4]
        uid = _uid(row_html)
        if uid:
            rid, scheme = f"KR:{uid}", "safetykorea_uid"
        else:
            nk = f"{date}|{product}|{model}|{company}"
            rid = "KR:nk-" + hashlib.sha1(nk.encode()).hexdigest()[:16]
            scheme = "natural_key"
        mandatory = "명령" in kind
        out.append({
            "id": rid,
            "id_scheme": scheme,
            "jurisdiction": "KR",
            "agency": "KATS/제품안전정보센터 (via Seoul mirror)",
            "record_type": "recall",
            "title": product,
            "product_name": product,
            "category_source": _category(product),
            "model": model,
            "company": company,
            "recall_kind_source": kind,            # verbatim: 명령에따른리콜/자발적리콜
            "enforcement": "mandatory" if mandatory else "voluntary",
            "date_published": date,
            "list_kind": list_kind,                # domestic | overseas
            "source_url": ("https://sftc.seoul.go.kr/fe/si/recallDmstc/NR_list.do"
                           if list_kind == "domestic" else
                           "https://sftc.seoul.go.kr/fe/si/recallOutnatn/NR_list.do"),
            "source_payload": {"cells": cells, "uid": uid},
        })
    return out
