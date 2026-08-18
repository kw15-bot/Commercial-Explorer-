"""
Copernicus Data Space Ecosystem (CDSE) OData API クライアント。

検索: GET https://catalogue.dataspace.copernicus.eu/odata/v1/Products
      認証不要（公開カタログ検索）。ダウンロードにのみアクセストークンが必要だが、
      本ツールは現在ダウンロードを外部リンク方式（Copernicus Browser側で認証）に
      しているため、このモジュールは検索のみで完結する。

Copernicus Browser の画面スクレイピングは行わず、Browser自身が内部で
呼んでいるのと同じ公式カタログAPIを直接叩く。
"""
import logging
import requests

logger = logging.getLogger(__name__)

ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
DOWNLOAD_BASE = "https://download.dataspace.copernicus.eu/odata/v1/Products"
BROWSER_BASE = "https://browser.dataspace.copernicus.eu"


def _wkt_polygon_from_bbox(bbox):
    w, s, e, n = bbox[:4]
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def _wkt_polygon_from_ring(ring):
    pts = ",".join(f"{lon} {lat}" for lon, lat in ring)
    return f"POLYGON(({pts}))"


def search(bbox, start_iso, end_iso, collections, top=100, timeout=30, aoi_ring=None):
    """aoi_ring（[[lon,lat],...]の閉じたリング）を渡すと、bboxではなくその
    正確なポリゴンでintersects検索する。渡さない場合はbboxの矩形を使う。"""
    poly = _wkt_polygon_from_ring(aoi_ring) if aoi_ring else _wkt_polygon_from_bbox(bbox)

    filters = [
        f"ContentDate/Start gt {start_iso}",
        f"ContentDate/Start lt {end_iso}",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{poly}')",
    ]
    if collections:
        coll_filter = " or ".join(f"Collection/Name eq '{c}'" for c in collections)
        filters.append(f"({coll_filter})")

    params = {
        "$filter": " and ".join(filters),
        "$orderby": "ContentDate/Start desc",
        "$top": top,
        "$expand": "Attributes",
    }
    resp = requests.get(ODATA_URL, params=params, headers={"Accept": "application/json"}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return [normalize(p) for p in data.get("value", [])]


def normalize(product):
    attrs = product.get("Attributes", []) or []

    def get_attr(name):
        for a in attrs:
            if a.get("Name") == name:
                return a.get("Value")
        return None

    product_id = product.get("Id")
    name = product.get("Name")
    return {
        "source": "Copernicus",
        "id": product_id,
        "sensor": (name or "").split("_")[0] or "-",
        "datetime": (product.get("ContentDate") or {}).get("Start"),
        "cloud_cover": get_attr("cloudCover"),
        "resolution": "-",
        "name": name,
        "links": [
            {"label": "OData詳細", "url": f"{ODATA_URL}({product_id})"},
            {"label": "Copernicus Browserで開く", "url": f"{BROWSER_BASE}/?zoom=8&product={name}" if name else BROWSER_BASE},
        ],
        "footprint": product.get("GeoFootprint"),
        "raw": product,
    }


def get_access_token(username, password, timeout=30):
    """現在は未使用（ダウンロードは外部リンク方式のため）。将来サーバー経由
    ダウンロードに切り替える場合のために残してある。"""
    if not username or not password:
        raise RuntimeError("COPERNICUS_USERNAME / COPERNICUS_PASSWORD が設定されていません")
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "cdse-public",
            "username": username,
            "password": password,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_download_url(product_id):
    return f"{DOWNLOAD_BASE}({product_id})/$value"
