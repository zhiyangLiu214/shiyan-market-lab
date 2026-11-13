from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from io import StringIO
from typing import Any

import akshare as ak
import pandas as pd
import requests
import py_mini_racer
from akshare.datasets import get_ths_js
from flask import Flask, jsonify, render_template_string, request


app = Flask(__name__)


CACHE_TTL_SECONDS = 20
DEFAULT_BOARD_COUNT = 3
DEFAULT_LEADERS_PER_BOARD = 3
MAX_BOARD_COUNT = 12
MAX_LEADERS_PER_BOARD = 10


@dataclass
class CacheEntry:
    created_at: float
    data: Any


cache: dict[str, CacheEntry] = {}


for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(proxy_name, None)

_original_session_init = requests.Session.__init__


def _session_init_without_env_proxy(self, *args, **kwargs):
    _original_session_init(self, *args, **kwargs)
    self.trust_env = False


requests.Session.__init__ = _session_init_without_env_proxy


def cached(key: str, loader, ttl: int = CACHE_TTL_SECONDS):
    now = time.time()
    entry = cache.get(key)
    if entry and now - entry.created_at <= ttl:
        return entry.data
    data = loader()
    cache[key] = CacheEntry(now, data)
    return data


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "-":
            return default
        result = float(value)
        if math.isnan(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def cn_amount_to_yi(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "--"):
        return 0.0
    multiplier = 1.0
    if text.endswith("万"):
        multiplier = 0.0001
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 1.0
        text = text[:-1]
    return to_float(text) * multiplier


def pick_column(df: pd.DataFrame, candidates: list[str]) -> str:
    for column in candidates:
        if column in df.columns:
            return column
    raise KeyError(f"Missing expected columns. Have: {list(df.columns)}")


def normalize_board_df(df: pd.DataFrame) -> pd.DataFrame:
    name_col = pick_column(df, ["板块名称", "板块", "名称"])
    pct_col = pick_column(df, ["涨跌幅", "涨跌幅%", "涨幅"])
    amount_col = next((c for c in ["成交额", "成交金额", "总成交额"] if c in df.columns), None)
    rise_count_col = next((c for c in ["上涨家数", "上涨数"] if c in df.columns), None)
    fall_count_col = next((c for c in ["下跌家数", "下跌数"] if c in df.columns), None)
    leader_col = next((c for c in ["领涨股", "领涨股票"] if c in df.columns), None)

    result = pd.DataFrame(
        {
            "name": df[name_col].astype(str),
            "pct": df[pct_col].map(to_float),
            "amount_yi": df[amount_col].map(to_float) if amount_col else 0,
            "rise_count": df[rise_count_col].map(to_float) if rise_count_col else 0,
            "fall_count": df[fall_count_col].map(to_float) if fall_count_col else 0,
            "leader": df[leader_col].astype(str) if leader_col else "",
        }
    )
    return result.sort_values("pct", ascending=False).reset_index(drop=True)


def normalize_stock_df(df: pd.DataFrame) -> pd.DataFrame:
    code_col = pick_column(df, ["代码", "股票代码"])
    name_col = pick_column(df, ["名称", "股票名称"])
    pct_col = pick_column(df, ["涨跌幅", "涨跌幅%"])
    price_col = next((c for c in ["最新价", "收盘"] if c in df.columns), None)
    amount_col = next((c for c in ["成交额", "成交金额"] if c in df.columns), None)
    turnover_col = next((c for c in ["换手率"] if c in df.columns), None)
    volume_ratio_col = next((c for c in ["量比"] if c in df.columns), None)

    result = pd.DataFrame(
        {
            "code": df[code_col].astype(str).str.zfill(6),
            "name": df[name_col].astype(str),
            "pct": df[pct_col].map(to_float),
            "price": df[price_col].map(to_float) if price_col else 0,
            "amount": df[amount_col].map(cn_amount_to_yi) if amount_col else 0,
            "turnover": df[turnover_col].map(to_float) if turnover_col else 0,
            "volume_ratio": df[volume_ratio_col].map(to_float) if volume_ratio_col else 0,
        }
    )
    return result


def load_boards() -> pd.DataFrame:
    return cached(
        "industry_boards_ths",
        lambda: normalize_board_df(ak.stock_board_industry_summary_ths()),
    )


def load_board_codes() -> dict[str, str]:
    def loader():
        df = ak.stock_board_industry_name_ths()
        return dict(zip(df["name"], df["code"]))

    return cached(
        "industry_codes_ths",
        loader,
        ttl=3600,
    )


def ths_cookie() -> str:
    js_code = py_mini_racer.MiniRacer()
    with open(get_ths_js("ths.js"), encoding="utf-8") as file:
        js_code.eval(file.read())
    return js_code.call("v")


def load_board_stocks(board_name: str, weak: bool = False) -> pd.DataFrame:
    order = "asc" if weak else "desc"

    def loader():
        board_code = load_board_codes()[board_name]
        v_code = ths_cookie()
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Cookie": f"v={v_code}",
            "hexin-v": v_code,
            "Referer": "http://q.10jqka.com.cn",
        }
        url = f"http://q.10jqka.com.cn/thshy/detail/code/{board_code}/field/199112/order/{order}/page/1/ajax/1/"
        response = requests.get(url, headers=headers, timeout=12)
        response.raise_for_status()
        tables = pd.read_html(StringIO(response.text))
        if not tables:
            return pd.DataFrame()
        df = tables[0]
        df = df.rename(
            columns={
                "代码": "代码",
                "名称": "名称",
                "现价": "最新价",
                "涨跌幅(%)": "涨跌幅",
                "换手(%)": "换手率",
            }
        )
        return normalize_stock_df(df)

    return cached(f"industry_cons_ths:{board_name}:{order}", loader)


