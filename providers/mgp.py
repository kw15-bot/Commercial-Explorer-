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


def search(bbox, start_iso, end_iso, collections, api_key, limit=100, timeout=30):
    if not api_key:
        raise RuntimeError("MGP_API_KEY が設定されていません")

    params = {
        "bbox": ",".join(str(v) for v in bbox),
        "datetime": f"{start_iso}/{end_iso}",
        "limit": limit,
    }
    if collections:
        params["collections"] = ",".join(collections)

    headers = {"maxar-api-key": api_key, "Accept": "application/json"}
    resp = requests.get(DISCOVERY_URL, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return [normalize(f) for f in data.get("features", [])]


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

    return {
        "source": "MGP",
        "id": feature.get("id"),
        "sensor": p.get("platform") or p.get("constellation") or p.get("mission") or "-",
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


def _bbox_to_ring(bbox):
    w, s, e, n = bbox[:4]
    return [[w, s], [e, s], [e, n], [w, n], [w, s]]
