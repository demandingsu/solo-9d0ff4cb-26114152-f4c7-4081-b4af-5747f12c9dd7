"""采集编排：列表页发现 → 详情页抓取 → 复核变化 → 业务合并。

复核（recheck）是本模块的关键步骤：采购站可能在“抓列表”与“抓详情”之间发布更正。
因此抓齐本项目全部详情后，会再读一次列表页、并重抓已解析的本项目详情页，核对：
* 列表中本项目的关联链接是否增删；
* 已抓详情页的发布时间、截止时间、取消状态是否变化；
* 复核请求本身是否失败。
任一复核异常都会让结果进入 ``pending`` 并清空有效期限，同时在 ``changes`` 与
``sources`` 中列出变化来源；无关项目的目录/详情变化不影响本项目。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from app.merger import MergeInput, merge_notices
from app.models import (
    ChangeAlert,
    FetchRequest,
    FetchResponse,
    NoticeKind,
    NoticeRecord,
    ProjectStatus,
    SourceLink,
)
from app.parser import DetailData, ParseError, extract_detail_links, parse_detail

_MAX_CONCURRENCY = 5
_USER_AGENT = "TenderCollector/1.0 (+local procurement announcement API)"
# 项目编号匹配时容忍大小写/空格/分隔符差异，如 “GSB-2026-001” 与 “GSB 2026 001”
_CODE_NORM = str.maketrans({" ": "", "　": "", "_": "", "/": "", "\\": "", "-": ""})
# 合理公告年份窗口，过滤“近三年（2023年1月1日…）”这类业绩/资质说明里的日期
_MIN_YEAR = 2000
_MAX_YEAR = 2100


def _normalize_code(code: str) -> str:
    return code.strip().lower().translate(_CODE_NORM)


def _code_matches(page_code: str | None, requested: str, title: str, row_context: str) -> bool:
    target = _normalize_code(requested)
    if page_code and _normalize_code(page_code) == target:
        return True
    # 详情页没有结构化编号时，退化为标题/列表行上下文包含匹配
    haystack = _normalize_code(f"{title} {row_context}")
    return target in haystack


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    # 4xx/5xx（如详情页下线返回 404）一律视为抓取失败
    response.raise_for_status()
    return response.text


@dataclass
class _ParsedNotice:
    """首次抓取解析成功、归入本项目的详情页，保留复核所需的原始解析结果。"""

    url: str
    context: str
    data: DetailData
    record: NoticeRecord


@dataclass
class _RecheckOutcome:
    changes: list[ChangeAlert]
    reread: list[_ParsedNotice]  # 复核时重新读到的本项目详情（含新增链接）
    still_failed: list[str]      # 原本抓取失败、复核时确实仍失败/仍未归入本项目的链接


_FIELD_NAMES = {
    "published_at": "发布时间",
    "deadline": "投标截止时间",
    "cancelled": "取消状态",
}

# 列表行上下文中的项目编号形态（如 GSB-2026-099），用于判定新增链接是否明确属于其他项目
_CODE_LIKE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_/ ][A-Za-z0-9]+)+")


def _row_names_other_project(context: str, target_code: str) -> bool:
    """列表行只写着其他项目编号 → 该链接与目标项目无关，不因目录变化而待核实。"""
    tokens = [_normalize_code(token) for token in _CODE_LIKE.findall(context or "")]
    return bool(tokens) and target_code not in tokens


def _same_project_changed(old: DetailData, new: DetailData, url: str) -> list[ChangeAlert]:
    """对比同一详情页复核前后的关键字段，只报告确实发生的变化。"""
    changes: list[ChangeAlert] = []
    for field_name, label in _FIELD_NAMES.items():
        before, after = getattr(old, field_name), getattr(new, field_name)
        if before == after:
            continue
        changes.append(
            ChangeAlert(
                kind="field_changed",
                url=url,
                message=f"{url} 的{label}由 {before} 变为 {after}",
            )
        )
    return changes


def _project_links(
    candidates: list[tuple[str, str, str]], project_code: str
) -> dict[str, str]:
    """从列表候选链接中筛出列表行上下文明确提及本项目的链接（URL → 行上下文）。"""
    target = _normalize_code(project_code)
    return {url: context for url, _text, context in candidates if target in _normalize_code(context)}


async def _recheck(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    list_url: str,
    req: FetchRequest,
    initial_links: dict[str, str],
    initial_urls: set[str],
    parsed: list[_ParsedNotice],
    initial_failures: list[str],
) -> _RecheckOutcome:
    """抓齐本项目详情后再核对列表关联链接与已抓详情的关键字段。

    复核读取失败按“变化”处理：读取不到的瞬间无法确认公告未变，
    不能把首次抓到的旧期限当作最终期限。
    """
    changes: list[ChangeAlert] = []
    reread: list[_ParsedNotice] = []
    still_failed: list[str] = []
    parsed_by_url = {item.url: item for item in parsed}
    # 已确认属于本项目的链接：列表行上下文命中，或详情页项目编号命中
    known_urls = set(initial_links) | set(parsed_by_url)

    # 1) 复核列表页：本项目链接增删
    try:
        list_html = await _fetch(client, list_url)
    except httpx.HTTPError as exc:
        changes.append(
            ChangeAlert(
                kind="recheck_failed",
                url=list_url,
                message=f"复核读取列表页失败，无法确认本项目链接未变化：{list_url}（{exc.__class__.__name__}）",
            )
        )
        return _RecheckOutcome(
            changes=changes, reread=reread, still_failed=list(initial_failures)
        )

    current_candidates = extract_detail_links(list_html, list_url)
    current_urls = {url for url, _text, _context in current_candidates}
    current_links = _project_links(current_candidates, req.project_code)

    added = set(current_links.keys()) - known_urls
    # 列表新增链接的行上下文可能没有项目编号列，需读取详情页确认归属本项目
    async def probe_new(url: str) -> tuple[str, str, str]:
        async with semaphore:
            try:
                return url, await _fetch(client, url), ""
            except httpx.HTTPError as exc:
                return url, "", exc.__class__.__name__

    new_urls = sorted((current_urls - initial_urls - known_urls) | added)
    if new_urls:
        probed = await asyncio.gather(*(probe_new(url) for url in new_urls))
        for url, html, error in probed:
            context = next((c for u, _t, c in current_candidates if u == url), "")
            if html:
                try:
                    data = parse_detail(html, url)
                except ParseError:
                    still_failed.append(url)
                    continue
                if not _code_matches(data.project_code, req.project_code, data.title, context):
                    if url in added:
                        # 列表行写着本项目、详情页却无法确认归属：按变化来源待核实
                        changes.append(
                            ChangeAlert(
                                kind="recheck_failed",
                                url=url,
                                message=f"复核新增链接的详情页无法确认属于本项目：{url}",
                            )
                        )
                    continue  # 明确属于其他项目，与本项目无关
                added.add(url)
                reread.append(_parsed_notice(url, context, data))
                continue
            if error and _row_names_other_project(context, _normalize_code(req.project_code)):
                # 列表行明确写着其他项目编号，读取失败也不影响本项目
                continue
            if url in added:
                still_failed.append(url)
            # 新增链接无法读取且无法排除属于本项目 → 按待核实来源记录
            changes.append(
                ChangeAlert(
                    kind="recheck_failed",
                    url=url,
                    message=f"复核发现列表新增链接但无法读取，无法确认其是否属于本项目：{url}",
                )
            )

    for url in sorted(added):
        changes.append(
            ChangeAlert(
                kind="link_added",
                url=url,
                message=f"复核发现列表新增本项目关联链接：{url}（首次抓取时不存在，可能为刚发布的更正）",
            )
        )
    removed = {url for url in known_urls if url not in current_urls}
    # 链接仍在列表、但列表行已不再把该项目归入本项目，也视为关联链接被移除
    removed |= {
        url for url in initial_links if url in current_urls and url not in current_links
    }
    for url in sorted(removed):
        changes.append(
            ChangeAlert(
                kind="link_removed",
                url=url,
                message=f"复核发现列表已移除本项目关联链接：{url}（首次抓取时存在）",
            )
        )

    # 无关项目的目录变化不产生以上差异，因此不会影响本项目结果。

    # 2) 复核已抓详情页的发布时间 / 截止时间 / 取消状态
    async def reread_one(url: str) -> tuple[str, str]:
        async with semaphore:
            try:
                return url, await _fetch(client, url)
            except httpx.HTTPError:
                return url, ""

    targets = list(parsed_by_url)
    results = await asyncio.gather(*(reread_one(url) for url in targets))
    for url, html in results:
        original = parsed_by_url[url]
        if not html:
            changes.append(
                ChangeAlert(
                    kind="recheck_failed",
                    url=url,
                    message=f"复核读取详情页失败，无法确认公告未变化：{url}",
                )
            )
            continue
        try:
            data = parse_detail(html, url)
        except ParseError:
            changes.append(
                ChangeAlert(
                    kind="recheck_failed",
                    url=url,
                    message=f"复核解析详情页失败，无法确认公告未变化：{url}",
                )
            )
            continue
        if not _code_matches(data.project_code, req.project_code, data.title, original.context):
            changes.append(
                ChangeAlert(
                    kind="recheck_failed",
                    url=url,
                    message=f"复核时详情页不再属于本项目或项目编号无法确认：{url}",
                )
            )
            continue
        changes.extend(_same_project_changed(original.data, data, url))
        reread.append(_parsed_notice(url, original.context, data))

    # 3) 首次抓取失败的链接：复核确认是否仍失败
    async def refetch_failed(url: str) -> tuple[str, str]:
        async with semaphore:
            try:
                return url, await _fetch(client, url)
            except httpx.HTTPError:
                return url, ""

    failed_results = await asyncio.gather(*(refetch_failed(url) for url in initial_failures))
    for url, html in failed_results:
        if not html:
            still_failed.append(url)
            continue
        try:
            data = parse_detail(html, url)
        except ParseError:
            still_failed.append(url)
            continue
        # 首次失败后被修复：重新纳入本项目（若它确实属于本项目）
        context = initial_links.get(url, "")
        if not _code_matches(data.project_code, req.project_code, data.title, context):
            still_failed.append(url)
            changes.append(
                ChangeAlert(
                    kind="recheck_failed",
                    url=url,
                    message=f"复核时此前失败的详情页可读取，但无法确认其属于本项目：{url}",
                )
            )
            continue
        changes.append(
            ChangeAlert(
                kind="recheck_recovered",
                url=url,
                message=f"复核发现此前抓取失败的详情页已可读取：{url}，需重新确认公告内容",
            )
        )
        reread.append(_parsed_notice(url, context, data))

    return _RecheckOutcome(changes=changes, reread=reread, still_failed=still_failed)


def _notice_kind(data: DetailData) -> NoticeKind:
    return {
        "original": NoticeKind.ORIGINAL,
        "correction": NoticeKind.CORRECTION,
        "cancellation": NoticeKind.CANCELLATION,
    }[data.kind_hint]


def _parsed_notice(url: str, context: str, data: DetailData) -> _ParsedNotice:
    return _ParsedNotice(
        url=url,
        context=context,
        data=data,
        record=NoticeRecord(
            url=url,
            kind=_notice_kind(data),
            title=data.title,
            published_at=data.published_at,
            deadline=data.deadline,
            cancelled=data.cancelled,
            cancellation_reason=data.cancellation_reason,
        ),
    )


async def collect(req: FetchRequest) -> FetchResponse:
    list_url = str(req.list_url)
    timeout = httpx.Timeout(req.timeout)
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
    ) as client:
        # 1) 抓列表页
        try:
            list_html = await _fetch(client, list_url)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"列表页抓取失败：{list_url}（{exc.__class__.__name__}）",
            ) from exc

        # 2) 发现同站点详情链接（URL, 链接文本, 行上下文）
        candidates = extract_detail_links(list_html, list_url)
        target_code = _normalize_code(req.project_code)
        # 本项目在列表里完全没有痕迹（无链接、无失败候选）时首轮仍按 not_found 候选处理，
        # 但随后一律复核：若复核列表出现本项目的新链接，改判 pending 待核实。
        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def fetch_one(url: str, link_text: str, context: str) -> tuple[str, str, str, str]:
            async with semaphore:
                try:
                    html = await _fetch(client, url)
                    return url, html, link_text, context
                except httpx.HTTPError:
                    return url, "", link_text, context  # 空 html 表示失败

        # 3) 并发抓取候选详情页（无候选时为空列表，仍进入复核）
        results = await asyncio.gather(*(fetch_one(u, t, c) for u, t, c in candidates))

        notices: list[NoticeRecord] = []
        parsed: list[_ParsedNotice] = []
        failures: list[str] = []
        initial_links = _project_links(candidates, req.project_code)
        initial_urls = {url for url, _text, _context in candidates}

        for url, html, link_text, context in results:
            # 列表行上下文包含目标项目编号 → 该链接属于本项目（即使详情页抓不回来）
            row_mentions_project = target_code in _normalize_code(context)

            if not html:
                if row_mentions_project:
                    failures.append(url)
                continue
            try:
                data = parse_detail(html, url)
            except ParseError:
                if row_mentions_project:
                    failures.append(url)
                continue

            if not _code_matches(data.project_code, req.project_code, data.title, context):
                continue  # 同站点但属于其他项目，忽略

            parsed.append(_parsed_notice(url, context, data))
            # 缺发布时间在 merger 中按待核实处理。

        notices = [item.record for item in parsed]

        # 3.5) 复核：抓齐本项目详情后再核对列表关联链接与已抓详情的关键字段。
        #      即使首次列表没有任何同站链接也复核一次（本地上游可能恰好此时发布公告）。
        changes: list[ChangeAlert] = []
        outcome = await _recheck(
            client,
            semaphore,
            list_url,
            req,
            initial_links,
            initial_urls,
            parsed,
            list(failures),
        )
        changes = outcome.changes
        if outcome.reread or changes:
            # 复核成功读到的页面用最新内容替换；复核读取失败的页面保留首次解析结果，
            # 仅凭 changes 标记待核实，避免丢失已知公告明细。
            latest = {item.url: item.record for item in outcome.reread}
            known_urls = {item.url for item in parsed}
            notices = [latest.get(item.url, item.record) for item in parsed]
            notices.extend(item.record for item in outcome.reread if item.url not in known_urls)
            failures = outcome.still_failed

    # 4) 业务合并
    merge_result = merge_notices(
        MergeInput(
            notices=notices,
            failures=sorted(set(failures)),
            changes=[alert.message for alert in changes],
        )
    )
    if not candidates and merge_result.status == ProjectStatus.NOT_FOUND:
        merge_result.warnings.append("列表页未发现任何同站点详情链接")

    # 5) 组装来源链接
    sources: list[SourceLink] = [
        SourceLink(role="list", url=list_url, title="采购公告列表页")
    ]
    role_map = {
        NoticeKind.ORIGINAL: "original",
        NoticeKind.CORRECTION: "correction",
        NoticeKind.CANCELLATION: "cancellation",
    }
    for notice in merge_result.ordered_notices:
        sources.append(
            SourceLink(role=role_map[notice.kind], url=notice.url, title=notice.title)
        )
    for failed_url in sorted(set(failures)):
        sources.append(SourceLink(role="failed", url=failed_url, title="抓取失败-待核实"))
    for alert in changes:
        sources.append(SourceLink(role="changed", url=alert.url, title=alert.message))

    return FetchResponse(
        project_code=req.project_code,
        list_url=req.list_url,
        status=merge_result.status,
        can_bid=merge_result.can_bid,
        effective_deadline=merge_result.effective_deadline,
        cancelled=merge_result.cancelled,
        notices=merge_result.ordered_notices,
        sources=sources,
        changes=changes,
        warnings=merge_result.warnings,
        fetched_at=datetime.now(timezone.utc),
    )
