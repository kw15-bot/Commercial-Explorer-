"""
MGP Pro (Maxar Geospatial Platform) Discovery API クライアント。
STAC互換のFeatureCollectionを返す。

参考:
  GET https://api.maxar.com/discovery/v1/search
  header: maxar-api-key: <API_KEY>

注意:
  Discovery APIはbboxでの矩形検索のみをサポートする想定で実装している
  （intersectsでの任意ポリゴン検索に対応しているかは契約により未確認のため、
  ここでは安全側でbboxのみを使用）。より厳密な地域絞り込みは、呼び出し側で
  footprintの重心に対して geo_utils.point_in_ring 等を使って後段フィルタする。

  レスポンスのプロパティ名（eo:cloud_cover, gsd, platform 等）は
  契約・APIバージョンにより差異があり得る。実データで挙動が合わない
  場合は normalize() を調整すること。
"""
import logging
import requests

logger = logging.getLogger(__name__)

DISCOVERY_URL = "https://api.maxar.com/discovery/v1/search"


def search(bbox, start_iso, end_iso, collections, api_key, limit=100, timeout=30, max_pages=5):
    """レスポンスのSTAC links配列に rel:"next" があれば追跡して追加ページを
    取得する（1リクエストのlimitには上限があるため、これがないと取りこぼす）。
    max_pages は無限ループ・過度なAPI呼び出しを防ぐための安全弁。"""
    if not api_key:
        raise RuntimeError("MGP_API_KEY が設定されていません")

    params = {
        "bbox": ",".join(str(v) for v in bbox),
        "datetime": f"{start_iso}/{end_iso}",
        "limit": limit,
        "sortby": "-datetime",  # 明示的に新しい順にしないと、返却順が不定でページ上限に
                                 # 収まらない場合に狙った日付が欠落しうる（STAC sort拡張の記法）
    }
    if collections:
        params["collections"] = ",".join(collections)

    headers = {"maxar-api-key": api_key, "Accept": "application/json"}

    all_features = []
    url = DISCOVERY_URL
    request_params = params
    for _ in range(max_pages):
        resp = requests.get(url, params=request_params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        all_features.extend(data.get("features", []))
        next_href = None
        for link in data.get("links", []) or []:
            if link.get("rel") == "next" and link.get("href"):
                next_href = link["href"]
                break
        if not next_href:
            break
        url = next_href
        request_params = None  # next hrefは完全なURL（クエリ文字列込み）なのでparams不要

    return [normalize(f) for f in all_features]


def normalize(feature):
    p = feature.get("properties", {}) or {}
    assets = feature.get("assets", {}) or {}

    links = [
        {"label": f"asset:{key}", "url": a["href"]}
        for key, a in assets.items()
        if a and a.get("href")
    ]
    # STACの self / order 系リンクも「元ページを開く」候補として拾っておく
    for l in feature.get("links", []) or []:
        if l.get("href") and l.get("rel") in ("self", "order", "stream"):
            links.append({"label": f"link:{l['rel']}", "url": l["href"]})

    raw_platform = p.get("platform") or p.get("constellation") or p.get("mission") or ""
    return {
        "source": "MGP",
        "id": feature.get("id"),
        "sensor": _resolve_sensor(raw_platform),
        "datetime": p.get("datetime"),
        "cloud_cover": p.get("eo:cloud_cover", p.get("cloudCover")),
        "resolution": f"{p['gsd']} m" if p.get("gsd") else "-",
        "name": feature.get("id"),
        "links": links,
        "footprint": {"type": "Polygon", "coordinates": [_bbox_to_ring(feature.get("bbox"))]}
        if feature.get("bbox")
        else feature.get("geometry"),
        "raw": feature,
    }


# Discovery APIの properties.platform は表記ゆれ（"WorldView-2" / "WV02" /
# "worldview-02" 等）があり得るため、正規化してこのビューアのSATELLITES一覧
# （GE01・WV01-03・WV03_SWIR/VNIR・LG01-06・QB02・WV04・IK02）が期待する
# IDに揃える。実データで想定外の表記が来た場合は _PLATFORM_ALIASES に追記
# するか、マッチしなければ大文字化した生の値をそのまま返す（フィルタには
# 引っかからなくなるが、値自体は残るのでデバッグしやすい）。
_PLATFORM_ALIASES = {
    "GE01": "GE01", "GEOEYE-1": "GE01", "GEOEYE1": "GE01", "GEOEYE-01": "GE01",
    "WV01": "WV01", "WORLDVIEW-1": "WV01", "WORLDVIEW1": "WV01", "WORLDVIEW-01": "WV01",
    "WV02": "WV02", "WORLDVIEW-2": "WV02", "WORLDVIEW2": "WV02", "WORLDVIEW-02": "WV02",
    "WV03": "WV03", "WORLDVIEW-3": "WV03", "WORLDVIEW3": "WV03", "WORLDVIEW-03": "WV03",
    "WV03_SWIR": "WV03_SWIR", "WV03-SWIR": "WV03_SWIR",
    "WV03_VNIR": "WV03_VNIR", "WV03-VNIR": "WV03_VNIR",
    "WV04": "WV04", "WORLDVIEW-4": "WV04", "WORLDVIEW4": "WV04", "WORLDVIEW-04": "WV04", "LEGACY_WV04": "WV04",
    "QB02": "QB02", "QUICKBIRD-2": "QB02", "QUICKBIRD2": "QB02", "QUICKBIRD-02": "QB02",
    "IK02": "IK02", "IKONOS-2": "IK02", "IKONOS2": "IK02", "IKONOS-02": "IK02",
    "LG01": "LG01", "LEGION-1": "LG01", "LEGION1": "LG01", "LEGION-01": "LG01",
    "LG02": "LG02", "LEGION-2": "LG02", "LEGION2": "LG02", "LEGION-02": "LG02",
    "LG03": "LG03", "LEGION-3": "LG03", "LEGION3": "LG03", "LEGION-03": "LG03",
    "LG04": "LG04", "LEGION-4": "LG04", "LEGION4": "LG04", "LEGION-04": "LG04",
    "LG05": "LG05", "LEGION-5": "LG05", "LEGION5": "LG05", "LEGION-05": "LG05",
    "LG06": "LG06", "LEGION-6": "LG06", "LEGION6": "LG06", "LEGION-06": "LG06",
}


def _resolve_sensor(raw_platform):
    key = (raw_platform or "").strip().upper().replace(" ", "")
    return _PLATFORM_ALIASES.get(key, key or "-")


def _bbox_to_ring(bbox):
    w, s, e, n = bbox[:4]
    return [[w, s], [e, s], [e, n], [w, n], [w, s]]
