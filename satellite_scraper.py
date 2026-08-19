#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
satellite_scraper.py
=====================

MGP Pro Discovery API と Copernicus Data Space Ecosystem OData API を、
中国MSA管轄相当のAOI（data/china_msa_aoi.geojson）に対して検索し、検出した
撮影フットプリントを state.json に累積保存、satellite_imagery.geojson を
再構築する。GitHub Actionsから定期的に呼ばれる想定（--onceで単発実行）。

方針（msa_scraperと同じ思想）:
  - state.json は "source:id" をキーにした独立レコードの集合。
  - 既知のキーはスキップ（重複検索してもコストがかからないよう、検索範囲は
    LOOKBACK_DAYS日分のみに絞り、新着だけを効率よく拾う設計）。
  - 間引きはしない（全件累積）。リポジトリ肥大化は将来の検討事項。

認証まわり:
  - MGP Pro: 環境変数 MGP_API_KEY が必要（未設定ならMGP検索はスキップ）。
  - Copernicus: 検索は認証不要。ダウンロードは外部リンク方式（Copernicus
    Browser側で都度ログイン）としているため、このスクレイパーは認証情報を
    一切必要としない。

使い方:
  python satellite_scraper.py --once
  python satellite_scraper.py --once --out-dir satellite_out --lookback-days 3
  python satellite_scraper.py --once --lookback-days 365 --chunk-days 3 --mgp-chunk-days 3   # 初回1年分バックフィル
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from providers import mgp, copernicus
import geo_utils

BASE_DIR = Path(__file__).parent
AOI_PATH = BASE_DIR / "data" / "china_msa_aoi.geojson"


def load_aoi():
    gj = json.loads(AOI_PATH.read_text(encoding="utf-8"))
    ring = gj["features"][0]["geometry"]["coordinates"][0]
    bbox = geo_utils.ring_bbox(ring)  # (w, s, e, n)
    return list(bbox), ring


def load_state(path):
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def item_key(it):
    return f"{it['source']}:{it['id']}"


def classify_type(sensor):
    return "SAR" if sensor == "Sentinel-1" else "optical"


def to_state_record(raw, first_seen_iso):
    return {
        "id": raw["id"],
        "source": raw["source"],
        "sensor": raw.get("sensor"),
        "type": classify_type(raw.get("sensor")),
        "datetime": raw.get("datetime"),
        "cloud_cover": raw.get("cloud_cover"),
        "name": raw.get("name"),
        "footprint": raw.get("footprint"),
        "thumbnail_url": raw.get("thumbnail_url"),
        "metadata_url": raw.get("metadata_url"),
        "download_url": raw.get("download_url"),
        "links": raw.get("links", []),
        "first_seen": first_seen_iso,
    }


def build_geojson(state):
    features = []
    for it in state.values():
        if not it.get("footprint"):
            continue
        features.append({
            "type": "Feature",
            "geometry": it["footprint"],
            "properties": {
                "id": it["id"],
                "source": it["source"],
                "sensor": it.get("sensor"),
                "type": it.get("type"),
                "datetime": it.get("datetime"),
                "cloud_cover": it.get("cloud_cover"),
                "name": it.get("name"),
                "thumbnail_url": it.get("thumbnail_url"),
                "metadata_url": it.get("metadata_url"),
                "download_url": it.get("download_url"),
                "links": it.get("links", []),
                "first_seen": it.get("first_seen"),
            },
        })
    return {
        "type": "FeatureCollection",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(features),
        "features": features,
    }


def date_chunks(start_dt, end_dt, chunk_days):
    """[start_dt, end_dt) を chunk_days 日ずつのISO文字列ペアに分割して返す。
    OData/Discovery APIとも1回のクエリで返る件数に上限があるため、長期間
    （1年分バックフィル等）を1クエリで投げると新しい方から上限件数だけしか
    取れず、大半を取りこぼす。日付を分割して繰り返し取得することで対応する。"""
    cur = start_dt
    step = timedelta(days=chunk_days)
    while cur < end_dt:
        nxt = min(cur + step, end_dt)
        yield cur.strftime("%Y-%m-%dT%H:%M:%S.000Z"), nxt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        cur = nxt