def stock_limit_pct(code: str, name: str) -> float:
    if name.startswith(("ST", "*ST")):
        return 5.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("8", "4")):
        return 30.0
    return 10.0


def clamp_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, min_value), max_value)


def enrich_leaders(
    stocks: pd.DataFrame,
    board_name: str,
    board_pct: float,
    limit: int = DEFAULT_LEADERS_PER_BOARD,
    weak: bool = False,
) -> list[dict[str, Any]]:
    if stocks.empty:
        return []

    df = stocks.copy()
    df["limit_pct"] = df.apply(lambda row: stock_limit_pct(row["code"], row["name"]), axis=1)
    df["limit_up"] = df["pct"] >= df["limit_pct"] - 0.25
    df["limit_down"] = df["pct"] <= -df["limit_pct"] + 0.25

    if weak:
        df["score"] = (
            (-df["pct"] * 1000)
            + df["limit_down"].astype(int) * 100000
            + df["amount"].rank(pct=True) * 200
            + df["turnover"].rank(pct=True) * 100
        )
        df = df.sort_values(["score", "pct"], ascending=[False, True])
    else:
        df["score"] = (
            (df["pct"] * 1000)
            + df["limit_up"].astype(int) * 100000
            + df["amount"].rank(pct=True) * 200
            + df["turnover"].rank(pct=True) * 100
        )
        df = df.sort_values(["score", "pct"], ascending=[False, False])

    leaders = []
    for rank, (_, row) in enumerate(df.head(limit).iterrows(), start=1):
        leaders.append(
            {
                "rank": rank,
                "code": row["code"],
                "name": row["name"],
                "board": board_name,
                "board_pct": round(board_pct, 2),
                "pct": round(float(row["pct"]), 2),
                "price": round(float(row["price"]), 2),
                "amount": round(float(row["amount"]), 2),
                "turnover": round(float(row["turnover"]), 2),
                "volume_ratio": round(float(row["volume_ratio"]), 2),
                "limit": bool(row["limit_down"] if weak else row["limit_up"]),
                "score": round(float(row["score"]), 2),
            }
        )
    return leaders


def leader_group(board: pd.Series, leaders: list[dict[str, Any]], rank: int) -> dict[str, Any]:
    return {
        "board": board_to_dict(board, rank=rank),
        "leaders": leaders,
    }


