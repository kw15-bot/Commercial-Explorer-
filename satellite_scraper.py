#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
satellite_scraper.py
=====================

MGP Pro Discovery API と Copernicus Data Space Ecosystem OData API を、
中国MSA管轄相当のAOI（data/china_msa_aoi.geojson）に対して検索し、検出した
撮影フットプリントを月×ソース単位のシャードファイル（satellite_out/shards/
{YYYY-MM}_{copernicus|mgp}.json）に累積保存し、対応するgeojsonを再構築する。
GitHub Actionsから定期的に呼ばれる想定（--onceで単発実行）。

方針（msa_scraperと同じ思想。2026-08にシャーディング対応で改訂）:
  - 各シャードは "source:id" をキーにした独立レコードの集合。
  - 既知のキーはスキップ（重複検索してもコストがかからないよう、検索範囲は
    LOOKBACK_DAYS日分のみに絞り、新着だけを効率よく拾う設計）。
  - 間引きはしない（全件累積）。
  - 旧設計（satellite_out/state.json 1本にすべて累積）は、1000日バックフィルで
    state.json/satellite_imagery.geojsonが3GB超まで肥大化し、GitHubの1ファイル
    100MB制限でpushが恒久的に失敗する事態を招いた。月×ソース単位に分割することで
    各ファイルを100MB未満に抑える（実データからの試算では最大でも約70MB程度）。
  - 大規模バックフィル時にpushが1回でも失敗すると、それまでの数時間分の収集結果が
    まるごと失われる問題もあった（GitHub Actionsランナーはジョブ終了時に破棄され、
    push未成功のデータはどこにも残らない）。これに対応するため、run_once()は
    一定間隔でシャードファイルをコミット・pushする「チェックポイント」機能を持つ
    （commit_state.pyのcommit_and_push_shards()をコールバックとして呼ぶ）。

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
SHARDS_DIR_NAME = "shards"


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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# シャーディング（月×ソース単位のファイル分割）
# ---------------------------------------------------------------------------
SOURCE_SLUG = {"Copernicus": "copernicus", "MGP": "mgp"}


def shard_key(source, datetime_iso):
    """レコードの撮像日時とソースから 'YYYY-MM_copernicus' 形式のシャードキーを
    算出する。datetime_isoが欠落/不正な場合はデータを失わないよう
    'unknown_<source>' に振り分ける（本来は起こらない想定だが保険）。"""
    slug = SOURCE_SLUG.get(source, source.lower())
    if not datetime_iso or len(datetime_iso) < 7:
        return f"unknown_{slug}"
    return f"{datetime_iso[:7]}_{slug}"


def shard_state_path(out_dir, key):
    return Path(out_dir) / SHARDS_DIR_NAME / f"{key}.json"


def shard_geojson_path(out_dir, key):
    return Path(out_dir) / SHARDS_DIR_NAME / f"{key}.geojson"


def load_shard(out_dir, key):
    return load_state(shard_state_path(out_dir, key))


def save_shard(out_dir, key, state):
    save_state(shard_state_path(out_dir, key), state)
    geojson = build_geojson(state)
    path = shard_geojson_path(out_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)


def rebuild_manifest(out_dir):
    """satellite_out/shards/ 配下の全シャードを走査し、manifest.jsonを再構築する。
    Webビューアはまずこのファイルだけを読み、選択された期間に該当する
    シャードのgeojsonだけを個別に取得する（全件を毎回ダウンロードしないため）。"""
    out_dir = Path(out_dir)
    shards_dir = out_dir / SHARDS_DIR_NAME
    entries = []
    if shards_dir.exists():
        for state_file in sorted(shards_dir.glob("*.json")):
            key = state_file.stem
            try:
                state = load_state(state_file)
            except (json.JSONDecodeError, OSError):
                continue
            entries.append({"key": key, "count": len(state)})
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shards": entries,
        "total_count": sum(e["count"] for e in entries),
    }
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


