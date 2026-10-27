#!/usr/bin/env python3
"""
みんなのIPO株: data.json を自動で更新するプログラム

やること(順番):
  1. 今の data.json を読み込む
  2. Claude(AI)に、Web検索で最新のIPO情報を調べてもらう
  3. AIの答えを、厳しくチェックする(日付・価格・形式・危険な文字など)
  4. チェックに通ったものだけを、data.json に反映する

大事な約束:
  - APIキーは、環境変数 ANTHROPIC_API_KEY だけから読みます。ファイルには書きません。
  - チェックに通らなかった場合は、data.json を変更せず、理由を表示して終了します。
  - AIには「わからない項目は null にする」「推測しない」と指示しています。

使い方:
  python scripts/update_data.py             # 調べて、チェックして、data.json を更新
  python scripts/update_data.py --dry-run   # 調べて、チェックして、結果だけ表示(保存しない)
  python scripts/update_data.py --from-file answer.json   # AIの答えを手元のファイルから読む(テスト用)
"""
import argparse
import copy
import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data.json"

MODEL = os.environ.get("IPO_MODEL", "claude-sonnet-5-5")
SEARCH_TOOL = os.environ.get("IPO_SEARCH_TOOL", "web_search_20250305")
MAX_SEARCHES = int(os.environ.get("IPO_MAX_SEARCHES", "30"))
MAX_COMPANIES = 30
JST = dt.timezone(dt.timedelta(hours=9))