def build_dashboard(
    board_count: int = DEFAULT_BOARD_COUNT,
    leaders_per_board: int = DEFAULT_LEADERS_PER_BOARD,
) -> dict[str, Any]:
    boards = load_boards()
    strongest = boards.head(board_count)
    weakest = boards.tail(board_count).sort_values("pct", ascending=True)

    strong_leader_groups = []
    weak_leader_groups = []

    for rank, (_, board) in enumerate(strongest.iterrows(), start=1):
        stocks = load_board_stocks(board["name"], weak=False)
        leaders = enrich_leaders(
            stocks,
            board["name"],
            board["pct"],
            limit=leaders_per_board,
            weak=False,
        )
        strong_leader_groups.append(leader_group(board, leaders, rank))

    for rank, (_, board) in enumerate(weakest.iterrows(), start=1):
        stocks = load_board_stocks(board["name"], weak=True)
        leaders = enrich_leaders(
            stocks,
            board["name"],
            board["pct"],
            limit=leaders_per_board,
            weak=True,
        )
        weak_leader_groups.append(leader_group(board, leaders, rank))

    return {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "board_count": board_count,
        "leaders_per_board": leaders_per_board,
        "strong_boards": [
            board_to_dict(row, rank=index + 1) for index, (_, row) in enumerate(strongest.iterrows())
        ],
        "weak_boards": [
            board_to_dict(row, rank=index + 1) for index, (_, row) in enumerate(weakest.iterrows())
        ],
        "strong_leader_groups": strong_leader_groups,
        "weak_leader_groups": weak_leader_groups,
        "note": "数据源为同花顺实时行业行情，经 AkShare 和同花顺页面接口获取；龙头按涨跌幅、是否涨跌停、成交额、换手率综合打分。",
    }


def board_to_dict(row: pd.Series, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "name": row["name"],
        "pct": round(float(row["pct"]), 2),
        "amount": round(float(row["amount_yi"]), 2),
        "rise_count": int(row["rise_count"]),
        "fall_count": int(row["fall_count"]),
        "leader": row["leader"],
    }