class ShardCache:
    """このラン中に読み書きするシャードをメモリ上に保持する薄いラッパー。
    触れたシャードのキーを touched に記録し、チェックポイント時に
    それだけをディスクへ書き出す・コミットする対象として使う。"""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self._cache = {}
        self.touched = set()

    def get(self, key):
        if key not in self._cache:
            self._cache[key] = load_shard(self.out_dir, key)
        return self._cache[key]

    def mark_touched(self, key):
        self.touched.add(key)

    def flush_to_disk(self, keys):
        for key in keys:
            if key in self._cache:
                save_shard(self.out_dir, key, self._cache[key])


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


def run_once(out_dir="satellite_out", lookback_days=3, chunk_days=3, mgp_chunk_days=3,
             request_delay=0.3, checkpoint_fn=None, checkpoint_every=1):
    """checkpoint_fn: (touched_shard_keys: set[str]) -> bool を渡すと、
    date_chunkを checkpoint_every 回処理するごとにシャードをディスクへ書き出し、
    このコールバックでコミット・pushする（commit_state.pyのcommit_and_push_shards
    を渡す想定）。大規模バックフィルの途中でジョブが落ちても、直前の
    チェックポイントまでの成果はGitHubに残るようにするための仕組み
    （2026-08、1000日バックフィルが最後のpush失敗で全損した反省から導入）。
    戻り値がFalse（push失敗）の場合、そのシャード群は次のチェックポイントで
    再度対象に含める（呼び出し側で管理する必要はなく、touchedから消さない）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = ShardCache(out_dir)
    bbox, ring = load_aoi()

    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=lookback_days)
    now_iso = now.isoformat()

    effective_chunk_days = chunk_days if lookback_days > chunk_days else lookback_days + 1
    effective_mgp_chunk_days = mgp_chunk_days if lookback_days > mgp_chunk_days else lookback_days + 1
    chunks = list(date_chunks(start_dt, now, effective_chunk_days))
    mgp_chunks = list(date_chunks(start_dt, now, effective_mgp_chunk_days))
    print(f"検索範囲: 過去{lookback_days}日分 / Copernicus{len(chunks)}回（約{effective_chunk_days}日単位） / MGP{len(mgp_chunks)}回（約{effective_mgp_chunk_days}日単位）", file=sys.stderr)

    new_count = 0
    mgp_key = os.environ.get("MGP_API_KEY", "").strip()

    TARGET_COLLECTIONS = ["SENTINEL-1", "SENTINEL-2"]
    EXCLUDED_MGP_SENSORS = {"WV04", "QB02", "IK02"}
    MGP_GRID_COLS, MGP_GRID_ROWS = 3, 3
    mgp_cells = geo_utils.split_bbox_grid(bbox, MGP_GRID_COLS, MGP_GRID_ROWS)

    pending_checkpoint_keys = set()  # 直近のチェックポイントでpushが失敗し、持ち越されたキー

    def maybe_checkpoint(force=False, chunk_counter=[0]):
        """チェックポイント間隔（checkpoint_every回のチャンク処理ごと）に達したら、
        触れたシャードをディスクへ書き出してコミット・push する。"""
        nonlocal pending_checkpoint_keys
        if checkpoint_fn is None:
            return
        chunk_counter[0] += 1
        if not force and chunk_counter[0] % checkpoint_every != 0:
            return
        keys = cache.touched | pending_checkpoint_keys
        if not keys:
            return
        cache.flush_to_disk(keys)
        ok = checkpoint_fn(keys)
        if ok:
            cache.touched -= keys
            pending_checkpoint_keys = set()
        else:
            # push失敗分は次回に持ち越す（データはローカルディスクに書き出し済みなので
            # このプロセスが生きている限りは失われない）。
            pending_checkpoint_keys = keys

    # ---------------- MGP Pro（独自のチャンク幅 × 地理グリッドで回す） ----------------
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
                                continue
                        rec = to_state_record(raw, now_iso)
                        key = item_key(rec)
                        sk = shard_key(rec["source"], rec.get("datetime"))
                        shard_state = cache.get(sk)
                        if key not in shard_state:
                            shard_state[key] = rec
                            cache.mark_touched(sk)
                            new_count += 1
                            kept += 1
                    if raws:
                        print(f"[MGP] {chunk_start[:10]}〜{chunk_end[:10]} cell{[round(v,1) for v in cell_bbox]}: 取得{len(raws)}件 / 新規{kept}件", file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    print(f"[MGP] {chunk_start[:10]}〜{chunk_end[:10]} cell{[round(v,1) for v in cell_bbox]}: エラー: {e}", file=sys.stderr)
                time.sleep(request_delay)
            maybe_checkpoint()
    else:
        print("[MGP] MGP_API_KEY未設定のためスキップ", file=sys.stderr)

    # ---------------- Copernicus ----------------
    for chunk_start, chunk_end in chunks:
        for collection in TARGET_COLLECTIONS:
            try:
                raws = copernicus.search(bbox, chunk_start, chunk_end, [collection], top=1000, aoi_ring=ring, max_pages=10)
                kept = 0
                for raw in raws:
                    if raw.get("sensor") == "-":
                        continue
                    rec = to_state_record(raw, now_iso)
                    key = item_key(rec)
                    sk = shard_key(rec["source"], rec.get("datetime"))
                    shard_state = cache.get(sk)
                    if key not in shard_state:
                        shard_state[key] = rec
                        cache.mark_touched(sk)
                        new_count += 1
                        kept += 1
                print(f"[Copernicus:{collection}] {chunk_start[:10]}〜{chunk_end[:10]}: 取得{len(raws)}件 / 新規{kept}件", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"[Copernicus:{collection}] {chunk_start[:10]}〜{chunk_end[:10]}: エラー: {e}", file=sys.stderr)
            time.sleep(request_delay)
        maybe_checkpoint()

    # 最終フラッシュ: チェックポイント間隔の端数や、checkpoint_fn未指定時の
    # 通常運用（差分ポーリング）は、ここで初めてディスクへ書き出す。
    cache.flush_to_disk(cache.touched | pending_checkpoint_keys)
    rebuild_manifest(out_dir)
    if checkpoint_fn is not None:
        remaining = cache.touched | pending_checkpoint_keys
        if remaining:
            ok = checkpoint_fn(remaining)
            if not ok:
                print(f"WARNING: 最終チェックポイントのpushに失敗しました。未反映のシャード: {sorted(remaining)}", file=sys.stderr)

    total = len(cache._cache)
    print(f"完了: 新規{new_count}件（触れたシャード数: {total}）", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="1回だけ実行して終了（現状これ以外のモードはない）")
    ap.add_argument("--out-dir", default="satellite_out")
    ap.add_argument("--lookback-days", type=int, default=int(os.environ.get("LOOKBACK_DAYS", "3")))
    ap.add_argument("--chunk-days", type=int, default=int(os.environ.get("CHUNK_DAYS", "3")),
                     help="この日数を超えるlookback-daysの場合、この日数単位に分割してAPIを叩く（APIの返却件数上限による取りこぼし防止）。Copernicus用")
    ap.add_argument("--mgp-chunk-days", type=int, default=int(os.environ.get("MGP_CHUNK_DAYS", "3")),
                     help="MGP用のチャンク幅。MGPはCopernicusより新規撮影量が多いため、既定でCopernicusより細かく分割する")
    ap.add_argument("--checkpoint-every", type=int, default=int(os.environ.get("CHECKPOINT_EVERY", "10")),
                     help="このチャンク数ごとにシャードをコミット・pushする（大規模バックフィル時の途中経過保護用）。"
                          "0を指定するとチェックポイントを無効化し、最後に1回だけコミットする（従来同様の挙動）。")
    ap.add_argument("--no-checkpoint-push", action="store_true",
                     help="チェックポイント時のgit操作を無効化する（テスト用。指定時はシャードのディスク書き出しのみ行う）")
    ap.add_argument("--branch", default="main", help="チェックポイント時にpushするブランチ")
    args = ap.parse_args()

    checkpoint_fn = None
    if not args.no_checkpoint_push and args.checkpoint_every > 0:
        import commit_state
        checkpoint_fn = lambda keys: commit_state.commit_and_push_shards(  # noqa: E731
            args.out_dir, args.branch, keys,
            commit_message="Auto-update satellite shards (checkpoint) [skip ci]",
        )

    run_once(args.out_dir, args.lookback_days, args.chunk_days, args.mgp_chunk_days,
              checkpoint_fn=checkpoint_fn, checkpoint_every=max(args.checkpoint_every, 1))


if __name__ == "__main__":
    main()