CODE_RE = re.compile(r"^(\d{3}[A-Z]|\d{4})$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------ 読み書き
def load_data():
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    tmp = DATA_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(DATA_FILE)


def today_jst():
    return dt.datetime.now(JST).date()


def parse_date(s):
    return dt.date.fromisoformat(s)


# ------------------------------------------------------------------ AIへの依頼
SCHEMA_HELP = """
返す JSON の形(この形の JSON だけを返してください。前後に説明文を付けないでください):
{
  "companies": [          // 今日以降に上場する予定の企業(株式のIPOのみ。ETF・債券・REITは除く)
    {
      "code": "652A",                      // 証券コード(例 652A や 9334)
      "name": "会社名",
      "group": "g",                        // 市場: g=グロース, s=スタンダード, n=名証ネクスト(福証Qボード等も n)
      "sector": "業種の短い説明(例: IT(クラウド受付システム))",
      "about": "何をする会社か(1〜2文。目論見書やJPXの資料の事業内容にもとづく)",
      "listing": "2026-11-10",              // 上場予定日
      "bb": ["2026-10-23", "2026-10-29"],   // ブックビルディング期間(申込期間)の開始日・終了日
      "bbNote": null,                       // 期間の呼び名が違う場合のみ(例 "抽選申込期間")
      "priceDate": null,                    // 公開価格の決定日(わかる場合のみ)
      "lo": 820, "hi": 850,                 // 仮条件の下限・上限(円)。未発表なら null
      "price": null,                        // 公開価格(円)。決定済みの場合のみ
      "assumed": null,                      // 想定価格(円)。仮条件が未発表のときのみ
      "uw": {"lead": "SBI証券", "others": [["SMBC日興証券", "共同主幹事"]]},   // 主幹事と、わかる範囲の幹事
      "pub": 50000, "sec": 1416200, "oa": 219900,   // 公募株数・売出株数・オーバーアロットメント株数。不明は null
      "secNote": null,                      // 例 "売出株数にオーバーアロットメントを含む"
      "lockup": null,                       // ロックアップの内容(わかる場合のみ)
      "founded": "2016年1月", "hq": "東京都目黒区",   // 不明は null
      "fin": null,                          // 直近期の数字がわかる場合のみ {"label":"2025年9月期","sales":1336,"ni":9}(百万円)
      "links": [["JPXの新規上場会社の概要(PDF)", "https://..."]]   // 根拠にした公式ページ(https のみ、最大4件)
    }
  ],
  "new_past": [           // 前回の data.json に無い、最近上場した企業のうち、公開価格と初値の両方を確認できたもの
    {"name": "会社名", "code": "648A", "group": "g", "date": "2026-10-15", "price": 1480, "open": 2030, "lead": "野村證券"}
  ],
  "notes": "確認できなかった点や、情報源どうしで食い違った点を、短く書く"
}
"""

RULES = """
あなたは、日本のIPO(新規上場)情報を調べる、慎重なリサーチ担当です。
次のルールを必ず守ってください。
- Web検索で見つけた情報だけを使う。見つからない項目、確認できない項目は null にする。推測や補完はしない。
- 数字は、情報源に書かれているとおりに書く。計算し直したり、丸めたりしない。
- 情報源どうしで食い違う場合は、JPX(日本取引所グループ)の資料と、目論見書、主幹事証券会社の発表を優先する。食い違いは notes に書く。
- 「買い」「おすすめ」などの評価や、値上がりの予想は書かない。
- 文章は、事実だけを短く書く。HTMLタグや記号 < > は使わない。
- links には、実際に見つけた公式ページのURLだけを入れる。URLを作らない。
"""


def build_prompt(data, today):
    current = {
        "companies": data.get("companies", []),
        "past_codes": [p["code"] for p in data.get("past", [])],
    }
    return (
        f"今日の日付: {today.isoformat()}(日本時間)\n\n{RULES}\n"
        "やってほしいこと:\n"
        "1. 今日以降に上場予定のIPO(株式)を、すべて調べて companies に入れてください(最大20社)。"
        "すでに下の「現在のデータ」にある企業は、最新の情報に直してください。\n"
        "2. 現在のデータの past_codes に無い企業で、最近上場して初値がついたものがあれば、new_past に入れてください。"
        "公開価格と初値の両方を確認できたものだけにしてください。\n"
        "3. JPXの新規上場の一覧(カレンダー)、各社の目論見書やJPXの概要資料、主幹事証券会社の発表を、まず確認してください。\n\n"
        f"{SCHEMA_HELP}\n"
        "現在のデータ(参考):\n" + json.dumps(current, ensure_ascii=False)
    )


def ask_claude(prompt):
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic が入っていません。 pip install -r scripts/requirements.txt を実行してください。")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("環境変数 ANTHROPIC_API_KEY が設定されていません。(APIキーはファイルに書かず、環境変数で渡します)")
    client = anthropic.Anthropic()
    tools = [{"type": SEARCH_TOOL, "name": "web_search", "max_uses": MAX_SEARCHES}]
    messages = [{"role": "user", "content": prompt}]
    for _ in range(6):  # 長い調べものが途中で止まったときは、続きをお願いする
        with client.messages.stream(model=MODEL, max_tokens=16000, tools=tools, messages=messages) as stream:
            resp = stream.get_final_message()
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text


def extract_json(text):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    if not raw:
        raise ValueError("AIの答えに JSON が見つかりませんでした。")
    return json.loads(raw)


# ------------------------------------------------------------------ チェック
class Bad(Exception):
    pass


def clean_text(v, where, maxlen=300, required=False):
    """AIが書いた文字を、HTMLとして安全な形にする(サイト側は innerHTML で表示するため)。"""
    if v is None or v == "":
        if required:
            raise Bad(f"{where}: 空です")
        return None
    if not isinstance(v, str):
        raise Bad(f"{where}: 文字ではありません")
    v = re.sub(r"[\x00-\x1f]", " ", v).strip()
    if len(v) > maxlen:
        raise Bad(f"{where}: 長すぎます({len(v)}文字)")
    return html.escape(v, quote=True)


def num(v, where, lo=None, hi=None, integer=False, allow_none=True):
    if v is None:
        if allow_none:
            return None
        raise Bad(f"{where}: 必須です")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise Bad(f"{where}: 数字ではありません({v!r})")
    if integer and float(v) != int(v):
        raise Bad(f"{where}: 整数ではありません({v!r})")
    if lo is not None and v < lo or hi is not None and v > hi:
        raise Bad(f"{where}: 範囲外です({v!r})")
    return int(v) if integer else v


def date_str(v, where, allow_none=False):
    if v is None and allow_none:
        return None
    if not isinstance(v, str) or not DATE_RE.match(v):
        raise Bad(f"{where}: 日付の形式が違います({v!r})")
    try:
        parse_date(v)
    except ValueError:
        raise Bad(f"{where}: 存在しない日付です({v!r})")
    return v


def check_url(u, where):
    if not isinstance(u, str) or not re.match(r"^https://[A-Za-z0-9.\-]+(/[^\s<>\"']*)?$", u):
        raise Bad(f"{where}: https のURLではありません({u!r})")
    return html.escape(u, quote=True)


def check_company(c, markets, today, i):
    w = f"companies[{i}]"
    if not isinstance(c, dict):
        raise Bad(f"{w}: 形が違います")
    code = c.get("code")
    if not isinstance(code, str) or not CODE_RE.match(code):
        raise Bad(f"{w}.code: 証券コードの形式が違います({code!r})")
    w = f"{code}"
    out = {"code": code}
    out["name"] = clean_text(c.get("name"), f"{w}.name", 80, required=True)
    if c.get("group") not in markets:
        raise Bad(f"{w}.group: g / s / n のどれかにしてください({c.get('group')!r})")
    out["group"] = c["group"]
    out["sector"] = clean_text(c.get("sector"), f"{w}.sector", 80)
    out["about"] = clean_text(c.get("about"), f"{w}.about", 300)
    out["listing"] = date_str(c.get("listing"), f"{w}.listing")
    if parse_date(out["listing"]) < today:
        raise Bad(f"{w}.listing: すでに過ぎた日付です({out['listing']})")
    bb = c.get("bb")
    if not (isinstance(bb, list) and len(bb) == 2):
        raise Bad(f"{w}.bb: 開始日と終了日の2つが必要です")
    out["bb"] = [date_str(bb[0], f"{w}.bb[0]"), date_str(bb[1], f"{w}.bb[1]")]
    if not (out["bb"][0] <= out["bb"][1] <= out["listing"]):
        raise Bad(f"{w}: 日付の順番がおかしいです(bb={out['bb']}, listing={out['listing']})")
    if (parse_date(out["listing"]) - parse_date(out["bb"][0])).days > 120:
        raise Bad(f"{w}: ブックビルディングから上場まで120日以上あります")
    out["bbNote"] = clean_text(c.get("bbNote"), f"{w}.bbNote", 20)
    out["priceDate"] = date_str(c.get("priceDate"), f"{w}.priceDate", allow_none=True)
    if out["priceDate"] and not (out["bb"][0] <= out["priceDate"] <= out["listing"]):
        raise Bad(f"{w}.priceDate: 日付の順番がおかしいです")
    for k in ("lo", "hi", "price"):
        out[k] = num(c.get(k), f"{w}.{k}", 1, 10_000_000)
    out["assumed"] = num(c.get("assumed"), f"{w}.assumed", 1, 10_000_000)
    if out["lo"] and out["hi"] and out["lo"] > out["hi"]:
        raise Bad(f"{w}: 仮条件の下限が上限より大きいです")
    if out["price"] and out["lo"] and out["hi"] and not (out["lo"] <= out["price"] <= out["hi"]):
        raise Bad(f"{w}: 公開価格が仮条件の範囲外です(別の情報源の誤りの可能性)")
    if not (out["price"] or out["hi"] or out["assumed"]):
        raise Bad(f"{w}: 価格の情報(公開価格・仮条件・想定価格)がありません")
    uw = c.get("uw")
    if not isinstance(uw, dict):
        raise Bad(f"{w}.uw: 主幹事の情報がありません")
    others = []
    for j, o in enumerate(uw.get("others") or []):
        if not (isinstance(o, list) and len(o) == 2):
            raise Bad(f"{w}.uw.others[{j}]: [会社名, 役割] の形にしてください")
        others.append([clean_text(o[0], f"{w}.uw.others[{j}][0]", 40, True), clean_text(o[1], f"{w}.uw.others[{j}][1]", 20, True)])
    out["uw"] = {"lead": clean_text(uw.get("lead"), f"{w}.uw.lead", 40, True), "others": others}
    for k in ("pub", "sec", "oa"):
        out[k] = num(c.get(k), f"{w}.{k}", 0, 10_000_000_000, integer=True)
    out["secNote"] = clean_text(c.get("secNote"), f"{w}.secNote", 80)
    out["lockup"] = clean_text(c.get("lockup"), f"{w}.lockup", 200)
    out["founded"] = clean_text(c.get("founded"), f"{w}.founded", 30)
    out["hq"] = clean_text(c.get("hq"), f"{w}.hq", 60)
    fin = c.get("fin")
    if fin:
        out["fin"] = {"label": clean_text(fin.get("label"), f"{w}.fin.label", 30, True),
                      "sales": num(fin.get("sales"), f"{w}.fin.sales", -1e9, 1e9, allow_none=False),
                      "ni": num(fin.get("ni"), f"{w}.fin.ni", -1e9, 1e9, allow_none=False)}
    else:
        out["fin"] = None
    links = []
    for j, l in enumerate((c.get("links") or [])[:4]):
        if not (isinstance(l, list) and len(l) == 2):
            raise Bad(f"{w}.links[{j}]: [名前, URL] の形にしてください")
        links.append([clean_text(l[0], f"{w}.links[{j}][0]", 60, True), check_url(l[1], f"{w}.links[{j}][1]")])
    out["links"] = links
    return out


def check_past(p, markets, today, i):
    w = f"new_past[{i}]"
    if not isinstance(p, dict):
        raise Bad(f"{w}: 形が違います")
    code = p.get("code")
    if not isinstance(code, str) or not CODE_RE.match(code):
        raise Bad(f"{w}.code: 証券コードの形式が違います({code!r})")
    w = f"過去 {code}"
    if p.get("group") not in markets:
        raise Bad(f"{w}.group: g / s / n のどれかにしてください")
    d = date_str(p.get("date"), f"{w}.date")
    if parse_date(d) > today:
        raise Bad(f"{w}.date: 未来の日付です")
    price = num(p.get("price"), f"{w}.price", 1, 10_000_000, allow_none=False)
    opn = num(p.get("open"), f"{w}.open", 1, 10_000_000, allow_none=False)
    ret = (opn / price - 1) * 100
    if not (-60 <= ret <= 1500):
        raise Bad(f"{w}: 初値の騰落率が{ret:.0f}%で、通常の範囲外です(数字の誤りの可能性)")
    return {"name": clean_text(p.get("name"), f"{w}.name", 80, True), "code": code, "group": p["group"], "date": d,
            "price": int(price), "open": int(opn), "lead": clean_text(p.get("lead"), f"{w}.lead", 40, True)}


# ------------------------------------------------------------------ 反映
def merge(data, answer, today):
    markets = data["markets"]
    report, new = [], copy.deepcopy(data)
    if not isinstance(answer, dict):
        raise Bad("AIの答えが、JSONの形(オブジェクト)ではありません")

    # 過去の実績
    known_past = {p["code"] for p in new["past"]}
    added = []
    for i, p in enumerate(answer.get("new_past") or []):
        item = check_past(p, markets, today, i)
        if item["code"] in known_past:
            continue
        new["past"].append(item)
        known_past.add(item["code"])
        added.append(item)
        report.append(f"過去の実績を追加: {item['name']}({item['code']}) 公開価格{item['price']}円 → 初値{item['open']}円")

    # 上場予定
    old_by_code = {c["code"]: c for c in data["companies"]}
    got = answer.get("companies")
    if not isinstance(got, list):
        raise Bad("companies が配列ではありません")
    if len(got) > MAX_COMPANIES:
        raise Bad(f"companies が多すぎます({len(got)}社)")
    checked, seen = [], set()
    for i, c in enumerate(got):
        ld = c.get("listing") if isinstance(c, dict) else None
        if isinstance(ld, str) and DATE_RE.match(ld) and ld < today.isoformat():
            report.append(f"すでに上場日を過ぎているため、予定には入れません: {c.get('name')}({c.get('code')})")
            continue
        item = check_company(c, markets, today, i)
        if item["code"] in seen:
            raise Bad(f"{item['code']}: 同じ企業が2回入っています")
        seen.add(item["code"])
        if item["code"] in known_past:
            report.append(f"上場済みのため、予定から外します: {item['name']}({item['code']})")
            continue
        checked.append(item)

    still_valid_old = [c for c in data["companies"] if parse_date(c["listing"]) >= today and c["code"] not in known_past]
    if len(still_valid_old) >= 3 and len(checked) < 0.4 * len(still_valid_old):
        raise Bad(f"企業数が急に減りました({len(still_valid_old)}社 → {len(checked)}社)。調べ方に失敗した可能性があるため、反映しません。")

    used_ids = {c["id"] for c in old_by_code.values()}
    result = []
    for item in checked:
        old = old_by_code.get(item["code"])
        if old:
            merged = copy.deepcopy(old)
            for k, v in item.items():
                if v is None and k in old and old[k] is not None and k in ("about", "sector", "founded", "hq", "lockup", "fin", "pub", "sec", "oa", "secNote"):
                    continue  # 今回わからなかった項目は、前回の値を残す
                merged[k] = v
            if not item["links"]:
                merged["links"] = old.get("links", [])
            for k in ("price", "lo", "hi", "assumed", "priceDate", "bbNote"):
                merged[k] = item[k]  # 価格まわりは、今回の答えを優先(決まった/変わった情報を反映)
            diffs = [k for k in ("listing", "bb", "lo", "hi", "price", "priceDate", "assumed") if old.get(k) != merged.get(k)]
            if diffs:
                report.append(f"更新: {merged['name']}({merged['code']}) 変わった項目: {', '.join(diffs)}")
            merged.setdefault("id", old["id"])
        else:
            merged = item
            base = "c" + item["code"].lower()
            merged["id"] = base if base not in used_ids else base + "x"
            merged["art"] = "deeptech" if re.search(r"バイオ|医薬|創薬", (item.get("sector") or "") + (item.get("about") or "")) else "it"
            used_ids.add(merged["id"])
            report.append(f"追加: {merged['name']}({merged['code']}) 上場予定日 {merged['listing']}")
        merged.setdefault("art", "it")
        result.append(merged)

    for c in data["companies"]:
        if c["code"] not in seen:
            if c["code"] in known_past:
                continue
            if parse_date(c["listing"]) < today:
                report.append(f"予定から外す(上場日を過ぎました): {c['name']}({c['code']})。初値が確認でき次第、過去の実績に入ります。")
            else:
                report.append(f"今回の調査に出てきませんでした(予定から外します): {c['name']}({c['code']}) 要確認")
    result.sort(key=lambda c: (c["listing"], c["code"]))
    new["companies"] = result
    notes = answer.get("notes")
    if notes:
        report.append("AIのメモ: " + str(notes)[:500])
    def norm(cs):  # null の項目は「無い」のと同じとして比べる
        return json.dumps([{k: v for k, v in c.items() if v is not None} for c in cs], sort_keys=True, ensure_ascii=False)
    changed = norm(new["companies"]) != norm(data["companies"]) or bool(added)
    if changed:
        new["updated"] = today.isoformat()
    return new, report, changed


# ------------------------------------------------------------------ 実行
def main():
    ap = argparse.ArgumentParser(description="data.json を自動で更新します")
    ap.add_argument("--dry-run", action="store_true", help="保存せず、結果だけ表示する")
    ap.add_argument("--from-file", help="AIの答え(JSON)を、このファイルから読む(テスト用)")
    args = ap.parse_args()

    today = today_jst()
    data = load_data()
    if args.from_file:
        answer = json.load(open(args.from_file, encoding="utf-8"))
    else:
        text = ask_claude(build_prompt(data, today))
        try:
            answer = extract_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            print("AIの答えを読み取れませんでした:", e)
            print(text[:1500])
            sys.exit(1)
    try:
        new, report, changed = merge(data, answer, today)
    except Bad as e:
        print("チェックに通らなかったため、data.json は変更しません。")
        print("理由:", e)
        sys.exit(1)

    lines = report or ["変更はありませんでした。"]
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## IPOデータの更新結果\n" + "\n".join("- " + l for l in lines) + "\n")
    if args.dry_run:
        print("(--dry-run のため、保存していません)")
        return
    if changed:
        save_data(new)
        print("data.json を更新しました。")
    else:
        print("data.json は変わりませんでした。")


if __name__ == "__main__":
    main()