def run_once(out_dir="satellite_out", lookback_days=3, chunk_days=3, mgp_chunk_days=3, request_delay=0.3):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "state.json"
    geojson_path = out_dir / "satellite_imagery.geojson"

    state = load_state(state_path)
    bbox, ring = load_aoi()

    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=lookback_days)
    now_iso = now.isoformat()

    # lookback_days が短い（通常運用の差分ポーリング）場合は分割せず1回で済ませる。
    # 長い（初回バックフィル等）場合のみ chunk_days ごとに分割する。
    # MGPはMaxarの新規撮影量が非常に多く（1日3.8万km²超、しかもAOIより広い
    # bboxで検索している）、Copernicusと同じ15日幅だとページ上限（5ページ
    # ×100件=500件）に収まりきらず、日付順ソートしていても後半の日付が
    # 欠落し得る。そのためMGPだけより細かい日数（既定5日）で分割する。
    effective_chunk_days = chunk_days if lookback_days > chunk_days else lookback_days + 1
    effective_mgp_chunk_days = mgp_chunk_days if lookback_days > mgp_chunk_days else lookback_days + 1
    chunks = list(date_chunks(start_dt, now, effective_chunk_days))
    mgp_chunks = list(date_chunks(start_dt, now, effective_mgp_chunk_days))
    print(f"検索範囲: 過去{lookback_days}日分 / Copernicus{len(chunks)}回（約{effective_chunk_days}日単位） / MGP{len(mgp_chunks)}回（約{effective_mgp_chunk_days}日単位）", file=sys.stderr)

    new_count = 0
    mgp_key = os.environ.get("MGP_API_KEY", "").strip()

    # SENTINEL-6は海面高度計（ナディア直下の細い軌道線データ）で「撮影
    # フットプリント」のモデルに合わないため対象外。SENTINEL-3（低解像度の
    # 海色・地表温度観測）・SENTINEL-5P（大気観測、画像ではない）はユーザー
    # 判断で対象外にしている。
    TARGET_COLLECTIONS = ["SENTINEL-1", "SENTINEL-2"]

    # レガシー機（運用終了済み・古いアーカイブ専用）はユーザー判断で対象外。
    EXCLUDED_MGP_SENSORS = {"WV04", "QB02", "IK02"}

    # ---------------- MGP Pro（独自のチャンク幅 × 地理グリッドで回す） ----------------
    # 日付だけでなく地理的にも分割する。中国全土という非常に広いbboxのままだと、
    # 日付を細かく刻んでもなお1回のクエリの該当件数がページ上限を超えることが
    # あり得るため（実例: 2026-08-12 海壇島(福建省沿岸)のLG01画像が欠落）。
    MGP_GRID_COLS, MGP_GRID_ROWS = 3, 3
    mgp_cells = geo_utils.split_bbox_grid(bbox, MGP_GRID_COLS, MGP_GRID_ROWS)

    if mgp_key:
        for chunk_start, chunk_end in mgp_chunks:
            for cell_bbox in mgp_cells:
                try:
                    raws = mgp.search(cell_bbox, chunk_start, chunk_end, [], mgp_key, limit=100, max_pages=10)
                    kept = 0
                    for raw in raws:
                        if raw.get("sensor") in EXCLUDED_MGP_SENSORS:
                            continue
                        fp = raw.get("footprint")
                        if fp and fp.get("type") == "Polygon":
                            lon, lat = geo_utils.polygon_centroid(fp["coordinates"][0])
                            if not geo_utils.point_in_ring(lon, lat, ring):
                                continue  # AOIポリゴンの外（bbox検索の粗さを後段で補正）
                        rec = to_state_record(raw, now_iso)
                        key = item_key(rec)
                        if key not in state:
                            state[key] = rec
                            new_count += 1
                            kept += 1
                    if raws:
                        print(f"[MGP] {chunk_start[:10]}〜{chunk_end[:10]} cell{[round(v,1) for v in cell_bbox]}: 取得{len(raws)}件 / 新規{kept}件", file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    print(f"[MGP] {chunk_start[:10]}〜{chunk_end[:10]} cell{[round(v,1) for v in cell_bbox]}: エラー: {e}", file=sys.stderr)
                time.sleep(request_delay)
    else:
        print("[MGP] MGP_API_KEY未設定のためスキップ", file=sys.stderr)

    # ---------------- Copernicus ----------------
    for chunk_start, chunk_end in chunks:
        for collection in TARGET_COLLECTIONS:
            try:
                # top=1000, max_pages=10: 最大10,000件（Copernicus OData の $skip 上限）まで
                # 追従する。以前は top=100 かつ max_pages未指定（既定5）で実質500件キャップに
                # なっており、広域AOI・複数日チャンクでは黙って取りこぼしていた
                # （MGP側で発生したのと同じ種類のページ上限問題）。1回のリクエストの
                # 件数を増やす方向にしたのは、Copernicus側のレート制限閾値が未確認のため、
                # リクエスト回数自体を極力増やしたくないという判断。
                raws = copernicus.search(bbox, chunk_start, chunk_end, [collection], top=1000, aoi_ring=ring, max_pages=10)
                kept = 0
                for raw in raws:
                    if raw.get("sensor") == "-":
                        continue  # 想定外のプロダクト（念のための保険）
                    rec = to_state_record(raw, now_iso)
                    key = item_key(rec)
                    if key not in state:
                        state[key] = rec
                        new_count += 1
                        kept += 1
                print(f"[Copernicus:{collection}] {chunk_start[:10]}〜{chunk_end[:10]}: 取得{len(raws)}件 / 新規{kept}件", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"[Copernicus:{collection}] {chunk_start[:10]}〜{chunk_end[:10]}: エラー: {e}", file=sys.stderr)
            time.sleep(request_delay)

    save_state(state_path, state)
    geojson = build_geojson(state)
    with open(geojson_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)

    print(f"完了: 新規{new_count}件 / 累積{len(state)}件 -> {geojson_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="1回だけ実行して終了（現状これ以外のモードはない）")
    ap.add_argument("--out-dir", default="satellite_out")
    ap.add_argument("--lookback-days", type=int, default=int(os.environ.get("LOOKBACK_DAYS", "3")))
    ap.add_argument("--chunk-days", type=int, default=int(os.environ.get("CHUNK_DAYS", "3")),
                     help="この日数を超えるlookback-daysの場合、この日数単位に分割してAPIを叩く（APIの返却件数上限による取りこぼし防止）。Copernicus用"
                          "（MGP用のmgp-chunk-daysと同じ既定値3に統一）")
    ap.add_argument("--mgp-chunk-days", type=int, default=int(os.environ.get("MGP_CHUNK_DAYS", "3")),
                     help="MGP用のチャンク幅。MGPはCopernicusより新規撮影量が多いため、既定でCopernicusより細かく分割する"
                          "（1セル・1チャンクあたりの上限は limit=100×max_pages=10=1000件のため、"
                          "チャンクを細かくするほど1チャンクあたりの想定件数が下がり、上限超過による"
                          "取りこぼしリスクがさらに下がる）")
    args = ap.parse_args()
    run_once(args.out_dir, args.lookback_days, args.chunk_days, args.mgp_chunk_days)


if __name__ == "__main__":
    main()