@app.get("/api/dashboard")
def api_dashboard():
    board_count = clamp_int(request.args.get("board_count"), DEFAULT_BOARD_COUNT, 1, MAX_BOARD_COUNT)
    leaders_per_board = clamp_int(
        request.args.get("leaders_per_board"),
        DEFAULT_LEADERS_PER_BOARD,
        1,
        MAX_LEADERS_PER_BOARD,
    )
    try:
        return jsonify({"ok": True, "data": build_dashboard(board_count, leaders_per_board)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/legacy")
def index():
    return render_template_string(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A股强弱板块实时看板</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #161a22;
      --muted: #657084;
      --red: #d92323;
      --green: #07834f;
      --blue: #2457c5;
      --amber: #a15c00;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 12px;
      color: var(--muted);
      font-size: 14px;
    }
    button, select, input {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      border-radius: 6px;
      height: 34px;
      padding: 0 10px;
      font-size: 14px;
    }
    input {
      width: 64px;
    }
    button {
      cursor: pointer;
      color: var(--blue);
      font-weight: 650;
    }
    main {
      padding: 20px 24px 28px;
      max-width: 1440px;
      margin: 0 auto;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }
    h2 {
      margin: 0;
      font-size: 16px;
      letter-spacing: 0;
    }
    .hint {
      color: var(--muted);
      font-size: 13px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 14px;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid #edf0f4;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      text-align: right;
    }
    th:first-child, td:first-child,
    th:nth-child(2), td:nth-child(2),
    .left {
      text-align: left;
    }
    th {
      color: var(--muted);
      font-weight: 650;
      background: #fbfcfd;
    }
    tr:last-child td { border-bottom: 0; }
    .rank {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 24px;
      height: 24px;
      border-radius: 5px;
      background: #eef2f8;
      color: #344054;
      font-weight: 750;
    }
    .red { color: var(--red); font-weight: 700; }
    .green { color: var(--green); font-weight: 700; }
    .tag {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 42px;
      height: 22px;
      border-radius: 5px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid var(--line);
      color: var(--muted);
      background: #fff;
    }
    .tag.hot { color: var(--red); border-color: #f0b4b4; background: #fff5f5; }
    .tag.cold { color: var(--green); border-color: #a7d9c3; background: #f2fbf6; }
    .status {
      min-height: 28px;
      padding: 8px 0 14px;
      color: var(--muted);
      font-size: 13px;
    }
    .error {
      color: var(--amber);
      font-weight: 650;
    }
    .leader-group {
      border-bottom: 1px solid var(--line);
    }
    .leader-group:last-child {
      border-bottom: 0;
    }
    .leader-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      background: #fbfcfd;
      border-bottom: 1px solid #edf0f4;
      font-size: 13px;
      color: var(--muted);
    }
    .leader-title strong {
      color: var(--text);
      font-size: 14px;
    }
    .leader-empty {
      padding: 14px 12px;
      color: var(--muted);
      font-size: 13px;
    }
    @media (max-width: 980px) {
      header { align-items: flex-start; flex-direction: column; }
      .grid { grid-template-columns: 1fr; }
      main { padding: 14px; }
      th, td { padding: 9px 8px; font-size: 13px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>A股强弱板块实时看板</h1>
    <div class="toolbar">
      <span id="updated">等待刷新</span>
      <label>板块
        <input id="boardCount" type="number" min="1" max="12" value="3">
      </label>
      <label>每板龙头
        <input id="leaderCount" type="number" min="1" max="10" value="3">
      </label>
      <label>刷新
        <select id="interval">
          <option value="15">15秒</option>
          <option value="30" selected>30秒</option>
          <option value="60">60秒</option>
        </select>
      </label>
      <button id="refresh" type="button">立即刷新</button>
    </div>
  </header>
  <main>
    <div id="status" class="status"></div>
    <div class="grid">
      <section>
        <div class="section-head">
          <h2 id="strongBoardsTitle">最强行业板块</h2>
          <span class="hint">按板块实时涨跌幅</span>
        </div>
        <table>
          <thead><tr><th style="width:52px">名</th><th>板块</th><th>涨幅</th><th>成交额(亿)</th><th>涨/跌家</th></tr></thead>
          <tbody id="strongBoards"></tbody>
        </table>
      </section>
      <section>
        <div class="section-head">
          <h2 id="weakBoardsTitle">最弱行业板块</h2>
          <span class="hint">按板块实时涨跌幅</span>
        </div>
        <table>
          <thead><tr><th style="width:52px">名</th><th>板块</th><th>跌幅</th><th>成交额(亿)</th><th>涨/跌家</th></tr></thead>
          <tbody id="weakBoards"></tbody>
        </table>
      </section>
      <section>
        <div class="section-head">
          <h2 id="strongLeadersTitle">强板块龙头股票</h2>
          <span class="hint">每个强板块内分别排序</span>
        </div>
        <div id="strongLeaders"></div>
      </section>
      <section>
        <div class="section-head">
          <h2 id="weakLeadersTitle">弱板块领跌股票</h2>
          <span class="hint">每个弱板块内分别排序</span>
        </div>
        <div id="weakLeaders"></div>
      </section>
    </div>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let timer = null;

    function rankCell(rank) {
      return `<span class="rank">${rank}</span>`;
    }

    function signed(value) {
      const cls = value >= 0 ? "red" : "green";
      const sign = value > 0 ? "+" : "";
      return `<span class="${cls}">${sign}${value.toFixed(2)}%</span>`;
    }

    function renderBoards(id, boards) {
      $(id).innerHTML = boards.map(item => `
        <tr>
          <td>${rankCell(item.rank)}</td>
          <td class="left" title="${item.name}">${item.name}</td>
          <td>${signed(item.pct)}</td>
          <td>${Number(item.amount).toFixed(2)}</td>
          <td>${item.rise_count}/${item.fall_count}</td>
        </tr>
      `).join("");
    }

    function renderLeaders(id, leaders, weak = false) {
      $(id).innerHTML = leaders.map(item => `
        <tr>
          <td>${rankCell(item.rank)}</td>
          <td class="left" title="${item.code} ${item.name}">${item.name}<br><span class="hint">${item.code}</span></td>
          <td class="left" title="${item.board}">${item.board}</td>
          <td>${signed(item.pct)}</td>
          <td>${Number(item.amount).toFixed(2)}</td>
          <td><span class="tag ${item.limit ? (weak ? "cold" : "hot") : ""}">${item.limit ? (weak ? "跌停" : "涨停") : "活跃"}</span></td>
        </tr>
      `).join("");
    }

    function readBoundedInt(id, fallback, min, max) {
      const input = $(id);
      const parsed = Number.parseInt(input.value, 10);
      const value = Number.isFinite(parsed) ? Math.min(Math.max(parsed, min), max) : fallback;
      input.value = value;
      return value;
    }

    function renderLeaderGroups(id, groups, weak = false) {
      $(id).innerHTML = groups.map(group => {
        const board = group.board;
        const rows = group.leaders.length ? group.leaders.map(item => `
          <tr>
            <td>${rankCell(item.rank)}</td>
            <td class="left" title="${item.code} ${item.name}">${item.name}<br><span class="hint">${item.code}</span></td>
            <td>${signed(item.pct)}</td>
            <td>${Number(item.amount).toFixed(2)}</td>
            <td>${Number(item.turnover).toFixed(2)}%</td>
            <td><span class="tag ${item.limit ? (weak ? "cold" : "hot") : ""}">${item.limit ? (weak ? "跌停" : "涨停") : "活跃"}</span></td>
          </tr>
        `).join("") : `<tr><td colspan="6" class="leader-empty">暂无成分股数据</td></tr>`;

        return `
          <div class="leader-group">
            <div class="leader-title">
              <strong>${board.rank}. ${board.name}</strong>
              <span>${signed(board.pct)} · 成交额 ${Number(board.amount).toFixed(2)} 亿 · 涨/跌 ${board.rise_count}/${board.fall_count}</span>
            </div>
            <table>
              <thead><tr><th style="width:52px">名</th><th>股票</th><th>${weak ? "跌幅" : "涨幅"}</th><th>成交额(亿)</th><th>换手</th><th>状态</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
        `;
      }).join("");
    }

    async function refresh() {
      $("status").textContent = "正在刷新行情...";
      try {
        const boardCount = readBoundedInt("boardCount", 3, 1, 12);
        const leaderCount = readBoundedInt("leaderCount", 3, 1, 10);
        const params = new URLSearchParams({
          board_count: boardCount,
          leaders_per_board: leaderCount,
          ts: Date.now()
        });
        const resp = await fetch("/api/dashboard?" + params.toString());
        const payload = await resp.json();
        if (!payload.ok) throw new Error(payload.error || "刷新失败");
        const data = payload.data;
        $("strongBoardsTitle").textContent = `最强 ${data.board_count} 个行业板块`;
        $("weakBoardsTitle").textContent = `最弱 ${data.board_count} 个行业板块`;
        $("strongLeadersTitle").textContent = `强板块龙头股票 · 每板 ${data.leaders_per_board} 个`;
        $("weakLeadersTitle").textContent = `弱板块领跌股票 · 每板 ${data.leaders_per_board} 个`;
        renderBoards("strongBoards", data.strong_boards);
        renderBoards("weakBoards", data.weak_boards);
        renderLeaderGroups("strongLeaders", data.strong_leader_groups, false);
        renderLeaderGroups("weakLeaders", data.weak_leader_groups, true);
        $("updated").textContent = "更新时间 " + data.updated_at;
        $("status").textContent = data.note;
      } catch (err) {
        $("status").innerHTML = `<span class="error">${err.message}</span>`;
      }
    }

    function schedule() {
      if (timer) clearInterval(timer);
      timer = setInterval(refresh, Number($("interval").value) * 1000);
    }

    $("refresh").addEventListener("click", refresh);
    $("interval").addEventListener("change", schedule);
    $("boardCount").addEventListener("change", refresh);
    $("leaderCount").addEventListener("change", refresh);
    schedule();
    refresh();
  </script>
</body>
</html>
        """
    )


from terminal_api import register_terminal

register_terminal(app, load_boards, load_board_stocks)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
